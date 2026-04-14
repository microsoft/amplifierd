# Unknowns and Open Questions — dot-graph Code Tracer

> These are paths and mechanisms that could not be fully traced from the source code alone,
> either because they depend on opaque runtime state, external packages, or intentional
> design gaps left for future completion.

---

## 1. Module Source Resolution Path (amplifier_core loader)

**What we know:** `loader.py:218-247` checks for a `module-source-resolver` mounted on the
coordinator. If present, it calls `source_resolver.async_resolve(module_id, source_hint)` to
get a `source` object, then `source.resolve()` to get the actual module path. If no resolver
is mounted, it falls back to `_load_direct(module_id, config)` using entry-point discovery.

**Open questions:**
- What mounts the `module-source-resolver` on the coordinator in a normal session? Which
  component is responsible — the bundle system, the prepared bundle, or the session factory?
- What protocol does `source_resolver.async_resolve()` implement? The loader passes both
  `source_hint` and `profile_hint` (duplicate) due to a `FIXME` comment noting backward
  compatibility (`loader.py:238`). When does the `source_hint` path vs. `profile_hint` path
  matter for `tool-dot-graph`?
- When `tool-dot-graph` is loaded via `source_hint` pointing to the bundle cache directory, how
  does `source.resolve()` translate that to a filesystem path that `importlib` can consume?
- The loader comment at `loader.py:202-211` says "creating fresh closure" for already-loaded
  modules. Is this idempotent? Can `mount()` be called more than once on the same coordinator
  without registering duplicate `dot_graph` tools?

---

## 2. `coordinator.mount("tools", tool, name="dot_graph")` — Protocol

**What we know:** `__init__.py:302` calls `await coordinator.mount("tools", tool, name=tool.name)`.
The coordinator then makes the tool available at `coordinator["tools"]["dot_graph"]`.

**Open questions:**
- What does `coordinator.mount()` do internally? Does it validate the tool against a protocol
  (checking that `name`, `description`, `input_schema`, and `execute()` exist)?
- Does `coordinator.mount()` call `wrap_tools_for_threading()` automatically, or does
  amplifierd's `threading.py` need to be run again after mounting?
- If a second `dot-graph` module version is mounted (e.g. after a bundle update),
  does `coordinator.mount()` replace the existing entry or raise an error?

---

## 3. Version Mismatch: pyproject.toml vs. mount() Metadata

**What we know:** `pyproject.toml` declares `version = "0.1.0"`, but `mount()` at
`__init__.py:306` logs `"(v0.4.0)"` and returns `{"version": "0.4.0"}`.

**Open questions:**
- Which version number is authoritative? The `0.4.0` in the mount metadata is hardcoded as
  a string literal, not derived from `importlib.metadata.version()`.
- Does the amplifier_core loader or session manager consume the returned mount metadata
  `{"version": "0.4.0"}` for any purpose (capability listing, upgrade detection, logging)?
- Is this a known maintenance hazard — a version string that must be bumped in two places
  (`pyproject.toml` AND `__init__.py`) on every release?

---

## 4. `_PSEUDO_NODES` Three-Stage Filter — Completeness

**What we know:** Both `validate.py` and `analyze.py` define `_PSEUDO_NODES = {"node", "edge", "graph"}`
and filter them at multiple stages. In `analyze.py:_pydot_to_networkx()`, the filter is applied:
1. When adding explicit nodes (`analyze.py:206`)
2. When adding edge endpoints (`analyze.py:213-216`)
3. In a final sweep to remove slipped-through pseudo-nodes (`analyze.py:219-221`)

**Open questions:**
- Are there other pydot pseudo-node names not in this set? The comment says these are
  "injected by default style declarations" — but pydot may also inject other identifiers in
  some versions.
- The `validate.py` structural layer also uses `_PSEUDO_NODES` but has its own independent
  constant definition. If pydot gains new pseudo-node names in a future version, both files
  would need to be updated independently. Is there a shared constant these could reference?
