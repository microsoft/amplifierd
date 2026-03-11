# Extending amplifierd with Plugins

Plugins add custom endpoints to the daemon. A plugin is a Python package that registers a FastAPI router via an entry point. No SDK, no base class -- just one function.

## The plugin contract

A plugin must:

1. Register an entry point in the `amplifierd.plugins` group.
2. Export a `create_router(state) -> fastapi.APIRouter` function.

That's it.

## Minimal plugin example

Create a package with this structure:

```
my-amplifierd-plugin/
  pyproject.toml
  src/my_plugin/__init__.py
```

**`pyproject.toml`**:

```toml
[project]
name = "my-amplifierd-plugin"
version = "0.1.0"
dependencies = ["fastapi"]

[project.entry-points."amplifierd.plugins"]
my-plugin = "my_plugin"

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/my_plugin"]
```

**`src/my_plugin/__init__.py`**:

```python
from fastapi import APIRouter


def create_router(state) -> APIRouter:
    router = APIRouter(prefix="/my-plugin", tags=["my-plugin"])

    @router.get("/hello")
    async def hello():
        return {"message": "Hello from my plugin"}

    return router
```

Install it into the amplifierd tool environment and restart:

```bash
uv tool install --with ./my-amplifierd-plugin git+https://github.com/microsoft/amplifierd
amplifierd serve
```

Your endpoint is now live at `GET /my-plugin/hello`.

If you're running amplifierd from a local checkout during development, use `uv pip install` instead:

```bash
cd amplifierd
uv pip install -e ../my-amplifierd-plugin
uv run amplifierd serve
```

## Accessing daemon state

The `state` argument passed to `create_router` is `app.state` from the FastAPI application. It gives plugins access to everything the daemon manages:

| Attribute | Type | What it gives you |
|-----------|------|-------------------|
| `state.session_manager` | `SessionManager` | Create, lookup, list, and destroy sessions |
| `state.event_bus` | `EventBus` | Publish events, subscribe to SSE streams |
| `state.bundle_registry` | `BundleRegistry` or `None` | Load, prepare, compose bundles |
| `state.settings` | `DaemonSettings` | Read daemon configuration |

**Example -- a plugin that publishes custom events:**

```python
from fastapi import APIRouter, Request


def create_router(state) -> APIRouter:
    router = APIRouter(prefix="/metrics", tags=["metrics"])

    @router.post("/report")
    async def report_metric(request: Request, name: str, value: float):
        event_bus = request.app.state.event_bus
        event_bus.publish(
            session_id="system",
            event_name="metrics:report",
            data={"name": name, "value": value},
        )
        return {"status": "published"}

    return router
```

**Example -- a plugin that wraps session creation with custom logic:**

```python
from fastapi import APIRouter, Request


def create_router(state) -> APIRouter:
    router = APIRouter(prefix="/quick", tags=["quick"])

    @router.post("/ask")
    async def quick_ask(request: Request, prompt: str, bundle: str = "default"):
        manager = request.app.state.session_manager
        registry = request.app.state.bundle_registry

        # Load and prepare the bundle
        loaded = registry.load(bundle)
        prepared = loaded.prepare()

        # Create a session, execute, and tear down
        handle = manager.register(
            session=prepared.create_session(),
            prepared_bundle=prepared,
            bundle_name=bundle,
        )
        try:
            result = await handle.execute(prompt)
            return {"response": str(result)}
        finally:
            await manager.destroy(handle.session_id)

    return router
```

## Disabling plugins

In `~/.amplifierd/settings.json`:

```json
{
    "disabled_plugins": ["my-plugin"]
}
```

Or via environment variable:

```bash
AMPLIFIERD_DISABLED_PLUGINS='["my-plugin"]' amplifierd serve
```

## Plugin resilience

Plugin failures never crash the daemon. If `create_router` raises an exception or returns something that isn't an `APIRouter`, the error is logged and that plugin is skipped. All other plugins and core endpoints continue to work.

## Managing environments with uv

When developing multiple plugins, keep each plugin in its own project with its own `pyproject.toml`. uv makes it straightforward to compose them without dependency conflicts.

**Installing multiple plugins into the tool environment:**

```bash
uv tool install \
  --with ./my-metrics-plugin \
  --with ./my-auth-plugin \
  git+https://github.com/microsoft/amplifierd
```

Each `--with` adds a plugin package to the amplifierd tool environment. All plugins share the same Python environment, so their dependencies must be compatible.

