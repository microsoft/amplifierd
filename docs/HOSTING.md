# Hosting amplifierd

This guide covers how to deploy amplifierd in different network topologies,
how TLS and authentication interact, and how to configure cookies and proxy
trust correctly.

---

## Deployment Modes

amplifierd has three canonical deployment modes. Each trades off security for
simplicity, so choose the one that matches your threat model.

### 1. Localhost (Default)

**Suitable for:** single-user development on your own machine.

```sh
amplifierd serve
```

| Setting       | Value             |
|---------------|-------------------|
| Bind address  | `127.0.0.1:8410`  |
| TLS           | off               |
| Auth          | off               |
| Cookies       | not used          |

The daemon only accepts connections from the same machine. No TLS, no API key,
no cookies — the OS network stack is the security boundary.

> **Port auto-increment:** If port 8410 is already occupied, amplifierd
> automatically tries 8411, 8412, … up to 10 candidates. It prints a message
> when it starts on a different port. Pass `--port` to pin a specific port and
> get an error rather than silent increment.

---

### 2. Network-Exposed

**Suitable for:** a shared team server, a home lab, or a machine accessed over
Tailscale from other devices.

```sh
amplifierd serve --host 0.0.0.0
```

| Setting       | Value                                  |
|---------------|----------------------------------------|
| Bind address  | `0.0.0.0:8410`                         |
| TLS           | auto (Tailscale → self-signed fallback) |
| Auth          | enabled (`AMPLIFIERD_API_KEY`)         |
| Cookie secure | `true`                                 |
| Cookie SameSite | `lax`                               |

When you bind to `0.0.0.0`, amplifierd detects the non-localhost binding at
startup and enables TLS automatically (`--tls auto`). You should also set an
API key so that only authorized clients can reach it:

```sh
AMPLIFIERD_API_KEY=mysecretkey amplifierd serve --host 0.0.0.0
```

Clients must pass the key in the `Authorization` header:

```
Authorization: Bearer mysecretkey
```

---

### 3. Behind a Reverse Proxy

**Suitable for:** production deployments where nginx / Caddy / Traefik handles
TLS termination and (optionally) user authentication.

```sh
# amplifierd stays on loopback; the proxy fronts it
AMPLIFIERD_TRUST_PROXY_AUTH=true amplifierd serve
```

| Setting             | Value                                          |
|---------------------|------------------------------------------------|
| Bind address        | `127.0.0.1:8410`                               |
| TLS                 | off (proxy terminates)                         |
| Auth                | delegated to proxy (`AMPLIFIERD_TRUST_PROXY_AUTH=true`) |
| Trusted proxies     | `127.0.0.1`, `::1` (default)                  |

The reverse proxy should inject an `X-Authenticated-User` header and forward
it to amplifierd. Because amplifierd listens only on loopback, only the proxy
(running on the same machine) can reach it.

---

## TLS Modes

Control TLS with `AMPLIFIERD_TLS_MODE` or the `--tls` flag.

| Mode     | Default? | Description |
|----------|----------|-------------|
| `off`    | ✓        | No TLS. Safe for localhost. Do not use over a routable network. |
| `auto`   |          | Probes for Tailscale certificates first; falls back to a self-signed certificate. Activated automatically when `--host 0.0.0.0` is used. |
| `manual` |          | User supplies certificate and key files via `AMPLIFIERD_TLS_CERTFILE` and `AMPLIFIERD_TLS_KEYFILE`. |

### Manual TLS example

```sh
amplifierd serve \
  --tls manual \
  --ssl-certfile /etc/amplifierd/cert.pem \
  --ssl-keyfile  /etc/amplifierd/key.pem
```

Or with environment variables:

```sh
AMPLIFIERD_TLS_MODE=manual \
AMPLIFIERD_TLS_CERTFILE=/etc/amplifierd/cert.pem \
AMPLIFIERD_TLS_KEYFILE=/etc/amplifierd/key.pem \
  amplifierd serve --host 0.0.0.0
```

---

## Proxy Deployment

### Trusted Proxies

`AMPLIFIERD_TRUSTED_PROXIES` is a comma-separated list of IP addresses whose
`X-Forwarded-For` headers amplifierd will trust for client IP resolution.
By default, it contains only localhost (`127.0.0.1`, `::1`) so that a proxy
running on the same machine works without explicit configuration.

```sh
# Trust a specific upstream load balancer
AMPLIFIERD_TRUSTED_PROXIES="10.0.0.1" amplifierd serve
```

If a request arrives from an IP that is **not** in the trusted list,
`X-Forwarded-For` is ignored and the direct connection IP is used as the
client address.

### Proxy Auth Trust

When `AMPLIFIERD_TRUST_PROXY_AUTH=true`, amplifierd reads the
`X-Authenticated-User` header from requests arriving from trusted proxies and
treats its value as the authenticated username. This lets you delegate
authentication entirely to the reverse proxy (e.g., via OAuth2 Proxy or
Authentik):

```
X-Authenticated-User: alice@example.com
```

amplifierd will accept this header **only** from IPs listed in
`AMPLIFIERD_TRUSTED_PROXIES`.

> **Security warning:** Enabling `AMPLIFIERD_TRUST_PROXY_AUTH=true` while
> relying solely on the default `trusted_proxies` (localhost) is safe for
> same-machine proxies, but you should explicitly set
> `AMPLIFIERD_TRUSTED_PROXIES` when your proxy runs on a different host.
> amplifierd logs a warning at startup when `trust_proxy_auth` is enabled
> without an explicit `trusted_proxies` value being set, to remind you to
> verify your proxy topology.

### Example: nginx + OAuth2 Proxy

