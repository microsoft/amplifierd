# Code-Tracer Findings — dot-graph

> **Scope:** HOW the `dot_graph` tool mechanism works, traced from bundle cache through
> amplifier_core module loading, amplifierd tool threading, and every operation dispatcher.
>
> **Primary source locations:**
> - Bundle: `~/.amplifier/cache/amplifier-bundle-dot-graph-43d42df775a679a7/`
> - Module pkg: `…/modules/tool-dot-graph/amplifier_module_tool_dot_graph/`
> - Daemon threading: `amplifierd/src/amplifierd/threading.py`
> - Loader: `amplifierd/.venv/…/amplifier_core/loader.py`

---

## 1. Bundle Identity and Layout

The `dot-graph` bundle is a first-class Amplifier bundle, cached at:
```
~/.amplifier/cache/amplifier-bundle-dot-graph-43d42df775a679a7/
```

**`bundle.md` (bundle root):**
```yaml
bundle:
  name: dot-graph
  version: 0.2.0
  description: General-purpose DOT/Graphviz infrastructure — knowledge, tools, graph intelligence,
               and codebase discovery
includes:
  - bundle: dot-graph:behaviors/dot-graph
```

The bundle contains:
- `modules/tool-dot-graph/` — the Python tool module (the implementation subject of this trace)
- `agents/` — 13 agent definitions (discovery-orchestrator, code-tracer, integration-mapper, …)
- `skills/` — dot-graph skills (dot-syntax, dot-quality, dot-patterns, dot-graph-intelligence, …)
- `recipes/` — discovery pipeline recipes
- `behaviors/` — behavior definitions
- `tests/` — 30 test files covering all operations

---

## 2. Python Package and Entry Point Registration

**`modules/tool-dot-graph/pyproject.toml`:**

```toml
[project]
name = "amplifier-module-tool-dot-graph"
version = "0.1.0"
dependencies = ["pydot>=3.0", "networkx>=3.0"]

[project.entry-points."amplifier.modules"]
tool-dot-graph = "amplifier_module_tool_dot_graph:mount"
```

The package registers itself under the `amplifier.modules` entry-point group with the name
`tool-dot-graph`. The entry point points directly to the `mount()` coroutine in `__init__.py`.

**Dependencies:**
| Package | Version | Purpose |
|---------|---------|---------|
| `pydot` | ≥ 3.0 | DOT parsing, structural inspection |
| `networkx` | ≥ 3.0 | Graph algorithms (reachability, cycles, paths, DAG, diff) |
| graphviz CLI | (system) | Rendering and render-quality validation |

---

## 3. Module Discovery and Loading — amplifier_core Loader

**`amplifier_core/loader.py:103-136` — `_discover_entry_points()`:**

```python
eps = importlib.metadata.entry_points(group="amplifier.modules")
for ep in eps:
    module_type, mount_point = self._guess_from_naming(ep.name)
    # "tool-dot-graph" → type="tool", mount_point="tools"
    module_info = ModuleInfo(id=ep.name, type=module_type, mount_point=mount_point, …)
```

**`loader.py:176-250` — `load(module_id, config, source_hint, coordinator)`:**

Two resolution paths:
1. **Source-resolver path** (if `module-source-resolver` is mounted on coordinator):
   `source_resolver.async_resolve(module_id, source_hint)` → `source.resolve()` → `module_path`
2. **Direct-discovery fallback** (no resolver):
   `_load_direct(module_id, config)` — finds module via entry points only

After resolving path, the loader:
- Imports the module via `importlib.import_module()` or path manipulation
- Retrieves the `mount` callable (the entry-point function)
- Returns a closure `mount_with_config(coordinator)` that calls `mount(coordinator, config)`

The `tool-dot-graph` entry-point name is parsed by naming convention:
`tool-dot-graph` → prefix `tool-` → type `tool` → mount point `tools`.

---

## 4. The `mount()` Function — Tool Registration

**`amplifier_module_tool_dot_graph/__init__.py:286-312`:**

```python
async def mount(coordinator: Any, config: dict[str, Any] | None = None) -> dict[str, Any]:
    tool = DotGraphTool()
    await coordinator.mount("tools", tool, name=tool.name)   # name="dot_graph"
    logger.info("tool-dot-graph mounted: registered 'dot_graph' tool "
                "with validate/render/setup/analyze/prescan/assemble routing (v0.4.0)")
    return {
        "name": "tool-dot-graph",
        "version": "0.4.0",          # NOTE: differs from pyproject "0.1.0"
        "provides": ["dot_graph"],
    }
```

