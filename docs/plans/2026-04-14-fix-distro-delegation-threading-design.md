# Fix Delegation in amp-distro Web Mode — ThreadedToolWrapper Cross-Event-Loop Error

## Goal

Fix the cross-event-loop error that breaks `tool-delegate`, `tool-task`, and `tool-recipes` in amp-distro web mode by inverting the `ThreadedToolWrapper` wrapping logic to only wrap known blocking tools, then progressively eliminate the need for wrapping entirely.

## Background

Amplifier runs in two modes: **CLI** (single event loop, everything inline) and **Web** (amp-distro, uvicorn + SSE streaming to the browser). In the web UI, Server-Sent Events stream LLM responses to the browser. If a tool runs synchronous blocking I/O (e.g., `subprocess.run()`), it freezes the event loop and the SSE stream goes dead — the browser shows no updates.

To solve this, `ThreadedToolWrapper` was introduced. It wraps every tool's `execute()` call:

```python
# ThreadedToolWrapper.execute() — amplifierd/threading.py:38-40
coro = tool.execute(input)
return await asyncio.to_thread(asyncio.run, coro)
```

This creates a **fresh event loop in a worker thread** per tool call. For simple tools (`grep`, `read_file`, `glob`), this works fine — they do their I/O and return. But for session-spawning tools (`tool-delegate`, `tool-task`, `tool-recipes`), it causes a fatal cross-event-loop crash.

### Why Session-Spawning Tools Crash

The crash follows a precise four-step sequence:

1. **Coroutine creation on main loop:** `coro = tool.execute(input)` runs on the main uvicorn loop. Inside `tool-delegate`, the PyO3 Rust bridge calls `pyo3_async_runtimes::future_into_py()` which **snapshots the current event loop** as the return address for async results.

2. **Execution on worker loop:** `asyncio.to_thread(asyncio.run, coro)` moves the coroutine to a brand-new temporary event loop in a worker thread.

3. **Cross-loop delivery:** The Rust engine tries to deliver results via `loop.call_soon_threadsafe()` back to the **main loop** (captured in step 1), but the coroutine is being driven by the **worker thread's loop**.

4. **Crash:** Python raises `RuntimeError: Task got Future attached to a different loop`, which surfaces as `ProviderUnavailableError` before any LLM call completes.

### Exact Error From Production

```
"error_message": "Task <Task pending name='Task-345'
    coro=<run_orchestrator() running at
    amplifier_core/_session_exec.py:43>
    cb=[<builtins.PyTaskCompleter object at 0x7a2e4194a810>()]>
    got Future <Future pending
    cb=[_chain_future.<locals>._call_check_cancel() at
    asyncio/futures.py:393]> attached to a different loop",
"error_type": "ProviderUnavailableError"
```

`PyTaskCompleter` is the PyO3 Rust-side callback, bound to the main loop at coroutine creation. `_chain_future._call_check_cancel` is Python asyncio's cross-loop chain — it cannot cross loop boundaries.

### Why CLI Works

CLI has no `ThreadedToolWrapper`. One event loop, everything inline. The PyO3 bridge captures the same loop it executes on. No mismatch, no crash.

### Additional Cross-Loop Crash Sites

Beyond the primary PyO3 crash, there are secondary cross-loop interactions that would also fail:

- `parent_cancel.register_child(child_cancel)` — parent's cancellation token lives on the main loop
- `session_manager.register()` — called from worker thread, lives on main loop
- `EventBus.publish()` — hook handlers fire from worker thread into main-loop EventBus

## Approach

**Hybrid: Invert the wrapping logic immediately (Phase 1), then fix blocking tools at the source progressively (Phase 2).**

The key insight from live-probing the tool ecosystem: most tools are **already async-safe**. Only a subset actually do synchronous blocking I/O. The original `ThreadedToolWrapper` was a blanket solution based on the assumption that all tools might block — that assumption is wrong.

### Why Not Other Approaches

