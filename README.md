# amplifierd

A localhost HTTP service that exposes Amplifier capabilities over REST, SSE, and WebSocket.

amplifierd is a thin HTTP layer (FastAPI + uvicorn) on top of two libraries:

- **[amplifier-core](https://github.com/microsoft/amplifier-core)** -- the agent runtime: sessions, LLM providers, tool execution, hooks, and the event system. amplifierd wires core events (content deltas, tool calls, approval requests) into its SSE transport and uses `HookResult` for tool-approval gates.
- **[amplifier-foundation](https://github.com/microsoft/amplifier-foundation)** -- higher-level orchestration: bundle loading/preparation, child-session spawning, session forking, and working-directory management. amplifierd delegates bundle lifecycle to `BundleRegistry` and agent delegation to `create_child_session`.

amplifierd itself adds HTTP routing, the `SessionManager`/`EventBus` state layer, plugin discovery, and streaming transport -- but all agent logic lives in the libraries.

## Why amplifierd?

Instead of every application importing amplifier-core as a Python library, amplifierd exposes the capability set over HTTP. Any language, any framework, any generated app can drive Amplifier sessions through REST calls.

This turns Amplifier from a Python-only library into language-agnostic infrastructure. A TypeScript CLI, a Swift desktop app, a Rust tool, or a browser extension can all create sessions, stream events, and manage bundles through the same HTTP interface. The protocol becomes the integration surface, not the Python import.

## Scope and Status

**Experimental.** amplifierd is v0.1.0. No support is provided (see [SUPPORT.md](SUPPORT.md)).

amplifierd is developed as infrastructure for [amplifier-distro](https://github.com/microsoft/amplifier-distro), but it is designed to be general-purpose. The API surface and transport layer are intentionally distro-agnostic, though the project hasn't yet been validated against a wide range of consumers.

Current limitations:

- **Localhost only.** Binds to `127.0.0.1` by default. No authentication or authorization.
- **Single-user.** Not multi-tenant. One user, one machine.
- **Unstable API.** Endpoints, event schemas, and configuration may change without notice.

## Quick Start

Install amplifierd as a tool:

```bash
uv tool install git+https://github.com/microsoft/amplifierd
amplifierd serve
```

The service starts on `http://127.0.0.1:8410`. Verify with:

```bash
curl http://127.0.0.1:8410/health
```

Interactive API docs are at `http://127.0.0.1:8410/docs` (Swagger UI) or `/redoc`. The raw OpenAPI 3.1 schema is at `/openapi.json`.

### Development setup

If you're working on amplifierd itself, use `uv run` from a local checkout:

```bash
cd amplifierd
uv sync --extra dev
uv run amplifierd serve
```

To run the test suite:

```bash
uv run pytest
```

### Configuration

Settings resolve in priority order: CLI flags > environment variables > `~/.amplifierd/settings.json`.

| Setting | Env var | Default | Description |
|---------|---------|---------|-------------|
| `host` | `AMPLIFIERD_HOST` | `127.0.0.1` | Bind address |
| `port` | `AMPLIFIERD_PORT` | `8410` | Bind port |
| `log_level` | `AMPLIFIERD_LOG_LEVEL` | `info` | Logging level |
| `default_working_dir` | `AMPLIFIERD_DEFAULT_WORKING_DIR` | `None` | Default CWD for new sessions |
| `disabled_plugins` | `AMPLIFIERD_DISABLED_PLUGINS` | `[]` | Plugin names to skip |

CLI flags override everything:

```bash
amplifierd serve --host 0.0.0.0 --port 9000 --log-level debug
```

## Architecture

amplifierd depends on [amplifier-core](https://github.com/microsoft/amplifier-core) and [amplifier-foundation](https://github.com/microsoft/amplifier-foundation).

amplifier-core is installed as a versioned PyPI package (`amplifier-core>=1.1.1`). Core is implemented in Rust with Python bindings and published as pre-built wheels, so users do not need a Rust toolchain.

amplifier-foundation is referenced via Git (`git+https://github.com/microsoft/amplifier-foundation`). Foundation is research-focused and its API surface may change significantly. This dependency may evolve toward versioned releases as the ecosystem matures.

## Plugins

Plugins add custom endpoints to the daemon. See [docs/plugins.md](docs/plugins.md) for the full guide on writing, installing, and configuring plugins.

## API Usage

See [docs/api-usage.md](docs/api-usage.md) for the full guide on driving amplifierd over HTTP and SSE, including usage patterns, a Python client wrapper, and the endpoint reference.

## Further Reading

| Document | Description |
|----------|-------------|
| [docs/api-usage.md](docs/api-usage.md) | HTTP and SSE client guide, endpoint reference |
| [docs/plugins.md](docs/plugins.md) | Plugin authoring, security layer, and auth plugin contract |
| [docs/bundles.md](docs/bundles.md) | Bundle management |
| [docs/design/](docs/design/) | Architecture and design docs |