After mounting, the tool is reachable as `coordinator["tools"]["dot_graph"]`.

The `DotGraphTool` object has three properties:
- `name` → `"dot_graph"` (tool call name used by the LLM)
- `description` → the full operation-by-operation text shown in the system prompt
- `input_schema` → JSON Schema with `operation` (required), `dot_content`, and `options`

---

## 5. amplifierd Tool Threading Wrapper

**`amplifierd/src/amplifierd/threading.py:51` — `wrap_tools_for_threading(session)`:**

Called at two points in amplifierd:
- `session_manager.py:273-275` — after `create_session()`
- `spawn.py:261` — after `child_session.initialize()`

**`threading.py:21-49` — `ThreadedToolWrapper.execute(input_data)`:**

```python
async def execute(self, input: dict) -> Any:
    coro = tool.execute(input)                          # create coroutine on main thread
    return await asyncio.to_thread(asyncio.run, coro)  # run in worker thread, own event loop
```

**Critical design:** Each `DotGraphTool.execute()` call runs inside a fresh `asyncio.run()` in a
thread-pool worker. This isolation means:
- Blocking subprocess calls in `render.py` and `validate.py` cannot stall the SSE event loop
- Each tool call has its own independent event loop (no shared async state between calls)
- The coroutine object is created on the main thread (before `to_thread`) to avoid potential
  loop-attachment issues in pydot/networkx internals

---

## 6. `DotGraphTool.execute()` — Operation Dispatcher

**`__init__.py:195-283`:**

```python
async def execute(self, input_data: dict[str, Any]) -> ToolResult:
    operation = input_data.get("operation", "unknown")
    dot_content = input_data.get("dot_content", "")
    options: dict[str, Any] = input_data.get("options") or {}

    if operation == "validate":  → validate.validate_dot(dot_content, layers)
    if operation == "render":    → render.render_dot(dot_content, format, engine, output_path)
    if operation == "setup":     → setup_helper.check_environment()
    if operation == "analyze":   → analyze.analyze_dot(dot_content, options)
    if operation == "prescan":   → prescan.prescan_repo(repo_path)
    if operation == "assemble":  → assemble.assemble_hierarchy(manifest, output_dir, render_png)
    # else: return ToolResult(success=False, output=json.dumps({"error": "Unknown operation"}))
```

All operations return `ToolResult(success: bool, output: str)` where `output` is
`json.dumps(result_dict)`. The `success` flag mirrors the dict's `success` key.

---

## 7. Validate Operation (`validate.py`)

**`validate.py:37-94` — `validate_dot(dot_content, layers)`:**

Three independent, stackable layers (run all three by default):

### Layer 1 — Syntax (`validate.py:73-77`)

```python
captured = io.StringIO()
with contextlib.redirect_stdout(captured):           # pydot prints errors to stdout
    graphs = pydot.graph_from_dot_data(dot_content)
```

**Why stdout redirect?** pydot's parser prints parse errors to `stdout`, not `stderr`. Without
capture, these would appear in the daemon's serve.log. The redirect captures them cleanly, and
the first captured line is used as the error message if parsing fails.

Returns `(graph_or_None, issues_list)`. If `graph is None`, Layer 2 is skipped.

### Layer 2 — Structural (`validate.py:135-225`)

Operates on the parsed `pydot.Dot` object. Checks:

| Check | Severity | Rule |
|-------|----------|------|
| Empty graph | error | No nodes AND no edges |
| Unreachable nodes | warn | Has outgoing edges, no incoming, not in `_ENTRY_HINTS` |
| Isolated nodes | warn | Zero edges of any kind |
| Orphan clusters | warn | No edges connecting cluster nodes to outside nodes |
| Missing legend | info | ≥ 10 nodes but no cluster named `cluster_legend*` or `legend` |

**`_ENTRY_HINTS = {"start", "entry", "root", "begin", "init", "source"}`** — these node names
are exempt from "no incoming edges" warnings even if they have in-degree 0.

**`_PSEUDO_NODES = {"node", "edge", "graph"}`** — pydot injects these synthetic nodes from
default-style declarations (e.g. `node [style=filled]`). They are filtered from all counts.

Node collection is recursive: `_recurse_nodes()` walks all subgraphs. Edge endpoints not
explicitly declared as nodes are also collected via `_collect_edge_endpoint_names()`.