| Alternative | Why rejected |
|------------|-------------|
| **Marker attribute (`__threading_exempt__`)** | Inverted default — wrapping should be opt-in (for blockers), not opt-out. New tools should work correctly by default without declaring anything. |
| **Fix at Rust/kernel level** | Loop affinity is captured by `pyo3_async_runtimes::future_into_py()` — a third-party library constraint. The fix belongs in the app layer where the wrapping decision is made. |
| **Threading metadata in Tool contract** | Fails the kernel litmus test: "Could two teams want different behavior?" Yes — CLI doesn't need wrapping at all. Threading strategy is deployment policy, not kernel mechanism. |
| **Run entire sessions in background threads** | Disproportionate. `EventBus` is main-loop-only and not thread-safe. Would require thread-safe event forwarding, cancellation bridging, and session lifecycle management across threads. |

## Architecture

### Current Architecture (Broken)

```
Model calls tool-delegate
    → ThreadedToolWrapper.execute()
        → coro created on main loop (PyO3 captures main loop)
        → asyncio.to_thread(asyncio.run, coro)
            → NEW worker thread event loop
            → tool-delegate.execute() runs here
            → spawn_fn() creates child AmplifierSession
            → RustSession.execute() → run_orchestrator() Task
            → PyO3 tries to deliver result to main loop
            → Worker loop is driving the Task
            → CRASH: "Future attached to a different loop"
```

### Fixed Architecture (Phase 1)

```
Model calls tool-delegate
    → wrap_tools_for_threading() skips it (not in _NEEDS_THREADING)
    → tool-delegate.execute() runs on main uvicorn loop
    → spawn_fn() creates child AmplifierSession on main loop
    → RustSession.execute() → run_orchestrator() Task on main loop
    → PyO3 delivers result to main loop
    → Same loop throughout → works

Model calls grep
    → wrap_tools_for_threading() wraps it (in _NEEDS_THREADING)
    → ThreadedToolWrapper.execute() → worker thread
    → subprocess.run() blocks worker thread, not main loop
    → SSE stream stays responsive → works
```

### End-State Architecture (Phase 2 Complete)

```
All tools run on main uvicorn loop
    → No ThreadedToolWrapper needed
    → grep uses asyncio.create_subprocess_exec() (non-blocking)
    → read_file uses asyncio.to_thread(path.read_text) (non-blocking)
    → SSE stays responsive because nothing blocks the loop
    → ThreadedToolWrapper deleted entirely
```

## Components

### Component 1: Inverted `wrap_tools_for_threading()` (Phase 1)

The core change. Replace the "wrap everything" logic with "wrap only known blockers."

```python
# amplifierd/src/amplifierd/threading.py

_NEEDS_THREADING = frozenset({
    "grep", "glob", "python_check",
    "read_file", "write_file", "edit_file", "apply_patch",
    "load_skill", "web_fetch",
})

def wrap_tools_for_threading(session: Any) -> None:
    coordinator = getattr(session, "coordinator", None)
    if coordinator is None:
        return
    get_fn = getattr(coordinator, "get", None)
    if get_fn is None or not callable(get_fn):
        return
    tools: Any = get_fn("tools")
    if not tools:
        return
    if isinstance(tools, dict):
        for key in list(tools):
            tool = tools[key]
            if isinstance(tool, ThreadedToolWrapper):
                continue  # idempotency guard — prevents double-wrapping
            tool_name = getattr(tool, 'name', key)
            if tool_name in _NEEDS_THREADING:
                tools[key] = ThreadedToolWrapper(tool)
    else:
        wrapped = []
        for tool in tools:
            if isinstance(tool, ThreadedToolWrapper):
                wrapped.append(tool)
            elif getattr(tool, 'name', '') in _NEEDS_THREADING:
                wrapped.append(ThreadedToolWrapper(tool))
            else:
                wrapped.append(tool)
        coordinator["tools"] = wrapped
```

**Key details:**

- `_NEEDS_THREADING` is a `frozenset` — immutable, fast lookups.
- The idempotency guard (`isinstance(tool, ThreadedToolWrapper)`) prevents double-wrapping when `wrap_tools_for_threading()` is called on child sessions that inherit parent tools.
- Both the dict-path (amplifierd) and list-path (amplifier-chat) are handled.
- `ThreadedToolWrapper` itself is unchanged — it still does `asyncio.to_thread(asyncio.run, coro)` for the tools that need it.

