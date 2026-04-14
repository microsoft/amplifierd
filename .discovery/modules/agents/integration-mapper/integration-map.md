# Integration Map — dot-graph

> **Agent:** integration-mapper (WHERE/WHY)
> **Topic:** `dot-graph` — the DOT/Graphviz tool bundle loaded into amplifierd
> **Scope:** Cross-boundary integration of the `dot-graph` tool with every adjacent
> mechanism: bundle loading, threading isolation, subprocess boundary, SSE transport,
> filesystem layout contract, and the discovery pipeline's dual-path invocation.
>
> Single-perspective findings (HOW the tool works internally) are in `code-tracer/findings.md`.
> This document focuses on **what happens at each boundary** — the emergent behavior that
> only appears when `dot-graph` composes with adjacent systems.

---

## Overview: The Integration Landscape

`dot-graph` crosses **seven distinct component boundaries** on its path from bundle cache
to SSE wire. At each boundary, something is transformed, isolated, or potentially broken.

```
Bundle Cache (filesystem)
  │  importlib.metadata entry-point discovery
  ▼
amplifier_core Loader ─→ coordinator.mount("tools", DotGraphTool)
  │  wrap_tools_for_threading()
  ▼
ThreadedToolWrapper (amplifierd threading.py)
  │  asyncio.to_thread(asyncio.run, coro)     ← EVENT LOOP BOUNDARY
  ▼
Worker Thread / asyncio.run(DotGraphTool.execute())
  │  subprocess.run([graphviz...])             ← PROCESS BOUNDARY
  ▼
graphviz binary (external process)
  │
  ▼ ToolResult(success, json_str)
amplifier_core hooks.emit("tool:post", data)
  │  _wire_events() in session_handle.py      ← SESSION BOUNDARY
  ▼
EventBus.publish() → asyncio.Queue → SSE stream ← TRANSPORT BOUNDARY
```

In parallel, the dot-graph bundle's **discovery pipeline** uses the same tool via a completely
different invocation path — direct Python import in recipe bash steps, bypassing the entire
amplifierd tool infrastructure.

---

## Boundary 1: Bundle Cache → Entry Point → amplifier_core Loader

**Files:** `pyproject.toml` (bundle cache), `amplifier_core/loader.py:103-136`

The dot-graph tool enters amplifierd through Python's package entry-point system, not through
any amplifierd-specific import mechanism.

### What crosses this boundary

| Direction | Data | Protocol |
|-----------|------|----------|
| Bundle → Loader | entry-point name `tool-dot-graph` | `importlib.metadata.entry_points(group="amplifier.modules")` |
| Loader → Mount | `source_hint` (bundle cache path) | `source_resolver.async_resolve()` OR `_load_direct()` |
| Mount → Coordinator | `DotGraphTool` instance | `coordinator.mount("tools", tool, name="dot_graph")` |

### The naming-convention protocol

The entry-point name `tool-dot-graph` is parsed by `loader.py:114` using a naming convention:
prefix `tool-` → type `tool` → mount point `tools`. This is a **string-based protocol**, not a
typed interface. Any entry-point registered under `amplifier.modules` with a `tool-` prefix is
assumed to be a tool, with no schema validation of the registered object.

### Two resolution paths

`loader.py:176-250` has two paths based on whether a `module-source-resolver` is mounted:

1. **Source-resolver path:** `source_resolver.async_resolve(module_id, source_hint)` → `source.resolve()` → import from bundle cache path
2. **Direct-discovery fallback:** `_load_direct()` → entry-point only, no source-resolver

In normal amplifierd sessions, the source-resolver path is taken (the bundle system mounts a
resolver on the coordinator). The fallback exists for headless/test contexts. The `tool-dot-graph`
package is pip-installed in the bundle's virtual environment, so both paths ultimately invoke
the same `amplifier_module_tool_dot_graph:mount` entry point.

### What the boundary does NOT transfer

The loader boundary does not transfer:
- **Configuration** — `config` dict is passed to `mount(coordinator, config)` but `DotGraphTool`
  ignores it entirely (`__init__.py:302`: `tool = DotGraphTool()` with no config argument)
- **Version metadata** — the mount return value `{"version": "0.4.0"}` is returned to the loader
  but there is no evidence it is consumed for any purpose (logging, capability listing, upgrade detection)