- In `_check_structural()`, pseudo-nodes are filtered from `_collect_all_nodes()` at the end,
  but `_collect_edge_endpoint_names()` might still add them to the node set if they appear as
  edge endpoints (e.g. `node -> A`). Is this covered by the final `names -= _PSEUDO_NODES`?

---

## 5. pydot stdout Redirect — Concurrency Safety

**What we know:** Both `validate.py:112-119` and `analyze.py:133-136` use
`contextlib.redirect_stdout(io.StringIO())` to capture pydot parse errors.

**Open questions:**
- `contextlib.redirect_stdout()` sets `sys.stdout` on the current thread. Since `DotGraphTool.execute()`
  runs inside `asyncio.to_thread(asyncio.run, coro)` — a worker thread — is `redirect_stdout`
  thread-safe? Each call gets its own thread and therefore its own `sys.stdout` binding?
  Or does `redirect_stdout` use a process-global `sys.stdout` that could race with other threads?
- If two `dot_graph` tool calls happen concurrently (in different worker threads), could their
  stdout captures interfere?

---

## 6. `tempfile.mktemp()` TOCTOU Window

**What we know:** `render.py:89-93` explicitly uses the deprecated `tempfile.mktemp()` with
an intentional comment: "graphviz requires the output path to not exist before writing, so
`NamedTemporaryFile(delete=False)` + close would leave an empty file that some graphviz
versions refuse to overwrite."

**Open questions:**
- Which graphviz versions exhibit this behavior? Is this still true in modern graphviz
  (≥ 2.50)?
- Could this be worked around with `NamedTemporaryFile(delete=True)` + extract the name +
  close + delete, since close+delete would restore the non-existence condition?
- Under high-concurrency scenarios (many parallel render calls in different threads), what is
  the actual TOCTOU risk in the temp dir? Could two concurrent renders pick the same name?

---

## 7. Subprocess Timeout — Thread Implications

**What we know:** Both `validate.py:262-264` and `render.py:105-110` use `subprocess.run()`
with `timeout=30`. The subprocess runs in a worker thread (via `asyncio.to_thread`).

**Open questions:**
- If the 30-second timeout expires, `subprocess.TimeoutExpired` is caught and a clean error
  is returned. But is the underlying graphviz process killed? `subprocess.run()` with
  `timeout` raises `TimeoutExpired` but does NOT call `process.kill()` automatically in all
  Python versions. Is the zombie graphviz process left running?
- If the daemon shuts down while a graphviz subprocess is running, what happens to the child
  process? amplifierd's shutdown path (`session_manager.py:493`) calls `handle.cleanup()`
  but doesn't track subprocess PIDs.

---

## 8. `assemble.py` — Canonical vs. Subsystem DOT File Contract

**What we know:** `assemble.py:33-36` explicitly states that per-module DOTs remain in their
canonical location (`output/modules/{slug}/diagram.dot`) and the `subsystems/` directory is
reserved for "true subsystem-aggregate DOTs explicitly written by agents or recipes."

**Open questions:**
- What writes subsystem DOT files to `subsystems/`? Is there a specific agent or recipe
  responsible? The `assemble_hierarchy()` function only discovers them — it doesn't create them.
- What is the expected relationship between a subsystem DOT and the module DOTs it
  aggregates? Is the subsystem DOT a merged supergraph, a summary, or an independently
  authored diagram?
- If no agent writes to `subsystems/`, `assemble_hierarchy()` returns `subsystem_paths = {}`
  with zero subsystems discovered. Is this a valid/expected state, or a signal that a
  prior pipeline step failed?
- The overview DOT at `output_dir/overview.dot` is also expected to be agent-produced.
  Which agent is responsible for writing it?

---

## 9. `_annotate_nodes()` / `_annotate_edges()` — Text Injection Correctness