### Layer 3 — Render Quality (`validate.py:233-284`)

```python
result = subprocess.run(
    ["dot", "-Tcanon", tmp_path],
    capture_output=True, text=True, timeout=30,
)
```

`-Tcanon` produces normalized DOT output — a lightweight render pass that catches attributes
graphviz rejects (e.g. bad color names, malformed HTML labels) without writing a full output
file. Timeout: 30 seconds. Temp file created with `NamedTemporaryFile(delete=False)` and
unlinked in `finally`. Returns `[]` (no issues) if graphviz is not installed — degrades
gracefully with an `info` issue rather than failing.

**Return schema:**
```json
{"valid": bool, "issues": [{"layer": str, "severity": str, "message": str}],
 "stats": {"nodes": int, "edges": int, "clusters": int, "lines": int}}
```

---

## 8. Render Operation (`render.py`)

**`render.py:25-149` — `render_dot(dot_content, output_format, engine, output_path)`:**

### Environment check first (`render.py:63-83`)

`setup_helper.check_environment()` → `shutil.which("dot")` detects graphviz.
If not installed, returns `{success: False, error: "Graphviz not installed. <install_hint>"}`.
If the requested engine is not on PATH, returns an error listing available engines.

### Intentional `tempfile.mktemp()` use (`render.py:89-93`)

```python
# NOTE: tempfile.mktemp() is deprecated due to TOCTOU race conditions.
# It is intentional here: graphviz requires the output path to not exist
# before writing, so NamedTemporaryFile(delete=False) + close would leave
# an empty file that some graphviz versions refuse to overwrite.
output_path = tempfile.mktemp(suffix=f".{output_format}")
```

The TOCTOU risk is accepted because the output path is in a temp dir, the window is tiny,
and graphviz's behavior of refusing to overwrite non-empty files would be worse.

### Subprocess call (`render.py:105-110`)

```python
subprocess.run(
    [engine, f"-T{output_format}", tmp_dot_path, "-o", output_path],
    capture_output=True, text=True, timeout=30,
)
```

Cleanup: `tmp_dot_path` unlinked in `finally`. If rendering failed and the path was
auto-generated, the partial output file is also removed.

**Return schema (success):**
```json
{"success": true, "output_path": str, "format": str, "engine": str, "size_bytes": int}
```

---

## 9. Setup Operation (`setup_helper.py`)

**`setup_helper.py:12-23` — `check_environment()`:**

```python
return {
    "graphviz": _check_graphviz(),   # shutil.which("dot") + subprocess "dot -V" + engine scan
    "pydot":    _check_pydot(),      # try: import pydot
    "networkx": _check_networkx(),   # try: import networkx
}
```

`_check_graphviz()` (`setup_helper.py:26-67`):
1. `shutil.which("dot")` → not found → return `{installed: False, install_hint: …}`
2. `subprocess.run(["dot", "-V"])` → version appears in **stderr** (graphviz quirk)
3. Scan each engine name with `shutil.which()` → builds `engines: […]` list
4. Install hints are platform-specific (Darwin/Linux/Windows) via `platform.system()`

---

## 10. Analyze Operation (`analyze.py`)

**`analyze.py:57-109` — `analyze_dot(dot_content, options)`:**

### Pre-NetworkX routing

Two operations bypass the pydot→NetworkX conversion:
- **`diff`** (`analyze.py:81-82`): `_diff(dot_content, options)` — parses both DOT strings independently via pydot, converts both to NetworkX, then compares node sets and edge sets
- **`subgraph_extract`** (`analyze.py:83-84`): `_dispatch_subgraph_extract()` — uses pydot directly to preserve cluster structure, labels, and attributes (NetworkX discards cluster hierarchy)

### pydot → NetworkX conversion (`analyze.py:172-223`)

**`_pydot_to_networkx(graph)` — critical recursive walk:**

```python
# Plain `from_pydot` call only sees top-level nodes and edges.
# Assembled DOT files place everything inside cluster_* blocks —
# yielding 0 nodes without the recursive walk.
def _collect_all_nodes_and_edges(pydot_graph):
    nodes = list(pydot_graph.get_nodes())
    edges = list(pydot_graph.get_edges())
    for subgraph in pydot_graph.get_subgraphs():
        sub_nodes, sub_edges = _collect_all_nodes_and_edges(subgraph)
        nodes.extend(sub_nodes); edges.extend(sub_edges)
    return nodes, edges
```