---

## Boundary 2: coordinator Tool Slot → threading.py (The Isolation Boundary)

**Files:** `amplifierd/src/amplifierd/threading.py:51-91`, `state/session_manager.py:273-275`, `spawn.py:261`

After `mount()` registers `DotGraphTool` at `coordinator["tools"]["dot_graph"]`, amplifierd
immediately wraps it before the session is usable. This is the **most significant architectural
boundary** for the tool.

### Where wrapping happens

`wrap_tools_for_threading(session)` is called at exactly **two call sites**:

```python
# site 1 — session_manager.py:273-275: after create_session()
await session.initialize()
wrap_tools_for_threading(session)

# site 2 — spawn.py:261: after child_session.initialize()
wrap_tools_for_threading(child_session)
```

Both sites follow the same pattern: `initialize()` first (which triggers `mount()`, loading the
tool), then immediately `wrap_tools_for_threading()` to replace it with `ThreadedToolWrapper`.

### What the wrapper does

`ThreadedToolWrapper` (`threading.py:21-48`) is a transparent proxy. It intercepts only `execute()`:

```python
async def execute(self, input: Any) -> Any:
    tool = object.__getattribute__(self, "_tool")   # get real DotGraphTool
    coro = tool.execute(input)                       # create coroutine on MAIN thread
    return await asyncio.to_thread(asyncio.run, coro) # run in WORKER thread + own loop
```

All other attribute access (`name`, `description`, `input_schema`) passes through to the real
`DotGraphTool` via `__getattr__`. The LLM sees identical metadata.

### Why this boundary matters

After wrapping, `coordinator["tools"]["dot_graph"]` is a `ThreadedToolWrapper`, not a
`DotGraphTool`. Any code that holds a reference to the original `DotGraphTool` before wrapping
would bypass threading isolation. The `tools` dict is mutated in-place (`threading.py:86`), so
existing references to the dict see the updated wrapper — but references to the tool value itself
(already retrieved before wrapping) would not.

### Composition effect: prewarm benefits future sessions

`prewarm()` (`app.py:26-110`) calls `session.initialize()` on a throwaway session. This executes
`mount()` which imports `amplifier_module_tool_dot_graph` into `sys.modules`. Subsequent real
sessions reuse the cached import — they do not re-import the package. The prewarm session's tool
is thrown away, but the import side-effect persists for the process lifetime.

---

## Boundary 3: ThreadedToolWrapper → asyncio.to_thread → Subprocess (The Isolation Chain)

**Files:** `threading.py:29-40`, `validate.py:233-284`, `render.py:89-149`

This is a **three-layer isolation chain**: asyncio call → thread → subprocess. Each layer adds
a new isolation boundary.

### Layer A: asyncio event loop boundary

`asyncio.to_thread(asyncio.run, coro)` creates a worker thread running its own `asyncio.run()`
with a **fresh event loop**. The main event loop continues serving SSE keepalives, new connections,
and approval requests unblocked.

```
Main event loop (uvicorn/asyncio)
  └── await asyncio.to_thread(asyncio.run, coro)   ← suspends HERE
        └── Worker thread: asyncio.run(coro)         ← fresh event loop
              └── DotGraphTool.execute()
```

The coroutine `coro` is **created on the main thread** before being handed to the worker. This
ensures any synchronous pre-flight code in `execute()` runs on the correct thread context before
the worker's event loop takes over.

### Layer B: subprocess boundary (graphviz)

Within the worker's `asyncio.run()`, both `validate.py` and `render.py` call `subprocess.run()`:

```python
# validate.py:262-264 (Layer 3 render quality)
subprocess.run(["dot", "-Tcanon", tmp_path], capture_output=True, timeout=30)

# render.py:105-110
subprocess.run([engine, f"-T{format}", tmp_dot_path, "-o", output_path], timeout=30)
```

Each `subprocess.run()` spawns a graphviz binary as a **child process of the amplifierd daemon**.
The daemon is the parent process; graphviz is the child. The 30-second timeout fires
`subprocess.TimeoutExpired` but does NOT kill the child process automatically.

### Layer C: tempfile coordination

