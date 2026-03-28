# Amplifierd Architecture Design

## Goal

Build `amplifierd`, a long-running localhost daemon that exposes amplifier-core and amplifier-foundation capabilities over HTTP, SSE, and WebSocket.

## Background

The Amplifier ecosystem has two library layers — `amplifier-core` (session lifecycle, orchestration, hooks, modules, error taxonomy) and `amplifier-foundation` (bundles, agent spawning, source resolution, config merging). These are powerful but have no standalone service interface. The existing `distro-server` is an "experience server" serving chat UIs, Slack integrations, and voice interfaces. `amplifierd` is architecturally distinct: a lower-level daemon exposing raw capabilities for programmatic consumers — CLIs, editors, CI pipelines, and other services.

## Design Decisions

| Decision | Resolution |
|---|---|
| Framework | FastAPI + uvicorn |
| Package management | uv with pyproject.toml |
| Authentication | Layered. Localhost: none (trusted). Network (--host 0.0.0.0): PAM session cookies via auth plugin. Behind proxy: X-Authenticated-User header trust (AMPLIFIERD_TRUST_PROXY_AUTH). API key (AMPLIFIERD_API_KEY) for programmatic access. |
| Default bind | `127.0.0.1:8410`. Port auto-increments if occupied. |
| Dependencies | `amplifier-core` and `amplifier-foundation` as pip/uv dependencies |
| Persistence | Filesystem (`transcript.jsonl` + `metadata.json` + `events.jsonl`) |
| Session model | Approach A — SessionManager with per-session queue (stateful in-memory) |
| Event delivery | Global EventBus with automatic session-tree propagation |
| Bundle reload | Stale flag pattern (mark stale, reload on next execute) |
| Multi-tenancy | None. Single-user. |
| TLS | Off by default (localhost). Auto mode: Tailscale cert → self-signed fallback. Manual mode: user-provided certs. |
| Cookie policy | secure=auto (matches TLS mode), samesite=lax (allows Slack/email link navigation). Configurable via env vars. |
| Proxy trust | AMPLIFIERD_TRUSTED_PROXIES gates X-Forwarded-For header trust. Defaults to localhost. CORS uses dynamic origin allow-list (not wildcard). |
| Contribution system | Not exposed (zero production callers; kept internal) |
| gRPC | Not now. HTTP + SSE + WebSocket only. |
| Hot-reload (dev) | uvicorn `--reload` flag passthrough |
| Docker | Ready from day one. `amplifierd serve --host 0.0.0.0 --port 8410` |

## Project Structure

```
amplifier-distro/amplifierd/
├── pyproject.toml              # Package: amplifierd, entry point: amplifierd
├── src/
│   └── amplifierd/
│       ├── __init__.py         # Version, package metadata
│       ├── __main__.py         # python -m amplifierd support
│       ├── cli.py              # CLI: amplifierd serve [--port] [--host] [--reload]
│       ├── config.py           # DaemonSettings (pydantic-settings, JSON + env)
│       ├── app.py              # FastAPI app factory: create_app()
│       ├── state.py            # SessionManager + EventBus (the in-memory singletons)
│       ├── models.py           # Pydantic request/response models
│       ├── errors.py           # Error handlers (LLMError → Problem Details)
│       ├── plugins.py          # Plugin discovery and loading (entry points)
│       └── routes/
│           ├── __init__.py     # Router registration
│           ├── sessions.py     # CRUD + execute + execute/stream
│           ├── events.py       # Global SSE endpoint
│           ├── agents.py       # Spawn, resume child sessions
│           ├── bundles.py      # Registry, load, prepare, compose
│           ├── modules.py      # Discover, mount, unmount
│           ├── context.py      # Message get/set/add/clear
│           ├── approvals.py    # List, respond, WebSocket
│           ├── validation.py   # Mount plan, module, bundle validation
│           ├── health.py       # /health, /info
│           └── reload.py       # Hot-reload / stale endpoints
├── tests/
│   └── ...
└── design/
    ├── architecture.md         # This document
    ├── endpoints.md            # 53 endpoints across 14 categories
    ├── events.md               # 58 event types with SSE envelope and data schemas
    └── errors.md               # RFC 7807 Problem Details mapping
```

The `cli.py` uses Click (or argparse) to provide `amplifierd serve --port 8410 --host 127.0.0.1 --reload`. The `--reload` flag passes through to uvicorn for dev hot-reload. The `app.py` has a `create_app()` factory so tests can create isolated app instances.

## Daemon Configuration

The daemon is configured via `~/.amplifierd/settings.json`, with env var and CLI flag overrides.

**Config file:** `~/.amplifierd/settings.json`

```json
{
  "host": "127.0.0.1",
  "port": 8410,
  "default_working_dir": "/home/user",
  "log_level": "info",
  "disabled_plugins": []
}
```

