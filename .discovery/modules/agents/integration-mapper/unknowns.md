# Unknowns — Integration Mapper
## dot-graph Cross-Boundary Open Questions

> These are unresolved questions that arise specifically at **integration boundaries** —
> places where the `dot-graph` tool meets an adjacent system and the contract between
> them is unclear, potentially unsafe, or architecturally ambiguous.
>
> Single-operation unknowns (e.g., pydot pseudo-node completeness, prescan module
> attribution) are in `code-tracer/unknowns.md`. This document focuses on what happens
> **between** components.

---

## U-IM-01: mount() Return Value — Is It Consumed?

**Boundary:** amplifier_core Loader ↔ Tool Registration

**What we know:**
- `mount()` in `__init__.py:286-312` returns `{"name": "tool-dot-graph", "version": "0.4.0", "provides": ["dot_graph"]}`.
- The loader returns `mount_with_config(coordinator)` — a closure that calls `mount()` — to its caller (`loader.py:200-206`).
- There is no evidence that the caller of `loader.load()` consumes the return value of the `mount_with_config(coordinator)` call.

**The boundary question:**
The mount return value is defined, documented, and typed — it clearly represents capability metadata. But if nothing reads it, the `provides: ["dot_graph"]` field is dead metadata. The version mismatch (`0.4.0` vs. `0.1.0` in `pyproject.toml`) would matter if any system uses this for upgrade detection, deduplication, or capability listing.

**Open questions:**
- Does `amplifier_core`'s coordinator or session registry consume the mount return dict for any purpose?
- If a future system adds capability routing (e.g., only load sessions that need `dot_graph`), would it read `provides`?
- Who is responsible for bumping `"0.4.0"` when the tool changes? If no system reads it, the mismatch is harmless. If a system begins reading it, the stale value is a silent misconfiguration.

**Risk:** LOW now — version mismatch is a maintenance hazard if capability metadata is ever consumed. MEDIUM if capability routing is added.

---

## U-IM-02: sys.stdout Redirect Under Concurrent Tool Calls

**Boundary:** Worker Thread Isolation ↔ pydot Parse (validate.py, analyze.py)

**What we know:**
- `validate.py:112-119` and `analyze.py:133-136` use `contextlib.redirect_stdout(io.StringIO())`.
- `redirect_stdout` temporarily sets `sys.stdout` — a **process-global** attribute — to a `StringIO` instance.
- Each `dot_graph` tool call runs in its own worker thread via `asyncio.to_thread(asyncio.run, coro)`.
- Multiple concurrent tool calls (e.g., multiple agents in a discovery run simultaneously calling `validate` or `analyze`) run in separate threads.

**The boundary question:**
Thread A sets `sys.stdout = StringIO_A`. Thread B sets `sys.stdout = StringIO_B`. Thread A's pydot parse error goes to `StringIO_B`. Thread A's `captured.getvalue()` returns `""` — the error is lost. The validation result is still correct (pydot returns `None` for failed parses), but the error message is lost. Error messages may appear in the daemon's actual `serve.log` instead.

**Open questions:**
- Does CPython's GIL make this race rare but not impossible? (Yes — threads CAN interleave at `sys.stdout = ...` boundaries, but GIL means C-extension code runs atomically.)
- Would replacing `redirect_stdout` with `contextlib.redirect_stdout` + thread-local stdout be correct? Python does not have a built-in thread-local stdout mechanism.
- The practical workaround is `io.StringIO` capture via `pydot`'s internal parse state, not stdout. Is there a pydot API for capturing errors without stdout redirect?

**Risk:** LOW-MEDIUM — functionally correct (parse failure is still detected), but parse error messages are unreliable under concurrency. The specific error text is useful for debugging.

---

## U-IM-03: assemble's Nested render.render_dot() — Thread Pool Impact

**Boundary:** assemble Operation ↔ render.py (Nested Call)

