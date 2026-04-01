# Building Apps on amplifierd

This guide covers the recommended pattern for building an app on top of amplifierd — one that can run standalone as its own command and also be composed into a larger system like amplifier-distro as a plugin.

---

## The pattern in brief

Your app is a **plugin that ships its own CLI**. The plugin half lets it be composed into any amplifierd-based host. The CLI half lets it run independently without requiring the user to set up anything else. The same `create_router(state)` entry point serves both cases — no conditional logic, no separate code paths.

---

## Project structure

```
my-app/
├── src/my_app/
│   ├── __init__.py       # exports create_router(state) -> APIRouter
│   ├── cli.py            # standalone entry point — boots amplifierd
│   ├── routes.py         # your FastAPI routes
│   └── ...
├── pyproject.toml
└── README.md
```

---

## pyproject.toml

```toml
[project]
name = "my-app"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = [
    "fastapi>=0.115",
    "pydantic>=2.0",
    # your app's own dependencies — NOT amplifierd
]

[project.optional-dependencies]
standalone = [
    "amplifierd @ git+https://github.com/microsoft/amplifierd",
    "uvicorn[standard]>=0.30",
    "click>=8.0",
]

[project.scripts]
my-app = "my_app.cli:main"

[project.entry-points."amplifierd.plugins"]
my-app = "my_app"

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"
```

Two things to note:

**`amplifierd` is NOT a base dependency.** It goes in the `[standalone]` optional extra. Your plugin code only imports from amplifierd if it's present — which it will be in both standalone mode (you pulled it in via the extra) and plugin mode (the host already has it). Keeping it out of base dependencies means a host system can install your plugin without getting a redundant copy of amplifierd.

**The entry point registers unconditionally.** Whenever your package is installed in a Python environment, `my-app = "my_app"` is registered in `amplifierd.plugins`. amplifierd discovers every entry point in its venv on startup — so your routes load automatically whether the user installed you as a standalone tool or as a plugin into an existing amplifierd installation.

---

## The plugin: `__init__.py`

```python
from fastapi import APIRouter

def create_router(state) -> APIRouter:
    router = APIRouter(prefix="/my-app", tags=["my-app"])

    @router.get("/")
    async def index():
        return {"app": "my-app"}

    session_manager = state.session_manager
    event_bus = state.event_bus

    # ... register your routes ...

    return router
```

All your routes live under a single prefix that matches your app name. Pick it once and don't change it — it becomes part of your public API.

---

## The CLI: `cli.py`

```python
import click
import uvicorn

from amplifierd.config import DaemonSettings
from amplifierd.daemon_session import create_session_dir, setup_session_log
from amplifierd.port_utils import find_available_port

@click.command()
@click.option("--host", default=None)
@click.option("--port", default=None, type=int, help="Bind port. Defaults to 8410.")
@click.option("--log-level", default=None)
def main(host, port, log_level):
    """my-app — standalone."""
    import os

    settings = DaemonSettings()
    effective_host = host or settings.host
    effective_log_level = log_level or settings.log_level

    if port is not None:
        effective_port = port
    else:
        effective_port, was_incremented = find_available_port(settings.port)
        if was_incremented:
            click.echo(
                f"Port {settings.port} is already in use — "
                f"starting on {effective_port} instead.\n"
                f"Use --port to set a specific port."
            )

    session_path = create_session_dir(
        settings.daemon_run_dir,
        host=effective_host,
        port=effective_port,
        log_level=effective_log_level,
    )
    setup_session_log(session_path)
    os.environ["AMPLIFIERD_DAEMON_SESSION_PATH"] = str(session_path)

    click.echo(f"my-app starting — http://{effective_host}:{effective_port}/my-app/")

    uvicorn.run(
        "amplifierd.app:create_app",
        host=effective_host,
        port=effective_port,
        log_level=effective_log_level,
        factory=True,
    )
```

The call to `find_available_port` uses the default from `DaemonSettings` (respecting `AMPLIFIERD_PORT` if set). When `--port` is passed explicitly the value is used as-is — you were deliberate, the OS error stands.

---

## Installation and isolation

### Standalone (recommended for end users)

```bash
uv tool install my-app --from "git+https://github.com/your-org/my-app[standalone]"
my-app
```

`uv tool install` creates an **isolated virtual environment** for each tool. In that environment, the only registered `amplifierd.plugins` entry point is yours. amplifierd discovers exactly one plugin: yours. Your routes and only your routes are mounted.

This is the isolation guarantee: **one tool environment, one app's entry points.**

### As a plugin into an existing amplifierd installation

