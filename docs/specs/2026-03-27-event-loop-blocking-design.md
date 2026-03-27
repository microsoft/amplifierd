# Design: Fix Event Loop Blocking in amplifierd

## Problem

amplifierd runs on a single asyncio event loop. Tool execution blocks this loop
because tools do synchronous I/O (disk reads, subprocess calls) inside their
`async def execute()` methods. When any session runs a tool, SSE event delivery
and HTTP handling freeze for ALL sessions.

This is the root cause of multi-session degradation -- not the EventBus.

## Scope

Five fixes, all following the `asyncio.to_thread()` pattern:

| # | Fix | Call Sites | Severity |
|---|-----|-----------|----------|
| 1 | ThreadedToolWrapper for tool.execute() | 3 (create, resume, spawn) | Critical |
| 2 | Wrap session_index.save() | 2 (register, destroy) | High |
| 3 | Wrap write_metadata() in PATCH routes | 2 (patch_session, update_metadata) | Medium |
| 4 | Wrap load_provider_config() | 2 (create, resume) | Medium |
| 5 | asyncio.Lock on SessionHandle | 1 (execute method) | Low |

### Out of scope

- Cold-path blocking (lifespan startup: keys.env, session meta, index load)
- spawn.py mkdir (single syscall, negligible)
- Orchestrator or amplifier-core changes
- Per-session process isolation

## Fix 1: ThreadedToolWrapper

### Mechanism

A transparent proxy that wraps Tool objects and redirects `execute()` to
a thread pool worker. The orchestrator doesn't know it's wrapped -- it calls
`await tool.execute()` as normal, but the wrapper sends the real work off
the event loop.

```python
import asyncio
from typing import Any


class ThreadedToolWrapper:
    """Wraps a Tool to run execute() off the event loop."""

    def __init__(self, tool: Any) -> None:
        self._tool = tool

    async def execute(self, input: dict[str, Any]) -> Any:
        coro = self._tool.execute(input)
        return await asyncio.to_thread(asyncio.run, coro)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._tool, name)
```

### Why the double-loop pattern

`tool.execute()` is `async def` but does sync I/O inside. `asyncio.to_thread()`
requires a sync callable. So we bridge: create the coroutine eagerly on the main
thread (capturing `input`), then send `asyncio.run` + the coroutine to a thread
pool worker. `asyncio.run()` creates a fresh event loop in that thread.

This is the same pattern used in `app.py:66` for `bundle.prepare()`.

### Integration point

A standalone function applied after session initialization in three places:

```python
def wrap_tools_for_threading(session: Any) -> None:
    """Replace tools in coordinator with threaded wrappers."""
    tools = session.coordinator.get("tools")
    if not tools:
        return
    for name, tool in list(tools.items()):
        tools[name] = ThreadedToolWrapper(tool)
```

Called from:
1. `session_manager.create()` -- after session initialization
2. `session_manager.resume()` -- after session initialization
3. `spawn.py:_spawn_with_event_forwarding()` -- after child session initialization

### Why this layer

The blocking call lives in the orchestrator module (`loop-streaming`), but fixing
it there would require changing a separate repo AND every orchestrator module.
The daemon layer fix is:
- Contained to one repo (amplifierd)
- Transparent to the orchestrator
- Applied once via the coordinator's mutable tools dict

### What stays on the main event loop

- SSE event delivery (EventBus subscribe/publish)
- HTTP request handling
- Hook emissions (tool:pre, tool:post) -- these fire from the orchestrator
  BEFORE and AFTER the `await tool.execute()`, so they remain on the main loop
- Provider API calls (LLM requests)
- Session management

### What moves off the event loop

- The actual tool.execute() call (file I/O, subprocess, etc.)

### Known tradeoffs

1. **Cancellation degrades to flag-based only.** `asyncio.CancelledError` can't
   reach into threads. The orchestrator's existing `coordinator.cancellation.is_cancelled`
   flag-based path still works. Acceptable.

2. **Thread pool pressure.** Default pool is ~32 workers. Deep delegation chains
   consume threads (one per level). Delegation rarely exceeds 3 levels. Low risk.

3. **Fresh event loop per tool call.** `asyncio.run()` creates/destroys a loop each
   time. No leak -- same pattern as `bundle.prepare()`. Tools don't depend on loop
   identity.