### Component 2: `spawn.py` — No Change

`spawn.py:261` calls `wrap_tools_for_threading(child_session)` unconditionally. This stays as-is. With the inverted logic in `wrap_tools_for_threading()`, it correctly wraps only blocking tools in child sessions and leaves session-spawning tools on the main loop. The idempotency guard handles edge cases.

### Component 3: `amplifier-chat` Convergence

`amplifier-chat` has a diverged copy of `threading.py` (list-only tools path, no `callable()` guard). The fix:

1. Apply the inverted wrapping logic to the `amplifierd` version of `threading.py`
2. Copy the updated file to `amplifier-chat`, replacing its simpler version
3. Bump the `amplifierd` dependency version in `amplifier-chat` to pick up changes

Both repos get identical, correct code. Structural unification (shared package extraction) is a separate future effort.

### Component 4: Progressive Tool Fixes (Phase 2)

Each blocking tool gets fixed independently in its own upstream module repo. As each is fixed, remove it from `_NEEDS_THREADING`. When the set is empty, delete all wrapping infrastructure.

## Data Flow

### Phase 1: Tool Execution in Web Mode

```
Incoming SSE request
    → uvicorn main loop
    → session.execute() on main loop
    → model returns tool call
    → is tool in _NEEDS_THREADING?
        YES → ThreadedToolWrapper
            → worker thread + temp event loop
            → tool.execute() (blocking I/O runs here)
            → result returned to main loop via asyncio Future
        NO → tool.execute() runs directly on main loop
            → async I/O yields back to loop naturally
    → result sent to model
    → SSE stream delivers response to browser
```

### Child Session Spawning (Delegation)

```
Parent session on main loop
    → model calls tool-delegate (NOT wrapped)
    → tool-delegate.execute() on main loop
    → spawn_fn() creates child AmplifierSession on main loop
    → wrap_tools_for_threading(child_session)
        → child's grep/glob/etc. get wrapped
        → child's delegate stays unwrapped
    → child session executes on main loop
    → PyO3 types consistent — same loop throughout
    → results forwarded to parent via EventBus (main loop)
```

## Error Handling

- **Unknown tool names:** Tools not in `_NEEDS_THREADING` run unwrapped. This is the safe default — async tools work correctly on the main loop.
- **New blocking tools added without updating the set:** SSE may freeze during that tool's execution. This is a degraded experience, not a crash. The fix is to add the tool name to `_NEEDS_THREADING` or (better) fix the tool's blocking I/O at the source.
- **Double-wrapping prevention:** The `isinstance(tool, ThreadedToolWrapper)` guard ensures idempotency across parent→child session inheritance.

## Testing Strategy

### Phase 1 Unit Tests (`threading.py`)

