# amplifierd: The Amplifier daemon

amplifierd is a localhost HTTP daemon that exposes amplifier-core and amplifier-foundation capabilities over REST and SSE. It lets you drive Amplifier sessions from any language or framework that can make HTTP calls.

Under the hood, amplifierd is a thin HTTP layer on top of two libraries:

- **[amplifier-core](../amplifier-core/)** — the agent runtime: sessions, LLM providers, tool execution, hooks, and the event system. amplifierd wires core events (content deltas, tool calls, approval requests) into its SSE transport and uses `HookResult` for tool-approval gates.
- **[amplifier-foundation](../amplifier-foundation/)** — higher-level orchestration: bundle loading/preparation, child-session spawning, session forking, and working-directory management. amplifierd delegates bundle lifecycle to `BundleRegistry` and agent delegation to `create_child_session`.

amplifierd itself adds HTTP routing, the `SessionManager`/`EventBus` state layer, plugin discovery, and streaming transport — but all agent logic lives in the libraries.

## Quick Start

Install amplifierd as a tool:

```bash
uv tool install git+https://github.com/microsoft/amplifierd
amplifierd serve
```

The daemon starts on `http://127.0.0.1:8410`. Verify with:

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

---

## Plugins

Plugins add custom endpoints to the daemon. See [docs/plugins.md](docs/plugins.md) for the full guide on writing, installing, and configuring plugins.

---

## API Usage

See [docs/api-usage.md](docs/api-usage.md) for the full guide on driving amplifierd over HTTP and SSE, including usage patterns, a Python client wrapper, and the endpoint reference.