## Fix 2: Wrap session_index.save()

`SessionIndex.save()` does `mkdir` + `tmp.write_text(json.dumps(...))` +
`os.rename()`. Called synchronously from:

- `session_manager.register()` (line ~172) -- every session create/resume/spawn
- `session_manager.destroy()` (line ~480) -- every session teardown

Fix: wrap the call at each site.

```python
# Before:
self._index.save()

# After:
await asyncio.to_thread(self._index.save)
```

Note: `register()` is currently a sync method. It will need to become `async def`
or the `to_thread` call needs to be fire-and-forget via `asyncio.create_task`.
Since `register()` is called from async contexts (create, resume, spawn), making
it async is the cleaner path.

## Fix 3: Wrap write_metadata() in PATCH routes

`write_metadata()` does: `exists()` + `read_text()` + JSON parse + atomic write.
Called unwrapped from two PATCH routes:

- `routes/sessions.py:~227` (patch_session)
- `routes/sessions.py:~779` (update_metadata)

Fix:

```python
# Before:
write_metadata(session_dir, metadata_updates)

# After:
await asyncio.to_thread(write_metadata, session_dir, metadata_updates)
```

This matches the pattern already used in `TranscriptSaveHook` and
`MetadataSaveHook` in `persistence.py`.

## Fix 4: Wrap load_provider_config()

`load_provider_config()` does `yaml.safe_load(settings_path.read_text())`.
Called from:

- `session_manager.create()` (line ~262)
- `session_manager.resume()` (line ~408)

Fix:

```python
# Before:
providers = load_provider_config()

# After:
providers = await asyncio.to_thread(load_provider_config)
```

## Fix 5: asyncio.Lock on SessionHandle

`SessionHandle.execute()` uses a status flag (EXECUTING/IDLE) to prevent
concurrent execution. This is a TOCTOU race -- two concurrent calls could
both pass the guard.

Fix: replace with `asyncio.Lock`.

```python
class SessionHandle:
    def __init__(self, ...):
        ...
        self._execute_lock = asyncio.Lock()

    async def execute(self, prompt: str) -> Any:
        if self._execute_lock.locked():
            raise HTTPException(409, "Session is already executing")
        async with self._execute_lock:
            self._status = SessionStatus.EXECUTING
            try:
                result = await self._session.execute(prompt)
                return result
            finally:
                self._status = SessionStatus.IDLE
```

The lock is `asyncio.Lock` (not `threading.Lock`) because `execute()` is
an async method called from the event loop. The status flag is kept for
observability (other code reads it), but the lock provides the actual
serialization.

## Testing Strategy

1. **Unit tests for ThreadedToolWrapper**: Mock tool with sync sleep inside
   execute(). Verify the event loop isn't blocked during execution.

2. **Unit tests for I/O wrapping**: Verify session_index.save(),
   write_metadata(), load_provider_config() are called via to_thread.

3. **Regression**: Run full test suite (556 tests). No regressions.

4. **Integration**: Manual test with multiple browser tabs -- verify one
   session's tool execution doesn't freeze another session's SSE stream.

## File Changes

| File | Change |
|------|--------|
| `src/amplifierd/threading.py` | NEW: ThreadedToolWrapper + wrap_tools_for_threading() |
| `src/amplifierd/state/session_manager.py` | Wrap index.save(), load_provider_config(). Make register() async. |
| `src/amplifierd/state/session_handle.py` | Add asyncio.Lock for execute serialization |
| `src/amplifierd/spawn.py` | Call wrap_tools_for_threading() after child init |
| `src/amplifierd/routes/sessions.py` | Wrap write_metadata() calls |
| `tests/test_threading.py` | NEW: Tests for ThreadedToolWrapper |

## Risk Assessment

- **Low risk**: Fixes 2-4 are simple `asyncio.to_thread()` wraps on known-safe
  sync functions. Same pattern used throughout the codebase.
- **Medium risk**: Fix 1 (ThreadedToolWrapper) is a new pattern. Mitigated by
  precedent (`app.py` bundle.prepare), transparent proxy design, and the fact
  that the orchestrator uses duck typing.
- **Low risk**: Fix 5 (asyncio.Lock) is a hardening, not a behavior change.
