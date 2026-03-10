"""CLI entry point for amplifierd."""

from __future__ import annotations

import logging

import click

_LOG_LEVELS: dict[str, int] = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warning": logging.WARNING,
    "error": logging.ERROR,
}


@click.group()
def main() -> None:
    """amplifierd – Amplifier daemon HTTP server."""


@main.command()
@click.option("--host", default=None, type=str, help="Bind host address.")
@click.option("--port", default=None, type=int, help="Bind port number.")
@click.option("--reload", is_flag=True, default=False, help="Enable hot-reload for development.")
@click.option(
    "--log-level",
    default=None,
    type=click.Choice(["debug", "info", "warning", "error"], case_sensitive=False),
    help="Log level (overrides AMPLIFIERD_LOG_LEVEL).",
)
@click.option(
    "--bundle",
    "-b",
    multiple=True,
    help="Register a bundle as NAME=URI (repeatable).",
)
@click.option(
    "--default-bundle",
    default=None,
    type=str,
    help="Default bundle name for sessions created without one.",
)
@click.option(
    "--api-key",
    default=None,
    type=str,
    help="Require API key for non-localhost requests.",
)
@click.option(
    "--tls",
    "tls_mode",
    default=None,
    type=click.Choice(["auto", "off", "manual"], case_sensitive=False),
    help="TLS mode: auto (Tailscale/self-signed), manual, off.",
)
@click.option(
    "--ssl-certfile",
    default=None,
    type=click.Path(),
    help="Path to SSL certificate (implies --tls manual).",
)
@click.option(
    "--ssl-keyfile",
    default=None,
    type=click.Path(),
    help="Path to SSL private key.",
)
@click.option(
    "--no-auth",
    is_flag=True,
    default=False,
    help="Disable authentication even when TLS is active.",
)
def serve(
    host: str | None,
    port: int | None,
    reload: bool,
    log_level: str | None,
    bundle: tuple[str, ...],
    default_bundle: str | None,
    api_key: str | None,
    tls_mode: str | None,
    ssl_certfile: str | None,
    ssl_keyfile: str | None,
    no_auth: bool,
) -> None:
    """Start the amplifierd HTTP server."""
    import json
    import os

    import uvicorn

    from amplifierd.config import DaemonSettings

    # Push CLI bundle overrides into env so DaemonSettings in the lifespan picks them up
    if bundle:
        parsed: dict[str, str] = {}
        for b in bundle:
            if "=" not in b:
                raise click.BadParameter(f"Expected NAME=URI, got: {b}", param_hint="--bundle")
            name, uri = b.split("=", 1)
            parsed[name] = uri
        existing = json.loads(os.environ.get("AMPLIFIERD_BUNDLES", "{}"))
        existing.update(parsed)
        os.environ["AMPLIFIERD_BUNDLES"] = json.dumps(existing)

    if default_bundle is not None:
        os.environ["AMPLIFIERD_DEFAULT_BUNDLE"] = default_bundle

    if api_key is not None:
        os.environ["AMPLIFIERD_API_KEY"] = api_key

    if tls_mode is not None:
        os.environ["AMPLIFIERD_TLS_MODE"] = tls_mode
    if ssl_certfile is not None:
        os.environ["AMPLIFIERD_TLS_CERTFILE"] = ssl_certfile
        if tls_mode is None:
            os.environ["AMPLIFIERD_TLS_MODE"] = "manual"
    if ssl_keyfile is not None:
        os.environ["AMPLIFIERD_TLS_KEYFILE"] = ssl_keyfile
    if no_auth:
        os.environ["AMPLIFIERD_AUTH_ENABLED"] = "false"

    settings = DaemonSettings()

    effective_host = host if host is not None else settings.host
    effective_port = port if port is not None else settings.port
    effective_log_level = log_level if log_level is not None else settings.log_level

    # 1. Console logging (always available before daemon session dir)
    logging.basicConfig(
        level=_LOG_LEVELS.get(effective_log_level.lower(), logging.INFO),
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )

    # 2. Create daemon session directory and route all output to serve.log
    from amplifierd.daemon_session import create_session_dir, setup_session_log

    session_path = create_session_dir(
        settings.daemon_run_dir,
        host=effective_host,
        port=effective_port,
        log_level=effective_log_level,
    )
    setup_session_log(session_path)

    # Store the daemon session path in env so the app lifespan can pick it up
    os.environ["AMPLIFIERD_DAEMON_SESSION_PATH"] = str(session_path)

    # 3. Resolve TLS configuration (Tailscale probe + cert resolution)
    from amplifierd.security.tls import resolve_tls

    ssl_kwargs = resolve_tls(settings, effective_port)

    click.echo(
        f"amplifierd starting – host={effective_host} port={effective_port} "
        f"log-level={effective_log_level}"
    )

    uvicorn.run(
        "amplifierd.app:create_app",
        host=effective_host,
        port=effective_port,
        reload=reload,
        log_level=effective_log_level,
        factory=True,
        **ssl_kwargs,
    )


if __name__ == "__main__":
    main()