**What we know:** `analyze.py:553-620` implements `_annotate_nodes()` and `_annotate_edges()`
by finding the first `{` in the DOT source and injecting attribute declarations on the
following line.

**Open questions:**
- If the graph attribute block contains `{` inside a string literal (e.g. `label="foo {bar}"`),
  the injection point will be wrong — it inserts after the string's `{`, not the graph's
  opening brace.
- For assembled DOTs that use HTML labels with `<TABLE>` etc., does the `{` detection still
  find the correct insertion point?
- The injected attribute declarations use the bare node name (e.g. `A [color="red"]`). If the
  node name contains special characters or spaces (requiring quotes in the DOT source), will
  the injected line be syntactically valid?
- The `_unreachable` annotations mark nodes red with style `filled`, but not all graph
  stylesheets use `filled` as a base style. Could this annotation conflict with existing
  node styles and produce visually unexpected results?

---

## 10. `prescan_repo` — Module Attribution for Deeply Nested Packages

**What we know:** `prescan.py:267-300` — `_is_file_in_module()` checks whether a file belongs
to a module by ensuring no intermediate directory is itself a module root. This handles
Python monorepos where `src/pkg/__init__.py` and `src/pkg/sub/__init__.py` both exist.

**Open questions:**
- The attribution logic uses `_MODULE_INDICATORS` priority (Cargo.toml first), but
  `_is_file_in_module()` operates on `dir_to_indicator` which already applied the priority
  rule. Is there any edge case where the same directory could appear under two different
  indicator types simultaneously in `dir_to_indicator`?
- If a repo has both `package.json` (node_package) and `__init__.py` (python_package) in the
  same directory (e.g. a Python extension with a JS wrapper), only one is recorded — whichever
  indicator appears first in the priority list. Is this the correct behavior?
- `prescan` does not detect Ruby (`Gemfile`), Java (`pom.xml` inside module dirs), or other
  common module boundary indicators. What is the intended extension path?

---

## 11. `_check_structural` — Cluster Orphan Detection Limitation

**What we know:** `validate.py:192-210` — orphan cluster detection only examines top-level
subgraphs. The docstring comment says: "only top-level subgraphs are examined for cluster
membership. Nested clusters (clusters within clusters) are not tracked — this is intentional
given DOT's rare usage of nested clusters and the absence of a spec requirement for deeper
recursion."

**Open questions:**
- For assembled discovery DOTs that use nested clusters (e.g. a subsystem cluster containing
  module cluster subgraphs), will the orphan cluster check produce false positives by treating
  the outer cluster's nodes as "all cluster nodes" and not finding outside edges?
- Is the "intentional" limitation tracked anywhere as a known false-positive source? If a user
  sees a warning "Cluster X has no edges connecting to outside nodes" on a valid nested-cluster
  DOT, is there documentation explaining the limitation?

---

## 12. amplifierd Threading — `asyncio.to_thread` and Python 3.13 Compatibility

**What we know:** `threading.py:29` uses `await asyncio.to_thread(asyncio.run, coro)` to
run the tool coroutine in a worker thread with its own event loop. This is described in
amplifierd findings as "each tool call gets its own isolated event loop in a thread-pool worker."

**Open questions:**
- In Python 3.13, `asyncio.to_thread` uses the default thread pool executor. Is the thread
  pool size bounded? If 50 concurrent `dot_graph` render calls each block on `subprocess.run()`
  for up to 30 seconds, can the executor exhaust threads?
- `asyncio.run(coro)` inside a thread creates a **new event loop** — but `DotGraphTool.execute()`
  is a coroutine that only performs blocking subprocess calls, not async I/O. Could pydot or
  networkx internals ever create tasks on this inner event loop that outlive the `asyncio.run()`
  call?
- The coroutine `coro = tool.execute(input_data)` is created on the main thread but runs on a
  worker thread's event loop. Are there any Python-level object boundaries that could cause
  issues if the main thread's loop advances between coroutine creation and worker thread
  execution?