`render.py:89-93` uses `tempfile.mktemp()` (deprecated TOCTOU-prone) to get a temp output path.
The temp path lives only within a single worker thread's execution scope — it is not shared across
concurrent tool calls. The TOCTOU window exists but is bounded to the worker's own temp directory.

### What this chain guarantees

| Guarantee | Mechanism |
|-----------|-----------|
| Blocking subprocess cannot freeze SSE | Worker thread isolates blocking call from main loop |
| Concurrent tool calls are independent | Each call gets its own thread + event loop + temp files |
| Graphviz timeout doesn't crash the daemon | `TimeoutExpired` is caught; ToolResult returns error |
| Main loop stays responsive for approvals | Main loop is suspended at `to_thread` await, not blocked |

### What this chain does NOT guarantee

| Risk | Mechanism |
|------|-----------|
| Zombie graphviz processes on timeout | `TimeoutExpired` does not kill the subprocess; it may continue running |
| `sys.stdout` race under concurrent calls | `redirect_stdout` sets process-global `sys.stdout` in both `validate.py:112` and `analyze.py:133` — multiple concurrent workers could race (see Boundary 6) |
| Thread pool exhaustion | Unlimited concurrent render calls could exhaust the `asyncio.to_thread` thread pool |

---

## Boundary 4: ToolResult → amplifier_core Hooks → EventBus → SSE

**Files:** `state/session_handle.py:117-197`, `state/event_bus.py:105-136`, `routes/events.py:19-57`

After the worker thread completes, `DotGraphTool.execute()` returns a `ToolResult(success, output)`.
This value crosses back to the main event loop through `asyncio.to_thread`, then the kernel fires
its post-tool hooks. Session-handle bridges those hooks to the EventBus.

### The complete crossing sequence

```
Worker thread: ToolResult(success=bool, output=json_str)
  └── asyncio.to_thread resolves with ToolResult
Main loop: amplifier_core kernel receives ToolResult
  └── hooks.emit("tool:post", data)                   ← kernel internal
Main loop: _wire_events() hook fires (session_handle.py:148-157)
  └── event_bus.publish(session_id, "tool:post", data)
Main loop: EventBus.publish() (event_bus.py:105-136)
  └── for each _Subscriber: sub.queue.put_nowait(TransportEvent)
Main loop: subscribe() async generator
  └── yield TransportEvent to _event_generator()
SSE route (routes/events.py:19-57)
  └── format SSE string → StreamingResponse
```

### What transforms at each crossing

| Crossing | Input | Output | Transformation |
|----------|-------|--------|----------------|
| Worker → main loop | `ToolResult(success, json_str)` | kernel hook data dict | kernel unpacks ToolResult |
| hook → EventBus | kernel event dict | `TransportEvent(seq, timestamp, ...)` | sequence number assigned |
| EventBus → queue | `TransportEvent` | `asyncio.Queue.put_nowait()` | if queue full (>10,000), **oldest event dropped** |
| queue → SSE | `TransportEvent` | `"id: N\nevent: tool:post\ndata: {...}"` | JSON serialize + SSE format |

### The drop-oldest risk for large dot-graph outputs

`DotGraphTool.execute()` returns `ToolResult(output=json.dumps(result_dict))`. For some operations:
- `prescan` result can be **very large** (full file inventory of a large repo)
- `analyze` with `cycles` on a dense graph can produce a large annotated DOT string

These are emitted as a single `tool:post` event. The `asyncio.Queue(maxsize=10_000)` limit is
per-subscriber (10,000 events), so a single large tool result rarely triggers a drop. But if a
session is generating many events in parallel (e.g., a multi-agent discovery run with many
sub-sessions all returning tool results simultaneously), the queue could fill and drop events.

### Filter scaffolding at this boundary

`GET /events?filter=...&preset=...` accepts filter parameters but does not apply them
(`event_bus.py:37-38`; `events.py:65-73`). All `tool:post` events from `dot_graph` tool calls
are delivered to every subscriber of the session — there is currently no way to subscribe only
to DOT-graph-related events.

---

## Boundary 5: prescan — Dual Invocation Path (Recipe vs. LLM Tool)

**Files:** `prescan.py:93-157`, discovery-pipeline recipe bash steps

`prescan` has two completely different integration paths to the same underlying function. This is
the most architecturally unusual integration boundary in the `dot-graph` system.