```nginx
location / {
    proxy_pass http://127.0.0.1:8410;

    # Pass the authenticated user header
    proxy_set_header X-Authenticated-User $auth_user;

    # Let amplifierd see the real client IP
    proxy_set_header X-Forwarded-For $remote_addr;
    proxy_set_header X-Forwarded-Proto $scheme;
}
```

```sh
AMPLIFIERD_TRUST_PROXY_AUTH=true \
AMPLIFIERD_TRUSTED_PROXIES="127.0.0.1" \
  amplifierd serve
```

---

## Cookie Behavior

amplifierd uses cookies for browser-based session continuity when auth is
enabled. Two environment variables control the cookie attributes:

| Env var                   | Default  | Values                    | Description |
|---------------------------|----------|---------------------------|-------------|
| `AMPLIFIERD_COOKIE_SECURE`   | `auto`   | `auto`, `true`, `false`  | When `auto`, the `Secure` flag is set if TLS is active. Set `true` to always require HTTPS; `false` to never set it (not recommended in production). |
| `AMPLIFIERD_COOKIE_SAMESITE` | `lax`    | `lax`, `strict`, `none`  | `lax` is the recommended default: it allows cookies on top-level navigations (e.g., clicking a link) but blocks cross-site subrequest cookies, protecting against CSRF while keeping most workflows functional. Use `strict` for maximum CSRF protection or `none` (with `Secure`) for explicit cross-origin embedding. |

**Why `lax` as the default?**
`strict` would break OAuth redirect flows where the browser navigates from the
identity provider back to amplifierd. `none` requires `Secure=true` and opens
CSRF risk. `lax` is the broadly accepted default for web application cookies.

---

## Port Auto-Increment

When you start amplifierd without an explicit `--port`, it tries port 8410
first. If that port is already bound (e.g., another amplifierd instance is
running), it automatically tries 8411, 8412, and so on — up to 10 candidates.

```
Port 8410 is already in use — starting on 8411 instead.
Use --port to set a specific port.
```

To prevent auto-increment and get a clear error instead, pass `--port 8410`
explicitly. This is useful in scripts or systemd units where you want a
predictable startup failure rather than silent port drift.

The auto-increment search binds to `127.0.0.1` regardless of the `--host`
value, so it correctly detects port conflicts even when the daemon will
ultimately bind to `0.0.0.0`.

---

## Configuration Reference

All settings can be provided as environment variables (prefix `AMPLIFIERD_`)
or in `~/.amplifierd/settings.json` (same key names, lowercase, underscores).
CLI flags override environment variables.

| Setting                     | Env var                         | Default                | Description |
|-----------------------------|----------------------------------|------------------------|-------------|
| `host`                      | `AMPLIFIERD_HOST`                | `127.0.0.1`            | Bind address. Set to `0.0.0.0` to accept remote connections. |
| `port`                      | `AMPLIFIERD_PORT`                | `8410`                 | Bind port. Auto-increments if occupied (unless `--port` is explicit). |
| `log_level`                 | `AMPLIFIERD_LOG_LEVEL`           | `info`                 | Log verbosity: `debug`, `info`, `warning`, `error`. |
| `tls_mode`                  | `AMPLIFIERD_TLS_MODE`            | `off`                  | TLS mode: `off`, `auto`, `manual`. |
| `tls_certfile`              | `AMPLIFIERD_TLS_CERTFILE`        | _(none)_               | Path to TLS certificate file (required for `manual` TLS). |
| `tls_keyfile`               | `AMPLIFIERD_TLS_KEYFILE`         | _(none)_               | Path to TLS private key file (required for `manual` TLS). |
| `auth_enabled`              | `AMPLIFIERD_AUTH_ENABLED`        | `false`                | Enable API key authentication. |
| `api_key`                   | `AMPLIFIERD_API_KEY`             | _(none)_               | Required API key value. Activates auth when set. |
| `allowed_origins`           | `AMPLIFIERD_ALLOWED_ORIGINS`     | `["*"]`                | CORS allowed origins list. |
| `trusted_proxies`           | `AMPLIFIERD_TRUSTED_PROXIES`     | `["127.0.0.1","::1"]`  | IPs trusted for `X-Forwarded-For` headers. |
| `trust_proxy_auth`          | `AMPLIFIERD_TRUST_PROXY_AUTH`    | `false`                | Accept `X-Authenticated-User` from trusted proxies. |
| `cookie_secure`             | `AMPLIFIERD_COOKIE_SECURE`       | `auto`                 | Cookie `Secure` flag: `auto` (follows TLS state), `true`, or `false`. |
| `cookie_samesite`           | `AMPLIFIERD_COOKIE_SAMESITE`     | `lax`                  | Cookie `SameSite` attribute: `lax`, `strict`, or `none`. |
| `home_dir`                  | `AMPLIFIERD_HOME_DIR`            | `~/.amplifierd`        | Directory for daemon state, session logs, and plugins. |
| `projects_dir`              | `AMPLIFIERD_PROJECTS_DIR`        | `~/.amplifier/projects`| Root directory for Amplifier project state. |
| `default_working_dir`       | `AMPLIFIERD_DEFAULT_WORKING_DIR` | _(none)_               | Default working directory for new sessions. |
| `default_bundle`            | `AMPLIFIERD_DEFAULT_BUNDLE`      | `distro`               | Bundle used when no bundle is specified at session creation. |
| `home_redirect`             | `AMPLIFIERD_HOME_REDIRECT`       | _(none)_               | URL path to redirect `/` to (e.g., `/distro/`). |
| `disabled_plugins`          | `AMPLIFIERD_DISABLED_PLUGINS`    | `[]`                   | List of plugin names to disable at startup. |
| `daemon_session_path`       | `AMPLIFIERD_DAEMON_SESSION_PATH` | _(auto)_               | Override path for the daemon session log directory. |