```bash
uv tool install amplifierd \
  --from git+https://github.com/microsoft/amplifierd \
  --with git+https://github.com/your-org/my-app
amplifierd serve
```

Your package is added to the `amplifierd` tool environment. Your entry point registers there alongside anything else in that environment.

Use this when the user wants to add your plugin to an existing bare amplifierd setup. Do not present this as the standalone install path — it modifies the shared `amplifierd` tool environment rather than creating an isolated one for your app.

### As part of a composed distribution (distro-style)

A distribution like amplifier-distro explicitly lists your package as a dependency and ships all its plugins together as a single product. All those plugins intentionally share state, sessions, and routes as parts of one system.

```toml
# distro-service/pyproject.toml
dependencies = [
    "amplifierd",
    "my-app",
    "another-app",
]
```

Your entry point registers alongside the others. This is correct — the distribution is one product.

---

## Running two standalone apps at the same time

Each standalone app starts its own amplifierd instance. amplifierd defaults to port `8410` and auto-increments if that port is occupied, announcing the change:

```
Port 8410 is already in use — starting on 8411 instead.
Use --port to set a specific port.
```

To pin a specific port:

```bash
amplifier-voice --port 8411
# or
AMPLIFIERD_PORT=8411 amplifier-voice
```

The apps share no state, routes, or sessions when running as separate processes.

---

## What causes route bleed

Route bleed — seeing another app's routes when you only intended to run yours — happens when **two apps' packages are installed in the same Python environment**. amplifierd's plugin discovery loads every `amplifierd.plugins` entry point it finds in the venv. That is what makes the composed-distribution pattern work. It also means that if multiple standalone apps end up in the same environment, their routes all appear together.

The safest approach: always use `uv tool install` with your app's own tool name and the `[standalone]` extra. That creates a dedicated environment with only your app's entry points.

What to avoid:

```bash
# ❌ Installs voice and chat into the shared `amplifierd` tool env together
uv tool install amplifierd \
  --with amplifier-voice \
  --with amplifier-chat

# ✅ Each app installs into its own named, isolated tool environment
uv tool install amplifierd-plugin-voice \
  --from "git+https://github.com/microsoft/amplifier-voice[standalone]"

uv tool install amplifier-chat \
  --from "git+https://github.com/microsoft/amplifier-chat[standalone]"
```

---

## Hosting considerations for app authors

amplifierd handles the hosting layer so you don't have to. These are the three areas where app authors most commonly try to reinvent what's already built.

### Don't implement your own auth

When a request arrives through a reverse proxy with `AMPLIFIERD_TRUST_PROXY_AUTH=true` enabled, amplifierd reads the `X-Authenticated-User` header and makes the value available on `request.state.authenticated_user`. In localhost mode (no proxy), `request.state.authenticated_user` is `None`.

Your routes should read this value rather than implement their own authentication:

```python
from fastapi import Request

@router.get("/profile")
async def profile(request: Request):
    user = request.state.authenticated_user  # str | None
    if user is None:
        # localhost mode — no auth enforced
        return {"user": "anonymous"}
    return {"user": user}
```

Never add a separate login flow, token check, or session cookie system in your app. The host handles that. Your code just reads the value that's already there.

### Smart defaults from `--host 0.0.0.0`

When the server is started with `--host 0.0.0.0` (network-exposed mode), amplifierd automatically enables TLS when available (via Tailscale or user-supplied certs) and activates auth enforcement when `AMPLIFIERD_TRUST_PROXY_AUTH` is set. You do not need to toggle any of this from your app. The right behaviour is wired to the host binding — your plugin code stays the same regardless of how the server is started.

### Port auto-increment

amplifierd defaults to port `8410` and automatically increments to find an available port if that one is in use. Your CLI should use `find_available_port` from `amplifierd.port_utils` rather than hardcoding a port or writing your own availability check — the CLI example above shows the correct pattern.

---

## Checklist

- [ ] `amplifierd` is in `[standalone]` optional deps, not base deps
- [ ] Entry point registered under `[project.entry-points."amplifierd.plugins"]`
- [ ] All routes are under a single prefix matching your app name
- [ ] CLI calls `uvicorn.run("amplifierd.app:create_app", factory=True)`
- [ ] CLI uses `find_available_port` from `amplifierd.port_utils` when no `--port` is passed
- [ ] README shows `uv tool install my-app --from "...[standalone]"` as the primary install path
- [ ] README documents how to change the port if needed
- [ ] App reads `request.state.authenticated_user` instead of implementing its own auth
- [ ] App works correctly in both localhost (no auth) and behind-proxy (proxy auth) modes