### Path A: LLM Tool Call (amplifierd tool infrastructure)

```
LLM generates: {"name": "dot_graph", "input": {"operation": "prescan", "options": {"repo_path": "..."}}}
  → amplifier_core kernel dispatches to coordinator["tools"]["dot_graph"]
  → ThreadedToolWrapper.execute() → asyncio.to_thread(asyncio.run, coro)
  → DotGraphTool.execute("prescan") → prescan.prescan_repo(repo_path)
  → ToolResult(success, json_str) → hooks → EventBus → SSE
```

Result: returned as a `ToolResult` JSON string, visible to the LLM as tool output.

### Path B: Recipe Bash Step (direct Python import)

```
Recipe YAML bash step:
  sys.path.insert(0, 'modules/tool-dot-graph')
  from amplifier_module_tool_dot_graph import prescan
  result = prescan.prescan_repo(repo_path)
  # writes to output_dir/prescan-result.json
```

Result: JSON written to filesystem, read by the next recipe step as a variable.

### What differs between the paths

| Dimension | Path A (LLM Tool) | Path B (Recipe Bash) |
|-----------|------------------|----------------------|
| Threading | Worker thread + isolated event loop | Recipe step process/context |
| Result delivery | ToolResult → SSE stream | File on disk → next recipe step |
| Error handling | ToolResult(success=False) | Recipe step failure → bash exit code |
| amplifierd involvement | Full threading + hook + EventBus chain | None — bypasses amplifierd entirely |
| pydot/graphviz needed | No (prescan is pure Python) | No |
| Concurrency safety | Thread-isolated | Recipe-sequential |

### Why this dual path matters

The recipe bash step imports `prescan` directly by manipulating `sys.path`. This means the recipe
does NOT go through the `amplifier.modules` entry-point system. If the bundle's module path
changes or the `sys.path.insert` path is wrong, the recipe falls back to a basic `rglob()` file
listing (recipe step code shows explicit fallback with `print('WARNING: prescan module unavailable...')`).

The dual path means **prescan has two clients with incompatible error surfaces**: LLM tool calls
get structured `ToolResult` errors; recipe steps get Python exceptions and bash exit codes.

---

## Boundary 6: validate/analyze → pydot stdout Redirect (Thread Safety)

**Files:** `validate.py:112-119`, `analyze.py:133-136`

Both `validate.py` and `analyze.py` use `contextlib.redirect_stdout(io.StringIO())` to capture
pydot parse errors that pydot emits to `stdout` rather than raising exceptions.

### The boundary problem

`contextlib.redirect_stdout()` works by temporarily setting `sys.stdout` to a new object:

```python
# This is effectively what redirect_stdout does:
old_stdout = sys.stdout
sys.stdout = io.StringIO()
try:
    pydot.graph_from_dot_data(dot_content)  # pydot prints errors here
finally:
    sys.stdout = old_stdout
```

`sys.stdout` is a **process-global attribute** — it is shared across all threads. When two
concurrent `dot_graph` tool calls (each in their own worker thread via `asyncio.to_thread`) both
execute `redirect_stdout` simultaneously, they race on `sys.stdout`:

- Thread A sets `sys.stdout = StringIO_A`
- Thread B sets `sys.stdout = StringIO_B` (overwrites A)
- Thread A reads from `StringIO_A` — but pydot errors went to `StringIO_B`
- Thread A thinks no errors occurred (StringIO_A is empty)