**Priority order** (highest wins):

1. CLI flags (`--port`, `--host`, `--log-level`)
2. Environment variables (`AMPLIFIERD_PORT`, `AMPLIFIERD_HOST`, `AMPLIFIERD_LOG_LEVEL`, `AMPLIFIERD_DEFAULT_WORKING_DIR`)
3. Settings file (`~/.amplifierd/settings.json`)
4. Built-in defaults (`127.0.0.1:8410`, log_level `info`, default_working_dir = user's home)

The settings file is created automatically on first run if it doesn't exist. The `~/.amplifierd/` directory is separate from the amplifier ecosystem's `~/.amplifier/` directory — the daemon has its own configuration space.

**Implementation:** `config.py` defines `DaemonSettings` using Pydantic-settings (`BaseSettings`) with a JSON settings source, env prefix `AMPLIFIERD_`, and CLI override in `cli.py`.

## Working Directory

Sessions have a working directory that tools (bash, filesystem) and @mention resolution use as the base path for relative paths.

**On session creation:** `POST /sessions` accepts an optional `working_dir` field (absolute path).

```json
{
  "bundle": "foundation",
  "working_dir": "/home/user/myproject"
}
```

This is passed as `session_cwd` to `PreparedBundle.create_session()`, which registers it as the `session.working_dir` capability on the coordinator.

**Default fallback chain:**

1. `working_dir` from the request body (if provided)
2. `default_working_dir` from daemon config (`~/.amplifierd/settings.json`)
3. User's home directory

**Updating on a running session:** `PATCH /sessions/{id}` can update `working_dir`. This updates the `session.working_dir` capability on the coordinator via `set_working_dir(coordinator, path)`. Tools that call `get_working_dir(coordinator)` will see the new path on subsequent calls.

**No server-wide lock-down:** Unlike some similar projects, the daemon config provides a convenient default, not a constraint. Sessions are free to use any absolute path.

## @Mention Resolution

The daemon wires up Foundation's `BaseMentionResolver` at session creation to support both bundle file references and working directory file references in prompts and context.

**Resolution chain** (handled by Foundation's `BaseMentionResolver`):

- `@bundle:path` → resolves to files within loaded bundles (e.g., `@foundation:docs/BUNDLE_GUIDE.md`)
- `@path` → resolves relative to the session's `working_dir`
- `@~/path` → resolves relative to the user's home directory

This is wired up automatically by `PreparedBundle.create_session()`, which registers a `mention_resolver` capability on the coordinator when the bundle has context or instructions. The daemon just needs to pass the correct `session_cwd` so that relative @mentions resolve against the right directory.

**Key point:** The daemon does NOT reimplement mention resolution. It relies entirely on Foundation's existing `BaseMentionResolver` and `load_mentions()` infrastructure. The daemon's responsibility is simply to configure the session with the correct `working_dir` so Foundation's resolver works correctly.

## Architecture

### Component Overview

```
┌─────────────────────────────────────────────────────────┐
│                       Clients                           │
│         (CLI, editors, CI pipelines, services)          │
└──────────┬──────────────┬───────────────┬───────────────┘
           │ HTTP         │ SSE           │ WebSocket
           ▼              ▼               ▼
┌─────────────────────────────────────────────────────────┐
│                    FastAPI Routes                        │
│  sessions │ events │ agents │ bundles │ modules │ ...   │
└──────────┬──────────────┬───────────────┬───────────────┘
           │              │               │
           ▼              ▼               ▼
┌──────────────────┐ ┌──────────┐ ┌───────────────┐
│  SessionManager  │ │ EventBus │ │ BundleRegistry│
│                  │ │          │ │               │
│  SessionHandle   │─┤ publish()│ │  load()       │
│  SessionHandle   │ │          │ │  prepare()    │
│  SessionHandle   │ │          │ │  compose()    │
└────────┬─────────┘ └──────────┘ └───────────────┘
         │
         ▼
┌─────────────────────────────────────────────────────────┐
│              amplifier-core + amplifier-foundation       │
│  AmplifierSession │ ModuleCoordinator │ HookRegistry    │
│  PreparedBundle   │ BundleRegistry    │ SourceResolver  │
└─────────────────────────────────────────────────────────┘
         │
         ▼
┌─────────────────────────────────────────────────────────┐
│                    Filesystem                            │
│  ~/.amplifier/projects/<slug>/sessions/<id>/            │
│    transcript.jsonl │ metadata.json │ events.jsonl      │
└─────────────────────────────────────────────────────────┘
```

## Components

### SessionManager

The central owner of all live sessions. A single in-memory instance created at startup.

```
SessionManager
├── sessions: dict[str, SessionHandle]
├── event_bus: EventBus
├── create(bundle, config_overrides, ...) → SessionHandle
├── get(session_id) → SessionHandle | None
├── resume(session_id, session_dir) → SessionHandle
├── destroy(session_id) → None
└── list(status_filter) → list[SessionSummary]
```

The `SessionManager` is the only component that creates or destroys `SessionHandle` instances. It holds a reference to the `EventBus` and passes it to each new `SessionHandle` so they can publish events.

### SessionHandle

Wraps one live `AmplifierSession` and serializes access to it. This is the core unit of the daemon.

```
SessionHandle
├── session: AmplifierSession
├── prepared_bundle: PreparedBundle
├── queue: asyncio.Queue          # Serializes execute requests
├── worker: asyncio.Task          # Processes queue items one at a time
├── status: idle | executing | completed | failed
├── stale: bool                   # Flag for bundle reload on next execute
├── execute(prompt) → str         # Enqueues, waits for result
├── cancel(immediate) → None
├── mark_stale() → None
├── children: dict[str, str]      # child_session_id → agent_name
└── tree() → SessionTree          # Full descendant hierarchy
```

Each `SessionHandle` registers a hook on `ALL_EVENTS` that publishes every event to the global `EventBus`. When a session spawns a child via the delegate tool, the child gets its own `SessionHandle` (registered with the `SessionManager`) and the parent's `children` dict records the relationship.

### EventBus

Global event fanout for all sessions. A single in-memory instance created at startup.

```
EventBus
├── subscribe(session_id?, filter?) → AsyncIterator[Event]
├── publish(session_id, event_name, data) → None
└── subscriber_count → int
```

The `EventBus` knows the session tree (via `SessionManager`) so that subscribing to a parent session automatically includes events from all descendants — children, grandchildren, etc. Clients don't need to manually track spawned session IDs.

### BundleRegistry

Wraps `amplifier-foundation`'s bundle system. Created at startup, pointed at `~/.amplifier` home.

Used by the `/bundles` routes for registration, loading, preparation, and composition. The `SessionManager` uses it when creating new sessions and when reloading bundles for stale sessions.

### Two-Tier Bundle Caching

The `BundleRegistry` (or a companion `BundleCache`) implements two-tier caching to avoid redundant bundle loading and preparation across sessions:

- **L1 cache — URI → loaded `Bundle`**: Avoids re-fetching and re-parsing the same bundle source. Keyed by the bundle's source URI (e.g., file path or registry identifier). If two sessions reference the same bundle URI, the second gets the already-parsed `Bundle` object instantly.

- **L2 cache — composite key → `PreparedBundle`**: Avoids re-preparing (downloading modules, installing dependencies, resolving sources) when multiple sessions use the same bundle composition. The composite key encodes the bundle identity plus its composition inputs, e.g., `"{bundle_name}:{behaviors_hash}:{provider_hash}"`. Different compositions of the same base bundle get separate L2 entries.

**Cost model:** The first session with a given bundle pays the full load + prepare cost. Subsequent sessions with the same bundle composition get the `PreparedBundle` instantly. Sessions with the same base bundle but different compositions pay only the prepare cost (L1 hit, L2 miss).

**Invalidation:** When `POST /sessions/{id}/stale` is called, the stale flag on the `SessionHandle` triggers a reload on next execute. The reload invalidates both L1 and L2 cache entries for the affected bundle, ensuring the next preparation picks up source changes. Cache entries for unaffected bundles remain warm.

## Data Flow

### Execute Flow — Dual Execution Model

Two execution modes are available, sharing the same internal pipeline (queue → worker → AmplifierSession). The difference is only in how the HTTP response is handled.

#### Synchronous: `POST /sessions/{id}/execute`

Blocks until execution completes. Returns the full response. Good for simple clients, scripts, and testing.

```
Client                  FastAPI              SessionHandle         AmplifierSession
  │                       │                       │                       │
  ├─POST /sessions/{id}/execute──►                │                       │
  │                       ├──get(id)──►           │                       │
  │                       │           ◄──handle───┤                       │
  │                       ├──handle.execute(prompt)──►                    │
  │                       │                       ├──[if stale: reload]   │
  │                       │                       ├──queue.put(prompt)──► │
  │                       │                       │   worker dequeues     │
  │                       │                       ├──session.execute()────►
  │                       │                       │                       │
  │                       │                       │  ◄──events via hooks──┤
  │                       │                       ├──EventBus.publish()   │
  │                       │                       │                       │
  │                       │                       │  ◄──result────────────┤
  │                       │  ◄──result────────────┤                       │
  │  ◄──HTTP response─────┤                       │                       │
```

#### Async Fire-and-Forget: `POST /sessions/{id}/execute/stream`

Returns `202 Accepted` immediately with a `correlation_id`. Execution runs as an `asyncio.create_task()` in the SessionHandle's worker. Results flow through the EventBus SSE stream. The client gets the response by subscribing to `GET /events?session={id}` and filtering for events with the matching `correlation_id`.

```
Client                  FastAPI              SessionHandle         AmplifierSession
  │                       │                       │                       │
  ├─POST /sessions/{id}/execute/stream──►         │                       │
  │                       ├──get(id)──►           │                       │
  │                       │           ◄──handle───┤                       │
  │                       ├──handle.execute_async(prompt)──►              │
  │  ◄──202 Accepted──────┤                       │                       │
  │   {correlation_id,    │                       ├──[if stale: reload]   │
  │    session_id,        │                       ├──queue.put(prompt)──► │
  │    status: accepted}  │                       │   worker dequeues     │
  │                       │                       ├──session.execute()────►
  │                       │                       │                       │
  │  (client subscribes   │                       │  ◄──events via hooks──┤
  │   to GET /events)     │                       ├──EventBus.publish()   │
  │  ◄──SSE: events───────┤◄──────────────────────┤                       │
  │                       │                       │  ◄──result────────────┤
  │  ◄──SSE: execute:done─┤◄──────────────────────┤                       │
```

Response for the 202:

```json
{
  "correlation_id": "prompt_abc123_1",
  "session_id": "abc123",
  "status": "accepted"
}
```

This decouples HTTP response time from LLM latency. Clients that want synchronous behavior use `/execute`. Clients building UIs use `/execute/stream` + SSE.

### Event Subscription Flow

```
Client                  FastAPI              EventBus            SessionHandle(s)
  │                       │                    │                       │
  ├─GET /events?session=abc──►                 │                       │
  │                       ├──subscribe(abc)───►│                       │
  │                       │                    │  (abc and all          │
  │                       │                    │   descendants)         │
  │                       │                    │                       │
  │                       │                    │  ◄──publish(abc,evt)───┤
  │  ◄──SSE: event────────┤◄──yield event──────┤                       │
  │                       │                    │                       │
  │                       │                    │  ◄──publish(child,evt)─┤
  │  ◄──SSE: event────────┤◄──yield event──────┤   (child of abc)      │
  │                       │                    │                       │
  │  ◄──SSE: keepalive────┤◄──(15s timeout)────┤                       │
```

### Stale Reload Flow

```
Client                  FastAPI              SessionHandle
  │                       │                       │
  ├─POST /sessions/{id}/stale──►                  │
  │                       ├──handle.mark_stale()──►
  │                       │                       ├──self.stale = True
  │  ◄──200 OK────────────┤                       │
  │                       │                       │
  │  ... time passes ...  │                       │
  │                       │                       │
  ├─POST /sessions/{id}/execute──►                │
  │                       ├──handle.execute()─────►
  │                       │                       ├──[stale == True]
  │                       │                       ├──reload bundle from source
  │                       │                       ├──re-compose includes
  │                       │                       ├──unmount all modules
  │                       │                       ├──remount from new PreparedBundle
  │                       │                       ├──self.stale = False
  │                       │                       ├──session.execute(prompt)
  │                       │                       │  ... normal flow ...
```

## Concurrency Model

**Between sessions:** Fully concurrent. Each `SessionHandle` is independent. Multiple sessions can execute simultaneously without coordination.

**Within a session:** Serialized. The per-session `asyncio.Queue` + worker task ensures one `execute()` at a time. A second request to the same session blocks until the first completes. This matches how `AmplifierSession` works — one orchestrator loop at a time.

**Reads are always safe:** `GET` endpoints (messages, status, hooks, tree) read from the live session without queuing. Only `execute()` and bundle reload go through the queue.

## Persistence

Filesystem layout, honoring `AMPLIFIER_HOME_CONTRACT.md`:

```
~/.amplifier/projects/<project-slug>/sessions/<session-id>/
├── transcript.jsonl    # Conversation messages, appended incrementally
├── metadata.json       # Session metadata (bundle, status, timestamps, stats)
└── events.jsonl        # Full event log
```

Persistence is handled by hooks registered on each session (the same pattern used by `distro-server`'s `register_transcript_hooks` and `register_metadata_hooks`). The daemon writes incrementally — after each tool call and at turn boundaries — not just at session end.

**Resume:** `POST /sessions/{id}/resume` loads `transcript.jsonl`, creates a fresh `AmplifierSession(is_resumed=True)`, injects the transcript via `context.set_messages()`, and wraps it in a new `SessionHandle`. The session is live again.

### Session Index

The daemon maintains a lightweight `index.json` alongside the per-session directories:

```
~/.amplifier/projects/<project-slug>/sessions/
├── index.json              # Lightweight index of all sessions
├── <session-id-1>/
│   ├── transcript.jsonl
│   ├── metadata.json
│   └── events.jsonl
├── <session-id-2>/
│   └── ...
```

The index contains `SessionIndexEntry` records:

```
SessionIndexEntry
├── session_id: str
├── status: str              # idle, executing, completed, failed
├── bundle: str              # Bundle name/URI
├── created_at: datetime
├── last_activity: datetime
├── parent_session_id: str | None
```

List/filter operations (`GET /sessions`) hit the index first, only loading full metadata for matches. This avoids scanning every session directory as session counts grow. The index is updated atomically whenever session metadata changes (creation, status transition, execution completion).

If the index is missing or corrupted at startup, the daemon rebuilds it by scanning individual session directories.

### Atomic File Writes

ALL non-append file persistence uses the tmp+rename pattern:

1. Write to `{path}.tmp`
2. `os.rename("{path}.tmp", "{path}")`

This prevents corrupted files from partial writes on crash. Applied to:
- `session.json` / `metadata.json` — session state
- `index.json` — session index

JSONL files (`transcript.jsonl`, `events.jsonl`) are append-only and don't need this pattern — a partial append leaves previous entries intact.

## Startup & Shutdown Lifecycle

### Startup (`amplifierd serve`)

1. Load `DaemonSettings` (settings.json → env vars → CLI flags, per priority order in [Daemon Configuration](#daemon-configuration))
2. Create `EventBus` (global singleton)
3. Create `SessionManager` (holds `EventBus` reference and `DaemonSettings` for session defaults like `default_working_dir`)
4. Create `BundleRegistry` (points at `~/.amplifier` home)
5. Create FastAPI app via `create_app(settings, session_manager, event_bus, registry)`
6. Register error handlers (LLMError -> Problem Details, BundleError -> 404/422, etc.)
7. Discover and load plugins (entry points in `amplifierd.plugins` group; see [Plugin System](#plugin-system))
8. Start uvicorn with host/port from `DaemonSettings`

No sessions are created at startup — they're created on demand via `POST /sessions`. The daemon starts fast and idle.

### Startup Resilience

Every startup step in the lifespan function is wrapped in a `try/except` that logs but does NOT kill the daemon. One failed initialization step should never prevent the daemon from accepting requests.

- If `BundleRegistry` init fails → start without a registry (bundles can be registered later via API)
- If the session `index.json` is corrupted → rebuild it from individual session directories
- If `AMPLIFIER_HOME` doesn't exist → create it
- If event persistence setup fails → start without event persistence (sessions still work)

```python
async def lifespan(app):
    try:
        registry = BundleRegistry(home=amplifier_home)
    except Exception:
        logger.warning("Bundle registry init failed, starting without registry")
        registry = BundleRegistry()  # empty fallback

    try:
        index = SessionIndex.load(sessions_dir / "index.json")
    except Exception:
        logger.warning("Session index corrupted, rebuilding from disk")
        index = SessionIndex.rebuild(sessions_dir)

    # ... continue with other steps
```

This ensures the daemon is always reachable for health checks and basic operations, even if some subsystems are degraded.

### Shutdown (SIGTERM/SIGINT)

1. Stop accepting new requests
2. For each live session: emit `session:end`, call `session.cleanup()`
3. Close all SSE subscriber connections (EventBus drains)
4. Shutdown uvicorn

### Dev Hot-Reload (`--reload`)

Passes through to uvicorn's `--reload` flag. This reloads the Python process on file changes. Live sessions are lost on reload (acceptable for dev — they can be resumed from disk). The explicit `POST /sessions/{id}/stale` endpoint is for marking sessions for bundle reload *without* restarting the process.

### Docker

Entry point: `amplifierd serve --host 0.0.0.0 --port 8410` (bind all interfaces inside container). A `Dockerfile` and `docker-compose.yml` will be provided.

## Event System

### Global SSE Endpoint

**`GET /events`** — the single SSE endpoint for all real-time events.

| Parameter | Type | Description |
|---|---|---|
| `session` | string | Session ID to subscribe to (includes all descendants automatically) |
| `filter` | string | Comma-separated glob patterns for event names (e.g., `content_block:*,tool:*`) |
| `preset` | string | Named shorthand: `streaming`, `tools`, `minimal`, `full`, `debug` |

Omit all parameters to receive all events from all sessions.

### Session Tree Propagation

When subscribing with a `session` parameter, the EventBus automatically includes events from all descendant sessions in the tree. When a parent spawns a child via the delegate tool:

1. The `SessionManager` registers the child as a full `SessionHandle`
2. The parent's `SessionHandle.children` records the relationship
3. Existing subscribers to the parent automatically receive child events

Clients never need to manually track spawned session IDs. `GET /sessions/{id}/tree` returns the live hierarchy for informational purposes.

### Operational Details

- **Keepalive:** Every 15 seconds of inactivity, the bus sends a `keepalive` event to prevent proxy timeouts
- **Backpressure:** Bounded queue per subscriber (10k events); drop oldest if full

### SSE Envelope: correlation_id + sequence

Every SSE event frame includes two fields for client-side correlation and ordering:

- **`correlation_id`**: Ties events to a specific prompt execution. Format: `prompt_{session_id}_{turn_number}`. Lets clients group events when reviewing event history or when multiple executions overlap across sessions.
- **`sequence`**: Monotonically increasing integer per SSE connection (starts at 0). Lets clients detect gaps from dropped events (backpressure) and maintain strict ordering.

Full SSE envelope:

```json
{
  "event": "tool:pre",
  "data": {"tool_name": "bash", "tool_call_id": "tc_42"},
  "session_id": "abc123",
  "timestamp": "2026-03-02T11:31:03.000Z",
  "correlation_id": "prompt_abc123_1",
  "sequence": 5
}
```

The `correlation_id` is set when `execute()` begins and attached to every event emitted during that execution. Child session events inherit the parent's `correlation_id` so the entire delegation tree can be traced back to the originating prompt. The `sequence` is scoped to the SSE connection, not the session — each subscriber gets its own counter.

## Bundle Reload via Stale Flag

`POST /sessions/{id}/stale` marks the session as needing a bundle reload. Returns immediately. The session continues to serve reads normally.

On the next `execute()`, the `SessionHandle`:

1. Detects the stale flag
2. Reloads the bundle from source
3. Re-composes includes
4. Re-prepares the bundle
5. Unmounts all modules
6. Remounts from the new `PreparedBundle`
7. Clears the stale flag
8. Runs the prompt

Context (conversation history) is preserved across reloads.

This pattern is extensible: if file-watch auto-detection is added later, it sets the same stale flag on affected sessions. The daemon doesn't need to know *why* a session is stale — only that it needs to reload before the next execute.

## Approval Handling via asyncio.Future

The `SessionHandle` manages approval gates (human-in-the-loop decisions) using `asyncio.Future` for request/response correlation. This avoids polling and gives clean timeout semantics.

### Flow

1. A hook returns `HookResult(action="ask_user")` during execution. The `SessionHandle` creates an `asyncio.Future` and stores it as a `PendingApproval` keyed by `request_id`.
2. The approval request is published to the `EventBus` as an `approval:required` event, which SSE subscribers receive immediately.
3. The `SessionHandle` awaits the `Future` with a timeout (default 300s, from `HookResult.approval_timeout`). Execution is suspended at this point.
4. When a client `POST`s to `/sessions/{id}/approvals/{request_id}`, the route handler resolves the `Future` with the client's decision (`allow`, `deny`, or `allow_always`).
5. If the timeout expires before a client responds, the `Future` is resolved with the default action (from `HookResult.approval_default`), and execution continues.

### Session-Scoped "Allow Always" Cache

If a client responds with `remember: true` (or uses the `allow_always` action), subsequent approval requests for the same `tool_name` are auto-resolved for the duration of that session. This avoids repeated prompts for tools the user has already trusted. The cache is per-session and does not persist across restarts or resumes — a resumed session starts with a clean approval cache.

```
PendingApproval
├── request_id: str
├── tool_name: str
├── tool_call_id: str
├── future: asyncio.Future[ApprovalDecision]
├── timeout: float
├── default_action: str
└── created_at: datetime
```

## Plugin System

amplifierd supports plugins via Python entry points. Plugins are pip-installable packages that register FastAPI routers, giving extensions (like Slack bridges, voice integration, custom UIs) access to the daemon's shared state without polluting the core server.

### Plugin Contract

A plugin is a Python package that:

1. Declares an entry point in the `amplifierd.plugins` group
2. Exports a `create_router(state) -> fastapi.APIRouter` function

That's the entire contract. No plugin SDK, no abstract base class.

**Plugin `pyproject.toml` example:**

```toml
[project]
name = "amplifierd-plugin-slack"
version = "0.1.0"
dependencies = ["amplifierd"]

[project.entry-points."amplifierd.plugins"]
slack = "amplifierd_plugin_slack:create_router"
```

**Plugin code example:**

```python
# amplifierd_plugin_slack/__init__.py
from fastapi import APIRouter, Request

def create_router(state) -> APIRouter:
    router = APIRouter()

    @router.post("/webhook")
    async def slack_webhook(request: Request):
        session_manager = state.session_manager
        event_bus = state.event_bus
        # ... handle Slack events using daemon state

    return router
```

### Discovery & Loading

At startup (step 7 in [Startup](#startup-amplifierd-serve)), the daemon:

1. Scans the `amplifierd.plugins` entry point group via `importlib.metadata.entry_points()`
2. Skips any plugins listed in `disabled_plugins` from daemon config
3. For each plugin: calls the entry point function with `app.state`, receives an `APIRouter`
4. Mounts each router at `/plugins/{plugin_name}/`
5. Wraps each step in try/except — one broken plugin doesn't prevent the daemon from starting

This follows the same startup resilience pattern as every other lifespan step: log the error, skip the plugin, continue.

### Configuration

In `~/.amplifierd/settings.json`:

```json
{
  "disabled_plugins": ["voice"]
}
```

`disabled_plugins` is a list of plugin names to skip during discovery. Omit or leave empty to load all discovered plugins.

### Why Entry Points (Not Directory Scanning)

Entry points are the standard Python plugin mechanism. They cover both production use (`pip install amplifierd-plugin-slack`) and local development (`uv pip install -e ./my-plugin`). Dependencies are managed by pip/uv. Versioning and distribution are solved. One mechanism covers all cases.

### Plugin Access

Plugins receive `app.state` which includes:

- `session_manager` — create, get, list, destroy sessions
- `event_bus` — subscribe to and publish events
- `bundle_registry` — load and manage bundles
- `settings` — daemon configuration

Plugins can also subscribe to the EventBus for real-time session events (e.g., a Slack plugin watching for session completion to post notifications).

## Error Handling

All errors use RFC 7807 Problem Details format. The full error mapping is documented in `errors.md`. Key mappings:

| Error Category | HTTP Status | Retryable |
|---|---|---|
| `RateLimitError` | 429 (with `Retry-After`) | Yes |
| `ProviderUnavailableError` / `NetworkError` | 503 | Yes |
| `LLMTimeoutError` | 504 | Yes |
| `AuthenticationError` / `AccessDeniedError` | 502 (provider's creds failed, not client's) | No |
| `ContextLengthError` | 413 | No |
| `ContentFilterError` | 422 | No |
| `BundleNotFoundError` | 404 | No |
| `SessionNotFound` | 404 | No |
| `SessionConflict` (e.g., already executing) | 409 | No |
| `AbortError` (cancellation) | 499 | No |

SSE and WebSocket errors use the same Problem Details shape, delivered as `event: error` in SSE streams and `{"type": "error"}` frames on WebSocket.

## Testing Strategy

### Unit Tests

Test `SessionManager`, `SessionHandle`, and `EventBus` in isolation using amplifier-core's testing utilities (`TestCoordinator`, `MockTool`, `ScriptedOrchestrator`, `MockContextManager`). No real LLM calls. These are fast and cover the state management logic: session lifecycle, queue serialization, stale flag behavior, tree tracking, event fanout and filtering.

### Integration Tests

Test FastAPI routes using `httpx.AsyncClient` with the test app from the `create_app()` factory. Create sessions with a `ScriptedOrchestrator` that returns canned responses, execute prompts, verify SSE events arrive, test error responses match Problem Details shape. These validate the HTTP contract without hitting real providers.

### Smoke Tests

One test that creates a real session with a real provider (gated behind `AMPLIFIERD_SMOKE_TEST=1` env var), executes a simple prompt, and verifies the full pipeline works end-to-end. Not run in CI by default.

### What We Don't Need

No mocking of amplifier-core internals. We use real `AmplifierSession` instances with test orchestrators/providers. The daemon is a thin layer — if core works, and our routes correctly call core, we're good.

## Endpoint Updates

This architecture document supersedes some details in `endpoints.md`. The following changes should be reflected:

- **Replace** `POST /sessions/{id}/reload/bundle` with `POST /sessions/{id}/stale`
- **Remove** `POST /sessions/{id}/reload/module` (can be added later if needed)
- **Add** `GET /sessions/{id}/tree` (returns live session hierarchy)
- **Change** `GET /events` to be the primary global SSE endpoint (replaces per-session `GET /sessions/{id}/events`); subscribing with `?session=<id>` automatically includes all descendant sessions
- **Add** `POST /sessions/{id}/execute/stream` (async fire-and-forget execution, returns 202 with `correlation_id`)
- **Add** `working_dir` field to `POST /sessions` request body (optional, absolute path; falls back to daemon config `default_working_dir`, then user's home)
- **Add** `working_dir` field to `PATCH /sessions/{id}` for updating the working directory on a running session
- **Reserve** `/plugins/{plugin_name}/` namespace for plugin-mounted routers — this path prefix will never conflict with core daemon endpoints

## Prior Art — amplifier-runtime

This design was informed by reviewing `amplifier-runtime`, a similar project that exposes Amplifier sessions over HTTP + SSE. That project is a Starlette-based server (~160KB of Python across 9 files) with a global singleton `SessionManager`, filesystem persistence, and `asyncio.Queue`-based streaming. Studying its architecture validated several of our design choices and surfaced patterns worth adopting.

### Adopted from amplifier-runtime

| Pattern | How we use it |
|---|---|
| `asyncio.Queue` bridge for streaming | `SessionHandle` uses a Queue to turn amplifier-core's callback-based hooks into an async generator that feeds SSE frames. Same pattern, confirmed as the right approach. |
| `TransportEvent` with `__slots__` | Lightweight event carrier on the internal hot path (EventBus → SSE writer). Pydantic is used only at the HTTP boundary for request/response validation, not for event fanout. |
| Approval via `asyncio.Future` | Create a Future keyed by `request_id`, await with timeout, resolve from the HTTP POST handler. Session-scoped "allow always" caching included. |
| Two-tier bundle caching | L1 (URI → Bundle) and L2 (composite key → PreparedBundle) to avoid redundant loading and preparation across sessions. |
| `correlation_id` + `sequence` on SSE frames | Every SSE envelope includes a `correlation_id` tying events to a prompt execution and a monotonic `sequence` for gap detection and ordering. |
| Event forwarder closure for child sessions | Child session hooks annotate events with `child_session_id`, `parent_tool_call_id`, and `nesting_depth` before publishing to the parent's event stream. |

### Deliberately improved upon

| amplifier-runtime approach | Our approach | Why |
|---|---|---|
| Module-level singletons (`session_manager = SessionManager()`) | `create_app()` factory with dependency injection | Testable: each test gets an isolated app instance with its own `SessionManager` and `EventBus`. No global state leaking between tests. |
| Ad-hoc JSON error responses | RFC 7807 Problem Details (see `errors.md`) | Standardized, machine-parseable error format with `type`, `title`, `status`, `detail`, and `instance` fields. |
| Per-session SSE endpoints only | Global `GET /events` endpoint with session-tree propagation from day one | Clients subscribe once and receive events from the entire session tree. No need to discover and subscribe to child session endpoints. |
| Raw Starlette with manual request parsing | FastAPI with Pydantic models | Automatic request validation, OpenAPI spec generation, and typed response models. Catches malformed requests before they reach business logic. |
| No graceful shutdown | Explicit shutdown lifecycle (drain sessions, close SSE, cleanup) | Clean process termination without orphaned sessions or dangling connections. |
| Minimal test coverage | Three-tier testing strategy (unit, integration, smoke) | Systematic coverage using amplifier-core's test utilities, `httpx.AsyncClient` for route testing, and optional real-provider smoke tests. |

## Prior Art — lakehoused

This design was also informed by reviewing `lakehoused`, a more mature FastAPI-based daemon that exposes Amplifier sessions over REST + SSE. It has a richer feature set (automation scheduling, unread badges, @mention resolution) and cleaner separation between transport (routers), business logic (services/sessions), persistence (storage/manager), and execution (runner). Several patterns were directly adopted.

### Adopted from lakehoused

| Pattern | How we use it |
|---|---|
| Dual execution model (sync + async/202) | `POST /execute` blocks until done; `POST /execute/stream` returns 202 immediately, results flow via SSE. Decouples HTTP response time from LLM latency. |
| Session index for fast queries | Lightweight `index.json` alongside session directories. List/filter hits the index first, avoids scanning every session dir as counts grow. |
| Atomic file persistence (tmp+rename) | All non-append file writes go to `{path}.tmp` then `os.rename()`. Prevents corrupted metadata from partial writes on crash. |
| Startup resilience (per-step error handling) | Every lifespan init step in `try/except` that logs but doesn't kill the daemon. One failed subsystem shouldn't prevent the daemon from starting. |
| `StreamingHookRegistry` decorator pattern | Hook wrapper delegates all methods to the wrapped `HookRegistry` while intercepting `emit()` to add SSE streaming. Cleaner than subclassing, preserves internal state. Also found in amplifier-runtime. |
| `_update_session(id, update_fn)` pattern | Pass a callable that mutates metadata; the manager handles atomic read-modify-write. Clean API for state transitions. |

### Not adopted (out of scope for v1)

| lakehoused feature | Why we skipped it |
|---|---|
| `CamelCaseModel` base class | Nice for JavaScript frontends, but adds a layer of indirection. Can add later for frontend consumers. |
| Automation / scheduler (APScheduler integration) | Not needed for a programmatic daemon. Clients can schedule externally. |
| Unread badge logic | UI-level concern, not relevant for a programmatic API. |
| @mention resolution | Wired up via Foundation's `BaseMentionResolver` — the daemon passes `session_cwd` so Foundation's resolver handles `@bundle:path`, `@path`, and `@~/path` natively. No daemon-layer reimplementation needed (see [@Mention Resolution](#mention-resolution)). |

## Open Questions

None at this time. All design decisions have been validated.