Pseudo-nodes (`node`, `edge`, `graph`) are filtered at three stages: explicit node add,
edge endpoint add, and a final sweep. Produces `MultiDiGraph` for digraphs, `MultiGraph` for
undirected.

### Operations dispatched after NetworkX conversion

| Operation | Function | Key behaviour |
|-----------|----------|---------------|
| `stats` | `_stats(G)` | density, is_dag, weakly_connected_components, self_loops |
| `reachability` | `_reachability(G, options)` | `nx.descendants(G, source)` |
| `unreachable` | `_unreachable(G, dot_content)` | in-degree == 0; not in `_ENTRY_HINTS`; produces annotated DOT |
| `cycles` | `_cycles(G, dot_content)` | `nx.simple_cycles(G)`; annotates cycle edges red |
| `paths` | `_paths(G, options)` | `nx.all_simple_paths(G, src, tgt)`; capped at `_PATH_CAP = 100` |
| `critical_path` | `_critical_path(G)` | `nx.dag_longest_path(G)`; fails if not DAG |
| `diff` | `_diff(dot_content_a, options)` | node + edge set diff; ignores MultiDiGraph parallel-edge keys |
| `subgraph_extract` | `_subgraph_extract(pydot_graph, …)` | builds standalone `pydot.Dot` from cluster; re-derives counts via NetworkX |

**Path-count cap (`analyze.py:491-498`):**
```python
for path in path_gen:
    raw_paths.append(…)
    if len(raw_paths) == _PATH_CAP:
        try:
            next(path_gen)          # peek to check if 101st path exists
            truncated = True
        except StopIteration:
            pass                    # exactly 100 paths — no truncation
        break
```

**DOT annotation helpers** (`analyze.py:553-619`): `_annotate_nodes()` and
`_annotate_edges()` inject inline attribute declarations immediately after the first `{`
line in the DOT source. This is a text-level injection, not a pydot round-trip, so original
formatting is preserved.

---

## 11. Prescan Operation (`prescan.py`)

**`prescan.py:93-157` — `prescan_repo(repo_path)`:**

Pure Python — no LLM, no graphviz, no pydot, no networkx. Uses only `os.walk` and `pathlib`.

### Skip dirs (`prescan.py:19-37`)

```python
_SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".env",
              "target", "build", "dist", ".tox", ".mypy_cache", ".pytest_cache",
              ".ruff_cache", ".eggs", "egg-info"}
```

Pruned in-place during `os.walk()` so the walker never descends into them.

### Module detection (`prescan.py:40-45`, `199-264`)

Priority order (first match wins per directory):
```python
_MODULE_INDICATORS = [
    ("Cargo.toml",  "rust_crate"),
    ("go.mod",      "go_module"),
    ("package.json","node_package"),
    ("__init__.py", "python_package"),
]
```

Repo root is excluded from module detection. For each module directory, files are attributed
to that module only if no intermediate ancestor is itself a module root (`_is_file_in_module`).

### Directory tree (`prescan.py:340-385`)

Capped at `_MAX_TREE_DEPTH = 4`. Only directories included (not files). `PermissionError`
returns `{}` without failing the whole scan.

**Return schema (success):**
```json
{
  "success": true,
  "repo_path": str,
  "languages": {"py": 42, "toml": 3, …},
  "total_files": int,
  "modules": [{"name": str, "path": str, "type": str, "indicator": str,
               "file_count": int, "files_by_type": {…}}],
  "build_manifests": ["pyproject.toml", …],
  "entry_points": ["src/main.py", …],
  "directory_tree": {nested dict}
}
```

---

## 12. Assemble Operation (`assemble.py`)

**`assemble.py:27-145` — `assemble_hierarchy(manifest, output_dir, render_png)`:**

### Key design constraint (`assemble.py:33-36`)

> Per-module DOT files are NOT copied — they remain in their canonical location
> (`output/modules/{slug}/diagram.dot`). The `subsystems/` directory is reserved for
> true subsystem-aggregate DOTs explicitly written by agents or recipes.

### Execution flow