**Impact:** parse errors would leak to the actual `sys.stdout` (daemon's serve.log) instead of
being captured. This is a reliability issue, not a correctness issue — `pydot.graph_from_dot_data()`
still returns `None` on parse failure, so the validation result is still correct (failed). The
lost information is the specific error message.

**Mitigation that exists:** Each tool call runs inside its own `asyncio.to_thread(asyncio.run, coro)`.
CPython's GIL and the thread pool scheduling make true simultaneous execution of the stdout
redirect rare in practice, but not impossible.

---

## Boundary 7: assemble → Filesystem Layout → render.py Nested Call

**Files:** `assemble.py:27-145`, `render.py:25-149`

`assemble` is the only operation that calls another operation (`render.render_dot()`) internally.
This creates a nested dependency within the same DotGraphTool execution context.

### The nested call chain

```
DotGraphTool.execute("assemble")
  → assemble.assemble_hierarchy(manifest, output_dir, render_png=True)
       → render.render_dot(dot_content, ...)    ← nested operation call
            → setup_helper.check_environment()  ← shared setup_helper
            → subprocess.run([graphviz...])
```

Both `assemble` and `render` are dispatched operations of the same `DotGraphTool`. When
`assemble` calls `render.render_dot()` directly (not via `coordinator.execute()`), it bypasses:
- The coordinator dispatch layer
- Any coordinator-level logging or metrics
- Any coordinator-level error wrapping

The nested `render_dot()` call is a **raw function call** inside the same worker thread's
`asyncio.run()` context. If `render_dot()` raises an unhandled exception, `assemble_hierarchy()`
catches it and records it as a non-fatal warning (`assemble.py:124-127`).

### The filesystem layout contract

`assemble` reads from a specific filesystem layout:
```
output_dir/
  subsystems/*.dot     ← discovered by assemble; written by agents/recipes
  overview.dot         ← written by agents/recipes
```

This layout is produced by the discovery pipeline's agent sub-sessions. The agents write to
`.discovery/modules/{slug}/agents/{type}/diagram.dot`. The assemble operation expects agents
to have also written subsystem-level DOTs to a `subsystems/` directory — but **no agent in the
current discovery pipeline writes to `subsystems/`**. `assemble_hierarchy()` returns
`subsystem_paths = {}` with zero subsystems discovered, which is a valid-but-empty state.

The overview DOT (`output_dir/overview.dot`) is expected to be written by a synthesis agent
before `assemble` is called. If the synthesis step has not run, `assemble` proceeds with
`overview_path = None` and records no overview in `manifest.json`.

---

## Boundary 8: analyze → assemble Output Format (Design Coupling)

**Files:** `analyze.py:172-223`, `assemble.py:33-36`

The `analyze` operation's most complex code — the recursive `_pydot_to_networkx()` walk — exists
specifically to handle the output format produced by `assemble`.

### The coupling

`assemble` writes DOT files where all nodes are inside `cluster_*` subgraphs (one per module).
A naive `pydot.graph_from_dot_data() → networkx.from_pydot()` conversion would see **zero nodes**
because `from_pydot()` only processes top-level nodes, not nodes inside subgraphs.

`analyze.py:147-169` includes an explicit comment explaining this:
```python
# Plain `from_pydot` call only sees top-level nodes and edges.
# Assembled DOT files place everything inside cluster_* blocks —
# yielding 0 nodes without the recursive walk.
```

This is a **format coupling**: one operation's output format drives another operation's internal
implementation. If the assemble format changes (e.g., removing cluster wrapping), the
`_pydot_to_networkx()` recursive walk becomes unnecessary but harmless. If `analyze` is used
on non-assembled DOTs with deep cluster nesting, the recursive walk correctly handles them.

---

## Boundary 9: validate → setup_helper (Shared Dependency with render)

**Files:** `validate.py:233-252`, `render.py:63-83`, `setup_helper.py:12-23`

Both `validate` (Layer 3 render quality check) and `render` call `setup_helper.check_environment()`
before invoking graphviz. This is a shared function that scans for graphviz installation via
`shutil.which("dot")` and `subprocess.run(["dot", "-V"])`.

### The redundancy

When `assemble` is called with `render_png=True` and it internally calls `render.render_dot()`,
the execution path is:

```
assemble → render.render_dot() → check_environment() → shutil.which("dot") + subprocess.run
```

When `validate` runs Layer 3:

```
validate → _check_render_quality() → check_environment() → shutil.which("dot") + subprocess.run
```

Both call `shutil.which("dot")` and `subprocess.run(["dot", "-V"])` on every invocation. There
is no caching of the environment check result. In a session that calls both `validate` and
`render` frequently, graphviz environment detection runs twice per pair of calls.

### Graceful degradation contract

`check_environment()` returns `{installed: False}` if graphviz is not on PATH. Both callers
handle this by returning early with a user-friendly error. This degradation contract is consistent
across both callers — the shared function enforces the same behavior.

---

## Boundary 10: Bundle Agents/Recipes → Sub-Sessions → tool-dot-graph

**Files:** `agents/discovery-code-tracer.md`, `agents/discovery-integration-mapper.md` (bundle cache)

The dot-graph bundle's discovery agents are defined with an explicit tool declaration:

```yaml
# In agent .md files (e.g., discovery-code-tracer.md)
tools:
  - module: tool-dot-graph
```

This means when the discovery recipe dispatches code-tracer, behavior-observer, or
integration-mapper as sub-sessions, each sub-session has `dot_graph` available as a tool.
The agents can use `dot_graph` during their investigation (e.g., to validate DOTs they write,
to analyze graph structure, to run assemble at the end of a module investigation).

### The self-referential integration

This creates a self-referential integration: the `dot-graph` bundle's own agents use the
`dot-graph` tool to do their work. The tool that is being investigated is also the tool that
the investigation agents are equipped with.

Each sub-session goes through the full integration chain independently:
```
recipe dispatches sub-session
  → session created + initialized
  → tool-dot-graph mounted into sub-session's coordinator
  → ThreadedToolWrapper wraps the tool
  → sub-session agent uses dot_graph tool via LLM tool call
  → ToolResult → sub-session's own EventBus → sub-session's SSE stream
  → parent session receives events via EventBus register_child() BFS
```

---

## Composition Effects: Emergent Behaviors at Boundaries

### CE-01: Thread Isolation Protects the SSE Heartbeat

The ThreadedToolWrapper (Boundary 2 → 3) ensures that a 30-second `dot -Tcanon` render in the
validate Layer 3 check does not stall the main event loop. Without this isolation, a single
slow graphviz call would freeze:
- SSE 15-second keepalive heartbeats → clients disconnect
- Approval gate request handling → approvals pile up
- New SSE connection setup → new clients cannot subscribe
- Health/info endpoint serving → health checks fail

The thread isolation is what makes graphviz subprocess calls safe in an async SSE server.

### CE-02: prescan's Dual Path Means Two Different Error Surfaces

When the recipe runs prescan via bash (Boundary 5, Path B), errors produce bash exit codes and
stderr output visible to recipe orchestration. When an LLM runs prescan via tool call (Path A),
errors produce `ToolResult(success=False)` JSON visible to the LLM. A user reading recipe logs
and an LLM responding to tool errors are looking at completely different error representations
of the same underlying failure.

### CE-03: analyze's Recursive Walk Exists Only Because of assemble

The `_pydot_to_networkx()` recursive cluster walk in `analyze.py` is dead code for any DOT
file without deep subgraph nesting. It is alive specifically because `assemble` produces
cluster-heavy DOTs. This coupling is architectural: the output format of one operation defines
the implementation requirement of another (Boundary 8).

### CE-04: assemble's render_png Bypasses Threading Isolation

When `assemble` calls `render.render_dot()` internally with `render_png=True`, the `subprocess.run`
for each PNG render happens INSIDE the same worker thread that is already running `assemble`.
This is correct (the worker thread can call blocking subprocess.run). But it means `assemble`
with `render_png=True` on a large discovery output (many module DOTs) will hold the worker
thread for an extended period — potentially blocking the thread pool slot that could serve
other tool calls.

### CE-05: Version Mismatch Creates Dual-Source Truth

`pyproject.toml` version `0.1.0` and `mount()` metadata `"0.4.0"` are independent hardcoded
strings. Any consumer of the mount metadata (if one existed) would see a different version than
pip's package metadata. Since the mount return value appears to be unused after logging, this is
currently harmless. If a version-routing or upgrade-detection system is added, both strings must
be updated together.

### CE-06: Bundle Discovery Pipeline is the Primary Consumer of All Six Operations

The discovery pipeline recipe uses all six `dot_graph` operations:
- **prescan** → structural scan (Boundary 5, direct import)
- **validate** → DOT quality checking by sub-session agents
- **analyze** → graph structure analysis by synthesis agents
- **render** → PNG rendering via assemble or directly
- **assemble** → hierarchical DOT assembly at end of deep pipeline
- **setup** → environment verification before pipeline starts

The tool was clearly designed with the discovery pipeline as its primary client. The operations
are not arbitrary — they form a coherent pipeline: scan → investigate → diagram → validate →
analyze → assemble.

---

## Cross-Cutting Concerns

### 1. graphviz system dependency crosses every subprocess boundary

`validate` (Layer 3), `render`, and `assemble` (via render) all share an implicit dependency on
the graphviz binary being on PATH. This dependency is:
- **Not declared** in `pyproject.toml` (only `pydot` and `networkx` are listed)
- **Checked at runtime** via `setup_helper.check_environment()`
- **Gracefully degraded** — operations return errors rather than raising exceptions
- **Potentially missing** — graphviz is a system package, not a Python package

A deployment without graphviz would see:
- `validate` Layer 3: skipped gracefully (info-level issue reported)
- `render`: returns `{success: False, error: "Graphviz not installed..."}`
- `setup`: returns `{graphviz: {installed: false}}`
- `assemble` with `render_png=True`: non-fatal warnings, no PNGs produced

### 2. Temp file cleanup is operation-local

Each operation that creates temp files manages its own cleanup in `finally` blocks. There is no
daemon-level temp file registry. If the amplifierd process is killed during a graphviz subprocess
call, the temp files in `/tmp/` are orphaned. Long-running daemons accumulate orphaned temp files.

### 3. All output is JSON-over-string — no streaming

`ToolResult(output=json.dumps(result_dict))` serializes the entire result into a single JSON
string. For large prescan results or large annotated DOT strings from `analyze`, this is a
monolithic payload. There is no streaming partial result delivery. The entire operation must
complete before any result bytes cross the worker→main event loop boundary.

---

## Integration Topology Summary

```
                    ┌─────────────────────────────────────────────────┐
                    │  amplifier_core kernel                           │
                    │  coordinator.hooks / orchestrator                │
                    └──────────────────┬──────────────────────────────┘
                                       │ hooks.emit("tool:post")
                                       ▼
┌──────────────────────────────────────────────────────────────────────────┐
│  amplifierd daemon                                                        │
│                                                                           │
│  Bundle Cache ──(entry point)──→ loader ──→ coordinator["tools"]["dot_graph"]
│                                                     │                     │
│                                         wrap_tools_for_threading()        │
│                                                     │                     │
│                                         ThreadedToolWrapper               │
│                                                     │                     │
│                               asyncio.to_thread(asyncio.run, coro)        │
│                                    ┌────────────────┴──────────────┐      │
│  EventBus ◄── _wire_events() ◄─── │    Worker Thread / asyncio.run │      │
│  ┌─────────┐       session_handle  │    DotGraphTool.execute()      │      │
│  │SSE route│ ◄── subscribe() ◄───  │    ├── validate ──→ subprocess │      │
│  └─────────┘                       │    ├── render  ──→ subprocess  │      │
│                                    │    ├── setup   ──→ shutil.which│      │
│  Recipe (bash steps)               │    ├── analyze ──→ pydot+NX   │      │
│  ├── prescan: direct import ──────►│    ├── prescan ──→ os.walk()  │      │
│  ├── topic selection: LLM agent    │    └── assemble──→ render.py  │      │
│  └── synthesis: LLM agent          └────────────────────────────────┘      │
│       (uses dot_graph tool via                                            │
│        full LLM tool call path)                                           │
└──────────────────────────────────────────────────────────────────────────┘
                                       │ graphviz subprocess
                                       ▼
                    ┌─────────────────────────────────────────────────┐
                    │  External: graphviz binary (system dep)          │
                    │  dot / neato / fdp / sfdp / twopi / circo        │
                    └─────────────────────────────────────────────────┘
```

---

## Key Integration Invariants

| Invariant | Boundary | Location |
|-----------|----------|----------|
| Tool is always wrapped before session is usable | B2 | `session_manager.py:273`, `spawn.py:261` |
| Each tool call gets its own event loop | B3 | `threading.py:40` |
| prescan has two integration paths with different error surfaces | B5 | recipe bash + LLM tool call |
| `sys.stdout` redirect is process-global — concurrent calls can race | B6 | `validate.py:112`, `analyze.py:133` |
| assemble's nested render call bypasses coordinator dispatch | B7 | `assemble.py:114-130` |
| analyze's recursive walk exists to handle assemble's cluster format | B8 | `analyze.py:147-169` |
| graphviz is a system dependency not declared in pyproject.toml | Cross-cutting | all subprocess callers |
| All ToolResult output is monolithic JSON — no streaming | B4 | `DotGraphTool.execute()` return |
| Discovery agents that run this tool are themselves run WITH this tool | B10 | agent .md files in bundle |
| Version mismatch (0.1.0 vs 0.4.0) must be bumped in two places | B1 | `pyproject.toml`, `__init__.py:306` |

---

## Additional Integration Findings (second-pass)

### Finding A — Three wrap_tools_for_threading Call Sites, Third Site Omitted

The invariant "Tool is always wrapped before session is usable" claims two call sites, but there
are **three independent paths** that must each call `wrap_tools_for_threading()`:

| Call site | Location | Condition |
|-----------|----------|-----------|
| New session | `session_manager.py:275` | After `prepared.create_session()` |
| Resumed session | `session_manager.py:427` | After `prepared.create_session(is_resumed=True)` |
| Child session | `spawn.py:261` (step 8b) | After `child_session.initialize()` |

The invariant table lists only `session_manager.py:273` and `spawn.py:261` — missing the
**resume path at `session_manager.py:427`**. All three paths are independent; a miss in any
one means that session's tools block the main event loop on every subprocess call.

**Critical ordering constraint (all three paths):**
`wrap_tools_for_threading()` MUST be called AFTER module loading completes
(`create_session()` / `initialize()`). If called before, `coordinator.get("tools")` returns
empty or None and the wrapping silently no-ops. Tools would then run unprotected on the main
event loop.

**Double-wrap risk:** `ThreadedToolWrapper` has no idempotency guard. If called twice,
`ThreadedToolWrapper(ThreadedToolWrapper(tool))` is created. The outer wrapper creates a
coroutine that — inside a worker thread — calls `asyncio.to_thread(asyncio.run, inner_coro)`.
This nests two `asyncio.run()` calls in the same thread, which raises
`RuntimeError: This event loop is already running` on Python ≥ 3.10.

---

### Finding B — Thread Pool Starvation Under Concurrent Graphviz Renders

`dot_graph render` and `dot_graph validate` (Layer 3) each call `subprocess.run(["dot", ...],
timeout=30)`. These run inside `asyncio.to_thread()` workers, which use Python's default
`ThreadPoolExecutor` (size = `min(32, cpu_count + 4)`).

On a 4-core machine: **maximum 8 concurrent workers**.

If 8 simultaneous `dot_graph render` calls are in-flight:
- All 8 worker slots are occupied for up to 30 seconds each
- Any 9th tool call (from any session, any tool) queues behind them
- SSE keepalive heartbeats (sent every 15s) are unaffected (they run on the main loop)
- But `POST /{session_id}/execute/stream` responses that involve tool calls will stall

`DotGraphTool` has no per-operation concurrency limit — the thread pool is the only global
rate limiter. `assemble` with `render_png=True` on a large discovery output can occupy a
single worker for the sum of all per-module render times (potentially minutes).

---

### Finding C — EventBus._children Memory Leak on Child Session Destroy

When a child session is destroyed after capability delegation (spawn.py:391–392):
```python
# spawn.py:391–392
await session_manager.destroy(child_session.session_id)
```

`SessionManager.destroy()` → `handle.cleanup()` removes the session from `_sessions`
and clears the `SessionIndex`. However, `event_bus.unregister_child(parent_id, child_id)`
is **never called**.

`EventBus._children[parent_id]` continues to hold the destroyed child's ID. The
`get_descendants()` BFS traversal visits all of them on every `publish()` call:

```python
def get_descendants(self, session_id: str) -> set[str]:
    visited: set[str] = set()
    queue: deque[str] = deque()
    for child in self._children.get(session_id, ()):   # stale IDs here
        queue.append(child)
    while queue: ...   # BFS traverses stale IDs every time
```

For a long-lived parent session (e.g. a recipe orchestrator that spawns 50 sub-agents),
`_children[parent_id]` grows to 50 entries and is never pruned. On each `publish()` call
for that parent, the BFS visits all 50 (now dead) child IDs before determining no subscriber
matches any of them.

`unregister_child()` exists (`event_bus.py:76`) but the call path from the child's lifecycle
end is missing. See **U-03** in `unknowns.md` for prior documentation; this finding adds the
specific file:line evidence from the spawn destroy path.
