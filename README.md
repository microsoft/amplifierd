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
| `tls_mode` | `AMPLIFIERD_TLS_MODE` | `off` | TLS mode: `off`, `auto` (Tailscale), or `manual` (supply cert/key) |
| `tls_certfile` | `AMPLIFIERD_TLS_CERTFILE` | `None` | Path to TLS certificate file (implies `--tls manual`) |
| `tls_keyfile` | `AMPLIFIERD_TLS_KEYFILE` | `None` | Path to TLS private key file (implies `--tls manual`) |
| `auth_enabled` | `AMPLIFIERD_AUTH_ENABLED` | `false` | Require authentication on all endpoints |
| `trust_proxy_auth` | `AMPLIFIERD_TRUST_PROXY_AUTH` | `false` | Trust `X-Authenticated-User` header from upstream proxy |
| `trusted_proxies` | `AMPLIFIERD_TRUSTED_PROXIES` | `["127.0.0.1","::1"]` | IP addresses allowed to set forwarded headers |
| `cookie_secure` | `AMPLIFIERD_COOKIE_SECURE` | `auto` | Set `Secure` on cookies: `auto`, `true`, or `false` |
| `cookie_samesite` | `AMPLIFIERD_COOKIE_SAMESITE` | `lax` | `SameSite` cookie attribute: `lax`, `strict`, or `none` |
| `api_key` | `AMPLIFIERD_API_KEY` | `None` | Static API key required in `Authorization: Bearer` header |
| `allowed_origins` | `AMPLIFIERD_ALLOWED_ORIGINS` | `["*"]` | CORS allowed origins list |

For deployment modes (localhost, network-exposed, behind proxy) and detailed security configuration, see [docs/HOSTING.md](docs/HOSTING.md).

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

## Contributing

> [!NOTE]
> This project is not currently accepting external contributions, but we're actively working toward opening this up. We value community input and look forward to collaborating in the future. For now, feel free to fork and experiment!

Most contributions require you to agree to a
Contributor License Agreement (CLA) declaring that you have the right to, and actually do, grant us
the rights to use your contribution. For details, visit [Contributor License Agreements](https://cla.opensource.microsoft.com).

When you submit a pull request, a CLA bot will automatically determine whether you need to provide
a CLA and decorate the PR appropriately (e.g., status check, comment). Simply follow the instructions
provided by the bot. You will only need to do this once across all repos using our CLA.

This project has adopted the [Microsoft Open Source Code of Conduct](https://opensource.microsoft.com/codeofconduct/).
For more information see the [Code of Conduct FAQ](https://opensource.microsoft.com/codeofconduct/faq/) or
contact [opencode@microsoft.com](mailto:opencode@microsoft.com) with any additional questions or comments.

## Trademarks

This project may contain trademarks or logos for projects, products, or services. Authorized use of Microsoft
trademarks or logos is subject to and must follow
[Microsoft's Trademark & Brand Guidelines](https://www.microsoft.com/legal/intellectualproperty/trademarks/usage/general).
Use of Microsoft trademarks or logos in modified versions of this project must not cause confusion or imply Microsoft sponsorship.
Any use of third-party trademarks or logos are subject to those third-party's policies.