1. **Validate manifest** — requires `modules` and `subsystems` keys
2. **Create `output_dir`** — `mkdir(parents=True, exist_ok=True)`
3. **Warn on missing module DOTs** — logs warnings but does NOT fail
4. **Discover subsystem DOTs** — `glob("subsystems/*.dot")` only if `subsystems/` exists
5. **Check for overview DOT** — looks for `output_dir/overview.dot`
6. **Write `manifest.json`** — records modules_def, subsystems_def, overview_path
7. **Optional PNG render** — if `render_png=True`, calls `render.render_dot()` for each DOT; failures are non-fatal warnings
8. Return stats: `{"subsystems": int, "modules": int}`

**`render_png` default** (`__init__.py:263`):
```python
render_png: bool = bool(options.get("render_png", True))  # Tool defaults to True
# But assemble_hierarchy itself defaults to False — protects tests without graphviz
```

---

## 13. Complete Execution Path — Tool Call to SSE Wire

```
LLM produces tool_use block {name: "dot_graph", input: {operation, dot_content, options}}
  │
  ▼ amplifier_core kernel dispatches to tool
coordinator["tools"]["dot_graph"].execute(input_data)
  │  ← ThreadedToolWrapper intercepts (threading.py:29)
  ▼
coro = DotGraphTool.execute(input_data)               # create coroutine on main thread
await asyncio.to_thread(asyncio.run, coro)            # run in worker thread + own event loop
  │
  ▼ DotGraphTool.execute() dispatches by operation
  ├─ "validate" → validate.validate_dot()
  │    ├─ Layer 1: pydot.graph_from_dot_data()        # stdout redirect captures parse errors
  │    ├─ Layer 2: _check_structural()                # pydot recursive walk
  │    └─ Layer 3: subprocess.run(["dot", "-Tcanon"]) # 30s timeout, tempfile cleanup
  ├─ "render" → render.render_dot()
  │    ├─ setup_helper.check_environment()            # shutil.which("dot")
  │    ├─ tempfile.mktemp() → intentional TOCTOU
  │    └─ subprocess.run([engine, f"-T{fmt}", ...])   # 30s timeout
  ├─ "setup" → setup_helper.check_environment()       # dot -V version detection
  ├─ "analyze" → analyze.analyze_dot()
  │    ├─ "diff" / "subgraph_extract" → pydot only (before NetworkX)
  │    └─ others → pydot → _pydot_to_networkx() (recursive cluster walk) → nx ops
  ├─ "prescan" → prescan.prescan_repo()
  │    └─ os.walk() + _SKIP_DIRS + _MODULE_INDICATORS → structured JSON inventory
  └─ "assemble" → assemble.assemble_hierarchy()
       └─ glob("subsystems/*.dot") + manifest.json + optional render_dot()
  │
  ▼ returns ToolResult(success=bool, output=json_str)
amplifier_core hooks.emit("tool:post", data)
  │
  ▼ SessionHandle._wire_events() hook fires
event_bus.publish(session_id, "tool:post", data)      # event_bus.py:105 — synchronous
  │
  ▼ _Subscriber.queue.put_nowait(TransportEvent)
_event_generator() in events.py formats SSE string
  │
  ▼ StreamingResponse delivers to HTTP client
```

---

## Key Architecture Invariants

| Invariant | Location |
|-----------|----------|
| Tool execution runs in isolated worker thread with its own event loop | `threading.py:29-40` |
| pydot stdout is redirected to prevent parse errors polluting serve.log | `validate.py:112-119`, `analyze.py:133-136` |
| Layer 3 (render quality) degrades gracefully if graphviz not installed | `validate.py:242-252` |
| `tempfile.mktemp()` TOCTOU is intentional — graphviz refuses to overwrite | `render.py:89-93` |
| Recursive subgraph walk is mandatory — assembled DOTs hide nodes in clusters | `analyze.py:147-169` |
| `diff` and `subgraph_extract` bypass NetworkX (operate on pydot) | `analyze.py:81-84` |
| Per-module DOTs stay canonical; assemble only discovers subsystem-level DOTs | `assemble.py:33-36` |
| `render_png` defaults True in tool interface, False in `assemble_hierarchy()` | `__init__.py:263`, `assemble.py:30` |
| Prescan is pure Python — no LLM, no graphviz, no pydot, no networkx | `prescan.py:6` |
| Module naming convention `tool-*` → mount point `tools` | `amplifier_core/loader.py:114` |
| `mount()` version string ("0.4.0") differs from pyproject version ("0.1.0") | `__init__.py:306`, `pyproject.toml:4` |
| Path count capped at 100; peek mechanism detects true truncation | `analyze.py:491-498` |