**What we know:**
- `assemble.py:114-130`: if `render_png=True`, calls `render.render_dot()` for each discovered `.dot` file.
- Each `render.render_dot()` call invokes `subprocess.run([engine, ...], timeout=30)`.
- This entire chain runs INSIDE the same worker thread that is executing `DotGraphTool.execute("assemble")`.
- A discovery run may discover many `.dot` files (one per module × 3 agents = 12+ files for a 4-module repo).

**The boundary question:**
The outer `assemble` call holds a thread-pool slot for the duration of ALL nested render calls. For a 12-module repo with `render_png=True`, this could be 36+ subprocess.run calls sequentially in a single thread, each blocking for up to 30 seconds each = up to 18 minutes holding a single thread-pool slot. No other `dot_graph` tool call can use that slot.

**Open questions:**
- What is the default thread pool size for `asyncio.to_thread` in the Python version used? (Python 3.12+: `min(32, os.cpu_count() + 4)`. For a 4-core machine: 8 threads.)
- If a discovery run starts 12 concurrent agent sub-sessions that each call `assemble` with `render_png=True`, could all 8 thread-pool slots be held by blocking render calls?
- Would batch-limiting the PNG renders (e.g., render at most N files per `assemble` call) be a safe optimization?

**Risk:** MEDIUM — thread pool exhaustion on large repos with `render_png=True` during concurrent multi-agent discovery runs.

---

## U-IM-04: prescan Direct Import Path vs. Tool Path — Behavioral Divergence

**Boundary:** Recipe Bash Step ↔ Tool LLM Call (Boundary 5, Dual prescan Path)

**What we know:**
- Recipe bash step: `sys.path.insert(0, 'modules/tool-dot-graph'); from amplifier_module_tool_dot_graph import prescan; prescan.prescan_repo(repo_path)`
- LLM tool call: `{"operation": "prescan", "options": {"repo_path": "..."}}` → `ThreadedToolWrapper.execute()` → `DotGraphTool.execute()` → `prescan.prescan_repo(repo_path)`
- The recipe bash step runs in the same working directory as the recipe executor, which may differ from the daemon's working directory.
- The LLM tool call runs in the worker thread's context, with no working directory manipulation.

**The boundary question:**
`prescan.prescan_repo(repo_path)` takes `repo_path` as an argument (it does NOT use `os.getcwd()`), so working directory differences should not matter. BUT: `sys.path.insert(0, 'modules/tool-dot-graph')` in the recipe bash step is a relative path. If the recipe executor's working directory is not the bundle cache root, this import will fail and fall back to the `rglob()` fallback.