- Tools in `_NEEDS_THREADING` get wrapped in `ThreadedToolWrapper`
- Tools NOT in the set stay unwrapped (verified: `delegate`, `task`, `recipes`, `bash`)
- Already-wrapped tools don't get double-wrapped (idempotency guard)
- Both dict-path and list-path work correctly
- Mixed tool sets (some need wrapping, some don't) handled correctly
- Empty tool sets handled gracefully

### Phase 1 Integration Tests

- Trigger a delegation chain in web mode: parent → delegate → child session
- Verify no cross-event-loop errors (`ProviderUnavailableError`)
- Verify SSE stream stays responsive during delegation
- Verify wrapped tools (`grep`, `glob`) still run without blocking SSE
- Verify `tool-recipes` can execute multi-step workflows in web mode

### Phase 2 Per-Tool Tests

For each tool fixed at the source:
- Existing test suite passes (behavior unchanged)
- Event loop blocking test using heartbeat probe pattern (confirm the tool no longer blocks)
- Removal from `_NEEDS_THREADING` verified — tool runs correctly unwrapped in distro

## Phase 2 — Fix Blockers at the Source

### Verified Blocking Tools and Correct Fixes

All blocking calls were verified against actual source code with file paths and line numbers.

#### P1 — High Severity

| Tool | Blocking Call | Location | Fix |
|------|-------------|----------|-----|
| `grep` (ripgrep path) | `subprocess.run()` | `grep.py:350` | Replace with `asyncio.create_subprocess_exec()` |
| `grep` (python fallback) | sync `glob()`, `open()`, `read()` | `grep.py:667-731` | Wrap entire `_execute_python()` in `asyncio.to_thread()` — can't async-ify piecemeal |
| `python_check` | `subprocess.run()` x3 | `checker.py:127,169,217` | `await asyncio.to_thread(check_files, ...)` at the **tool layer**, NOT inside the checker library (it's a sync library) |
| `load_skill` | `subprocess.run()` with 120s timeout | `sources.py:134` | `asyncio.create_subprocess_exec()` — matching the pattern already used at `sources.py:147`. Also fix `shutil.rmtree()` at line 129 and `write_text()` |

#### P2 — Filesystem Tools

| Tool | Blocking Call | Location | Fix |
|------|-------------|----------|-----|
| `read_file` | `path.read_text()`, `path.iterdir()` | `read.py:202`, `read.py:174` | `await asyncio.to_thread(...)` |
| `write_file` | `mkdir()` + `write_text()` | `write.py:143,146` | `await asyncio.to_thread(...)` — hook `emit()` stays outside the thread |
| `edit_file` | `read_text()` + `write_text()` | `edit.py:181,220` | `await asyncio.to_thread(...)` |
| `apply_patch` | `read_text()`, `write_text()`, `unlink()` | `native.py:256-361`, `function.py:263-311` | `await asyncio.to_thread(...)` — `_emit_event()` stays outside the thread |
| `glob` | `path.glob()`, `stat()`, `is_file()` | `glob.py:131-160` | Wrap entire traversal in `asyncio.to_thread()` |

#### P3 — Minor

| Tool | Blocking Call | Location | Fix |
|------|-------------|----------|-----|
| `web_fetch` (save-to-file path) | `path.write_text()` | `__init__.py:438` | `await asyncio.to_thread(...)` — HTTP streaming is already async |
| `tool-recipes` (session persistence) | `json.dump()`/`json.load()` | various | `await asyncio.to_thread(...)` — small files, low severity |

### Phase 2 Per-Tool Rollout

For each tool fix:
1. PR to the tool's upstream module repo (e.g., `amplifier-module-tool-search`)
2. Run existing tests — behavior is unchanged, only async wrapping changes
3. Add event loop blocking test
4. Remove tool from `_NEEDS_THREADING` in `amplifierd` + `amplifier-chat`
5. Verify distro works with the tool unwrapped

### Phase 2 End State

When all tools are fixed, `_NEEDS_THREADING` is empty. Delete:
- `ThreadedToolWrapper` class
- `wrap_tools_for_threading()` function
- All wrapping call sites in `spawn.py` and `session_manager.py`

Every tool runs on the main loop. No wrapping infrastructure remains.

## Rollout Plan

| Step | Scope | Description |
|------|-------|-------------|
| 1 | `amplifierd` | PR: inverted wrapping logic in `threading.py` |
| 2 | `amplifier-chat` | PR: converge `threading.py` to match `amplifierd` + bump `amplifierd` dependency version |
| 3 | Upstream tool modules | Independent PRs for P1 tools: `grep`, `python_check`, `load_skill` |
| 4 | `amplifierd` + `amplifier-chat` | Progressive removal from `_NEEDS_THREADING` as tool PRs merge |
| 5 | Upstream tool modules | Independent PRs for P2/P3 tools |
| 6 | `amplifierd` + `amplifier-chat` | Final cleanup: delete `ThreadedToolWrapper` and all wrapping infrastructure |

## Open Questions

1. **`nano-banana` and `dot_graph` audit:** Should these tools be audited for blocking behavior? They weren't in scope of the initial investigation and may contain `subprocess.run()` calls.

2. **`amplifier-chat` / `amplifierd` unification:** Both repos share the same `threading.py` and `spawn.py` code. This fix converges the files but doesn't solve the underlying "two repos, same code" problem. A longer-term shared package extraction is needed.

3. **`EventBus` thread safety:** `EventBus.publish()` is called from within tool execution context. If sessions ever move to background threads (future architecture), `EventBus` would need to become thread-safe. Not required for this fix since session-spawning tools now run on the main loop.