**Isolating plugin development with workspaces:**

If you're developing several plugins that might have conflicting dependencies, give each its own virtualenv and test against amplifierd independently:

```bash
# Plugin A — develop and test in isolation
cd my-metrics-plugin
uv sync
uv run pytest

# Plugin B — separate environment
cd ../my-auth-plugin
uv sync
uv run pytest
```

Then combine them only when installing into the amplifierd tool environment:

```bash
uv tool install \
  --with ./my-metrics-plugin \
  --with ./my-auth-plugin \
  git+https://github.com/microsoft/amplifierd
```

If two plugins pull incompatible versions of a shared dependency, uv will report the conflict at install time rather than at runtime.

## Security layer integration

amplifierd includes a security layer for TLS, remote access, and authentication. Plugins interact with this layer in two ways: auth plugins provide the session verification logic, and all plugins benefit from the TLS/remote infrastructure without needing to handle it themselves.

### TLS and remote access

amplifierd supports TLS natively with three modes, configured via `--tls`:

| Mode | Behavior |
|------|----------|
| `off` | No TLS. Plain HTTP on localhost. |
| `auto` | Waterfall: try Tailscale serve, then Tailscale cert, then self-signed. |
| `manual` | Use certificates you provide via `--ssl-certfile` and `--ssl-keyfile`. |

When Tailscale is connected and `--tls auto` is set, amplifierd will attempt `tailscale serve` first, which proxies HTTPS externally without requiring amplifierd to terminate TLS itself. If that isn't available, it tries `tailscale cert` for a Tailscale-managed certificate, and falls back to generating a self-signed cert in `~/.amplifierd/certs/`.

Plugins don't need to handle TLS -- it's resolved before uvicorn starts and applies to all routes including plugin routes.

An API key can also be required for non-localhost requests via the `--api-key` flag or `AMPLIFIERD_API_KEY` environment variable. When set, remote clients must include `Authorization: Bearer <key>` on every request. Localhost requests bypass this check.

### Authentication plugin contract

amplifierd provides `SessionAuthMiddleware` -- middleware that gates non-public routes behind a session cookie. The middleware itself does not validate sessions. Instead, it defers to an `auth_verify_session` callable that an auth plugin must register on `app.state`.

**What the middleware does:**

1. Checks for an `amplifier_session` cookie on incoming requests.
2. Calls `app.state.auth_verify_session(token)` with the cookie value.
3. If the callable returns a non-None value, the request is authenticated.
4. If the callable is not registered, the middleware **fails open** -- logs a warning and passes all requests through.

**What an auth plugin must do:**

1. Implement `/login`, `/logout`, and `/auth/me` routes (these paths are whitelisted by the middleware).
2. Register a verify callable on `app.state` during `create_router`:

```python
from fastapi import APIRouter, Request, Response


def create_router(state) -> APIRouter:
    router = APIRouter(tags=["auth"])
    sessions: dict[str, dict] = {}

    # Register the session verifier so SessionAuthMiddleware can use it.
    state.auth_verify_session = lambda token: sessions.get(token)

    @router.post("/login")
    async def login(request: Request, response: Response):
        # Authenticate the user (PAM, LDAP, OAuth, etc.) and create a session.
        # Set the session cookie:
        #   response.set_cookie("amplifier_session", token, httponly=True, secure=True)
        ...

    @router.post("/logout")
    async def logout(request: Request, response: Response):
        # Delete session and clear cookie.
        response.delete_cookie("amplifier_session")
        ...

    @router.get("/auth/me")
    async def me(request: Request):
        # Return current user info from the session cookie.
        ...

    return router
```

**Configuration dependency:** The middleware is only mounted when `auth_enabled` is `true` in settings. Users enable it via `AMPLIFIERD_AUTH_ENABLED=true` in the environment or in `~/.amplifierd/settings.json`. The `--no-auth` CLI flag explicitly disables it. Auth is off by default to preserve the existing localhost-only experience.

**Design note:** Authentication is most useful when combined with TLS and remote access. A typical remote setup would use `--tls auto` with an auth plugin and `auth_enabled: true` in settings.

**Reference implementation:** [amplifier-distro](https://github.com/microsoft/amplifier-distro) includes a PAM-based auth plugin at `amplifierd-plugins/amplifierd-plugin-auth/` that implements Linux PAM login, signed session cookies via `itsdangerous`, and the `/login`, `/logout`, `/auth/me` routes.