**Open questions:**
- What is the working directory of the recipe bash step executor? Is it set to the bundle cache directory before bash steps run?
- If the `sys.path.insert` import fails, the fallback uses `rglob()` which: (a) includes hidden files (unlike prescan's `_SKIP_DIRS`), (b) has no language detection, (c) produces no module hierarchy. Topic selection LLM then works from degraded data. Is there a signal to the LLM that prescan failed and it should be more conservative?
- Does the recipe step's `WARNING: prescan module unavailable` stderr output appear in the session's SSE stream?

**Risk:** MEDIUM — if the recipe's working directory is wrong, topic selection runs on incomplete repo data, producing lower-quality module identification without any explicit failure signal to the user.

---

## U-IM-05: assemble subsystems/ Contract — Missing Writer

**Boundary:** assemble Operation ↔ Agent/Recipe Output Filesystem Layout

**What we know:**
- `assemble.py:93-97`: discovers `subsystems/*.dot` by `glob("subsystems/*.dot")` in `output_dir`.
- `assemble.py:33-36` explicitly states: "subsystems/ is reserved for true subsystem-aggregate DOTs explicitly written by agents or recipes."
- No current agent in the discovery pipeline (code-tracer, behavior-observer, integration-mapper, synthesizer) writes to `subsystems/`.
- `assemble_hierarchy()` returns `subsystem_paths = {}` with zero subsystems when no files are found.

**The boundary question:**
The assemble API expects a populated `subsystems/` directory, but no code writes to it. The "explicitly written by agents or recipes" note implies a future agent or a higher-level synthesis step is responsible. Without subsystem DOTs, `manifest.json` records an empty subsystems section, and the hierarchy is flat (overview + modules only).

**Open questions:**
- Is the `subsystems/` directory populated by a wave-2 investigation or a future recipe step that is not yet implemented?
- Is the `discovery-subsystem-synthesizer` agent (seen in the bundle's agents/ listing) responsible for writing to `subsystems/`? If so, what triggers it and where does it write?
- Is the `discovery-overview-synthesizer` responsible for writing `overview.dot` (which `assemble` also reads)? When in the recipe pipeline is it dispatched?
- If `assemble` is called before the overview/subsystem writers run, `manifest.json` records null for overview and zero for subsystems — is this idempotent? Can `assemble` be called again after those files are written, and will it correctly detect them?

**Risk:** LOW functionally (assemble degrades gracefully) — HIGH architecturally (the subsystems layer of the DOT hierarchy may be permanently empty, making `assemble` partially pointless for the current pipeline).

---

## U-IM-06: Zombie Graphviz Processes on Timeout

**Boundary:** Worker Thread ↔ subprocess.run() (30-second timeout)

**What we know:**
- `validate.py:262-264` and `render.py:105-110` use `subprocess.run(..., timeout=30)`.
- `subprocess.TimeoutExpired` is caught and returns a clean error `ToolResult`.
- Python `subprocess.run()` with `timeout` raises `TimeoutExpired` but does NOT call `process.kill()` automatically before Python 3.12. Starting Python 3.12, `subprocess.run()` kills the child on timeout. Behavior depends on Python version.

**The boundary question:**
On Python < 3.12, a timed-out graphviz process is orphaned — it continues running in the OS process table. amplifierd's shutdown path (`session_manager.py:493` → `handle.cleanup()`) does not track subprocess PIDs, so these zombies are never explicitly killed. On a long-running daemon with frequent render timeout events, zombie graphviz processes accumulate.

**Open questions:**
- What Python version does the production amplifierd deployment target? (pyproject.toml declares `python = ">=3.12"` — if true, `subprocess.run()` with timeout DOES kill the child on Python 3.12+.)
- If Python 3.12+ is guaranteed, is this risk resolved? Or is there still a window between `TimeoutExpired` and the OS sending SIGKILL?
- Does the graphviz binary (`dot`) spawn its own child processes for complex renders? If so, a SIGKILL to the `dot` parent may leave grandchildren running.

**Risk:** LOW on Python 3.12+ (subprocess.run kills on timeout). MEDIUM on Python < 3.12.

---

## U-IM-07: Tool Config Ignored — Future Config Contract

**Boundary:** amplifier_core Loader ↔ DotGraphTool (config parameter)

**What we know:**
- `mount(coordinator, config)` receives a `config` dict from the loader.
- `DotGraphTool()` in `__init__.py:302` is created with zero arguments — `config` is completely ignored.
- No operation checks `config` for any settings (e.g., timeout overrides, skip_dirs customization, path aliases).

**The boundary question:**
The `mount(coordinator, config)` signature accepts configuration, but the tool ignores it. If a future deployment needs to customize behavior (e.g., `config = {"timeout": 60, "max_tree_depth": 6}`), the config silently has no effect. There is no validation, warning, or error when config is provided but ignored.

**Open questions:**
- Is ignoring config intentional? The `tool-dot-graph` is designed to be a general-purpose tool with hardcoded defaults — perhaps config customization is out of scope.
- If config is intended to be supported in the future, which settings would be most valuable? `prescan._SKIP_DIRS`, `validate` timeout, `render` engine whitelist, `analyze._PATH_CAP`?
- Should `mount()` log a warning if `config` is non-empty to alert operators that config is being ignored?

**Risk:** LOW now — MEDIUM if operators attempt to configure the tool and see no effect.

---

## U-IM-08: Self-Referential Agent Integration — Circular Discovery Risk

**Boundary:** Bundle Agents ↔ dot-graph Tool (they use the tool they investigate)

**What we know:**
- Discovery agents (`code-tracer`, `integration-mapper`, etc.) declare `tools: [{module: tool-dot-graph}]`.
- This means each agent sub-session loads and uses `dot_graph` to validate and analyze its own DOT output.
- If a discovery run is investigating the `amplifierd` repository (which contains the `dot-graph` bundle), the agents are investigating a system that contains the agents themselves.

**The boundary question:**
This is not a correctness problem under normal operation, but raises questions about self-referential scenarios:
- If the `dot-graph` bundle itself is the target of investigation (as in this session), the integration-mapper is using the `dot_graph` tool to write artifacts about the `dot_graph` tool.
- If `prescan` is called on the amplifierd repository, it discovers the bundle's modules, including `tool-dot-graph` itself, creating a recursive inventory.

**Open questions:**
- Can an investigation recipe enter a problematic recursive loop if it investigates a repository that contains the same recipe definitions it is running?
- If the `discover.yaml` recipe is run ON the bundle cache directory itself, does it correctly handle the case where it discovers its own recipe files?
- Is there any guard in the prescan or topic selection step to avoid treating the `.discovery/` output directory as a module to investigate?

**Risk:** LOW — primarily a philosophical/design question. The `.discovery/` directory IS in `_SKIP_DIRS`? (Actually, `prescan._SKIP_DIRS` does not include `.discovery` — only `.git`, `node_modules`, `__pycache__`, `.venv`, etc. If the repo root is being scanned, `.discovery/` would be included in the inventory, potentially creating topic suggestions about the discovery output itself.)

---

## Discrepancy D-IM-01: prescan in Recipe Bypasses Thread Isolation

**Between:** code-tracer (threading isolation finding) and integration-mapper (dual prescan path)

**The code-tracer states** (findings.md §5):
> `wrap_tools_for_threading()` is called after `session.initialize()` — tool calls are isolated in worker threads via `asyncio.to_thread(asyncio.run, coro)`.

**Integration mapper finding:**
The recipe bash step's direct import of `prescan` bypasses `ThreadedToolWrapper` entirely. When the recipe bash step calls `prescan.prescan_repo()`, it runs synchronously in the recipe executor's context — NOT in a worker thread, NOT in an isolated event loop.

**Implication:**
The threading isolation guarantee ("blocking operations cannot stall the main event loop") applies only to LLM tool calls routed through `coordinator["tools"]["dot_graph"]`. The recipe's direct import path is exempt — if `prescan_repo()` on a very large repo blocks for many seconds, the recipe executor is blocked, not the amplifierd main loop.

Whether the recipe executor runs on the main event loop, a dedicated asyncio task, or a separate process depends on how amplifierd executes recipe bash steps. This is unknown from available source evidence.

**Status:** OPEN — requires reading the recipe bash step execution path in amplifierd.

---

## Discrepancy D-IM-02: assemble Defaults — Tool vs. Function Level

**Between:** code-tracer (assemble defaults finding) and integration-mapper (assemble nested call)

**The code-tracer states** (findings.md §12):
> `render_png` defaults True in the tool interface (`__init__.py:263`) but False in `assemble_hierarchy()` itself.

**Integration mapper observation:**
When `assemble_hierarchy()` calls `render.render_dot()` internally (nested call), the `render_png` value was already resolved at the tool dispatch layer (`__init__.py:263`). The `False` default in `assemble_hierarchy()` protects tests without graphviz. The `True` default in the tool protects LLM users who expect visual output.

**The discrepancy:**
A future caller of `assemble_hierarchy()` directly (not via the tool) would get `render_png=False` by default. A caller via the tool would get `render_png=True` by default. Two callers of the same function get different defaults depending on whether they use the tool wrapper or call the function directly.

**Status:** KNOWN DESIGN — the comment in `__init__.py:263` documents this intentionally. But it creates a confusion point for anyone adding a new integration path that calls `assemble_hierarchy()` directly.

---

## U-IM-09: Thread Pool Exhaustion Under Concurrent Graphviz Renders

**Boundary:** ThreadedToolWrapper ↔ asyncio ThreadPoolExecutor ↔ `subprocess.run()`

**What we know:**
- `ThreadedToolWrapper.execute()` uses `asyncio.to_thread(asyncio.run, coro)`, which uses Python's
  default `ThreadPoolExecutor` with size `min(32, os.cpu_count() + 4)`.
- `dot_graph render` and `dot_graph validate` (Layer 3) each call `subprocess.run(["dot", ...],
  timeout=30)` — a blocking call that holds the worker thread for up to 30 seconds.
- `dot_graph assemble` with `render_png=True` calls `render_dot()` internally for **each module
  DOT** in the discovery output — potentially many sequential renders in a single tool call, all
  holding the same worker thread.

**The integration risk:**
On a 4-core machine, the default pool has 8 workers. If 8 concurrent `render` or `validate`
sessions are active, the pool is exhausted for up to 30 seconds. Any subsequent tool call from
any session — including tool calls that do NOT invoke graphviz (e.g., `prescan`) — must wait for
a worker to free up. From the main event loop's perspective, the `await asyncio.to_thread()` call
simply stalls.

**Open questions:**
- Is there a concurrency limit per session, per tool, or per executor? Currently none visible.
- What is the realistic maximum number of concurrent sessions in a typical amplifierd deployment?
  If it's 1–2, pool exhaustion is theoretical. If it's 10+, it is operational.
- Could `assemble` with `render_png=True` and a large discovery output (50 modules) hold the
  worker for minutes? The current 30s timeout per `render_dot()` call means the timeout guards
  individual subprocesses but not the cumulative assemble time.
- Should `dot_graph render` have a concurrency semaphore to bound simultaneous graphviz invocations?

**Risk level:** LOW in single-user deployments; MEDIUM in multi-session deployments running
simultaneous render-heavy operations.

---

## U-IM-10: ThreadedToolWrapper Double-Wrap Risk

**Boundary:** Session lifecycle ↔ `wrap_tools_for_threading()` ↔ coordinator tools dict

**What we know:**
- `wrap_tools_for_threading(session)` iterates `coordinator.get("tools")` and replaces each tool
  with `ThreadedToolWrapper(tool)` **in-place** (`threading.py:83–86`).
- There is no idempotency guard — no check whether a tool is already a `ThreadedToolWrapper`.
- The function is called at three independent sites:
  `session_manager.py:275`, `session_manager.py:427`, `spawn.py:261`.
- These three sites are structurally separate — no shared wrapper flag or lock.

**The integration risk:**
If `wrap_tools_for_threading()` were accidentally called twice on the same session (e.g., via a
future code path, a test helper, or a race condition in the session setup), each `DotGraphTool`
would be double-wrapped: `ThreadedToolWrapper(ThreadedToolWrapper(tool))`.

When the outer wrapper's `execute()` is called:
```python
coro = inner_wrapper.execute(input)          # creates coroutine on main thread
return await asyncio.to_thread(asyncio.run, coro)  # runs in worker thread
```

Inside the worker thread's `asyncio.run(coro)`:
```python
# inner_wrapper.execute() is:
coro2 = tool.execute(input)
return await asyncio.to_thread(asyncio.run, coro2)   # ← nested asyncio.to_thread!
```

`asyncio.to_thread()` requires a running event loop. Inside `asyncio.run(coro)`, an event loop
IS running. But Python's `asyncio.to_thread()` used inside a nested `asyncio.run()` on Python
≥ 3.10 will raise `RuntimeError: There is no current event loop in thread <worker>` or similar
errors because the inner `asyncio.run()` creates a new loop context that is not the thread's
loop for `to_thread` resolution.

**Open question:**
- Is the double-wrap scenario actually reachable in the current codebase? The three call sites
  appear to be mutually exclusive (create, resume, and child spawning don't overlap for the same
  session). However, if a future integration test or plugin re-calls `wrap_tools_for_threading()`
  for inspection purposes, it would silently create broken sessions.
- Should `wrap_tools_for_threading()` add an idempotency check (e.g.,
  `isinstance(tools[key], ThreadedToolWrapper)`)?

**Risk level:** LOW currently (no known double-wrap path); MEDIUM if the function is ever called
from test code or a plugin without checking current tool types.
