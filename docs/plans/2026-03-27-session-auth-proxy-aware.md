# SessionAuthMiddleware Proxy-Aware Localhost Bypass — Implementation Plan

> **Execution:** Use the subagent-driven-development workflow to implement this plan.

**Goal:** Make `SessionAuthMiddleware` use the same `_resolve_client_ip` pattern as `ApiKeyMiddleware` so that `X-Forwarded-For` from trusted proxies is honoured and spoofed localhost headers from untrusted sources are rejected.

**Architecture:** The existing `_resolve_client_ip` helper (already used by `ApiKeyMiddleware`) is reused inside `SessionAuthMiddleware.dispatch`. The middleware resolves the real client IP before checking the localhost bypass, then falls through to session-cookie verification for remote clients. Browser requests without a valid session get a 302 redirect to `/login`; API clients get a 401 JSON response.

**Tech Stack:** Python · FastAPI/Starlette · pytest · Starlette `TestClient`

---

> **STATUS: Implementation appears complete.** Both source files already contain the
> spec-matching code and all 20 tests pass (`uv run pytest tests/test_security_middleware.py -v`).
> The spec review loop exhausted after 3 iterations because an earlier iteration added a
> `with patch(...)` mock to `test_genuine_localhost_bypasses_session_auth` that was not in the spec.
> That mock has since been removed and the code now matches the spec. This plan documents the
> intended changes for auditability and to guide any re-implementation if needed.

---

## Critical Pitfall

**Do NOT mock `is_localhost` in the SessionAuthMiddleware tests.**

Starlette's `TestClient` sets `request.client.host` to the string `"testclient"`, which is not a valid IP. The `_resolve_client_ip` function treats non-IP `direct_ip` values as implicitly trusted (like a local proxy), so:

- **Without** `X-Forwarded-For`: `_resolve_client_ip("testclient", None, ...)` returns `"testclient"`, and `is_localhost("testclient")` returns `True` (non-IP values are treated as local). The localhost bypass fires naturally.
- **With** `X-Forwarded-For: 203.0.113.50`: `_resolve_client_ip("testclient", "203.0.113.50", ...)` returns `"203.0.113.50"`, and `is_localhost("203.0.113.50")` returns `False`. The request is treated as remote.

This means the spec's tests work as-is with no mocking. Adding `with patch("...is_localhost", return_value=True)` defeats the purpose of the test and violates the spec.

---

### Task 1: Add `_make_session_auth_app` test helper

**Files:**
- Modify: `amplifierd/tests/test_security_middleware.py` (append after line 130)

**Step 1: Write the helper function**

Add the following after the `TestResolveClientIp` class (after line 130):

```python
def _make_session_auth_app() -> FastAPI:
    from amplifierd.security.middleware import SessionAuthMiddleware

    app = FastAPI()
    app.state.trusted_proxies = {"127.0.0.1", "::1"}
    app.state.auth_verify_session = lambda token: "testuser" if token.startswith("valid-") else None
    app.add_middleware(SessionAuthMiddleware)

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.get("/dashboard")
    async def dashboard():
        return {"page": "dashboard"}

    return app
```

Key details:
- Import `SessionAuthMiddleware` inside the function (not at module level) to avoid circular issues and match the spec exactly.
- `trusted_proxies` must include `"127.0.0.1"` and `"::1"` — same set used for `_resolve_client_ip`.
- `auth_verify_session` returns `"testuser"` for tokens starting with `"valid-"`, `None` otherwise.

**Step 2: Verify the file still imports correctly**

Run: `cd /home/robotdad/Work/distro/amplifierd && uv run python -c "import tests.test_security_middleware"`
Expected: No output (clean import).

---

### Task 2: Add `TestSessionAuthMiddlewareProxyAware` test class

**Files:**
- Modify: `amplifierd/tests/test_security_middleware.py` (append after the helper)

**Step 1: Write the failing tests**

Add the following after `_make_session_auth_app`:

```python
@pytest.mark.unit
class TestSessionAuthMiddlewareProxyAware:
    def test_remote_client_via_proxy_requires_session(self):
        app = _make_session_auth_app()
        client = TestClient(app)
        resp = client.get("/dashboard", headers={"X-Forwarded-For": "203.0.113.50"})
        assert resp.status_code in (401, 302)

    def test_genuine_localhost_bypasses_session_auth(self):
        app = _make_session_auth_app()
        client = TestClient(app)
        resp = client.get("/dashboard")
        assert resp.status_code == 200

    def test_public_paths_bypass_for_remote_via_proxy(self):
        app = _make_session_auth_app()
        client = TestClient(app)
        resp = client.get("/health", headers={"X-Forwarded-For": "203.0.113.50"})
        assert resp.status_code == 200
```

**IMPORTANT: No mocks.** The test bodies are exactly three lines each. Do not add `with patch(...)` or any other wrapper. See the "Critical Pitfall" section above for why this works without mocking.

**Step 2: Run tests to verify they fail (middleware not yet updated)**

Run: `cd /home/robotdad/Work/distro/amplifierd && uv run pytest tests/test_security_middleware.py::TestSessionAuthMiddlewareProxyAware -v`
Expected: `test_remote_client_via_proxy_requires_session` — FAIL (old dispatch doesn't resolve proxy IP)
Expected: `test_genuine_localhost_bypasses_session_auth` — may PASS (depends on pre-existing dispatch)
Expected: `test_public_paths_bypass_for_remote_via_proxy` — may PASS (public paths already bypassed)

---

### Task 3: Update `SessionAuthMiddleware.dispatch` with proxy-aware bypass

**Files:**
- Modify: `amplifierd/src/amplifierd/security/middleware.py` — replace the `dispatch` method body in `SessionAuthMiddleware` (the method starting at line ~89 or wherever the class currently defines it)

**Step 1: Replace the dispatch method**

The full `SessionAuthMiddleware` class should look like this:

```python
class SessionAuthMiddleware(BaseHTTPMiddleware):
    """Enforce session cookie authentication for all non-public routes.

    The auth plugin registers a ``verify_session`` callable on
    ``app.state.auth_verify_session`` at startup.  This middleware reads that
    callable on every request so the secret is resolved after the plugin has
    fully initialised.

    Bypass order:
    1. Auth paths (/login, /logout, /auth/me, /favicon.svg) -> always pass
    2. Public paths (/health, /info, /docs, /redoc, /openapi.json) -> always pass
    3. Static assets (/static/*) -> always pass
    4. Localhost (resolved via _resolve_client_ip) -> always pass
    5. Valid ``amplifier_session`` cookie -> pass
    6. HTML-accepting clients -> redirect to /login
    7. Otherwise -> 401 JSON
    """

    async def dispatch(self, request: Request, call_next) -> Response:  # type: ignore[no-untyped-def]
        path = request.url.path
        if path in _AUTH_PATHS or path in _PUBLIC_PATHS or path.startswith("/static/"):
            return await call_next(request)
        direct_ip = request.client.host if request.client else None
        trusted_proxies = getattr(request.app.state, "trusted_proxies", set())
        forwarded_for = request.headers.get("x-forwarded-for")
        client_ip = _resolve_client_ip(direct_ip, forwarded_for, trusted_proxies)
        if is_localhost(client_ip):
            return await call_next(request)
        verify = getattr(request.app.state, "auth_verify_session", None)
        if verify is None:
            logger.warning("SessionAuthMiddleware active but auth_verify_session not set")
            return await call_next(request)
        session_token = request.cookies.get(_SESSION_COOKIE)
        if session_token is not None and verify(session_token) is not None:
            return await call_next(request)
        logger.debug("Unauthenticated request to %s from %s", path, client_ip)
        if "text/html" in request.headers.get("accept", ""):
            from urllib.parse import quote

            return RedirectResponse(url=f"/login?next={quote(path, safe='/')}", status_code=302)
        return JSONResponse(status_code=401, content={"detail": "Authentication required"})
```

Key changes from a naive implementation:
- `_resolve_client_ip` is called with `direct_ip`, the `x-forwarded-for` header, and `trusted_proxies` from `app.state`.
- The localhost check uses the resolved `client_ip`, not the raw `request.client.host`.
- The `from urllib.parse import quote` import is inside the conditional branch (lazy import).

**Step 2: Run all tests to verify they pass**

Run: `cd /home/robotdad/Work/distro/amplifierd && uv run pytest tests/test_security_middleware.py -v`
Expected: **20 passed** — all existing tests plus the 3 new ones.

**Step 3: Commit**

```
git add src/amplifierd/security/middleware.py tests/test_security_middleware.py
git commit -m "fix(amplifierd): add proxy-aware localhost bypass to SessionAuthMiddleware"
```

---

## Verification

Run the full acceptance criteria command:

```bash
cd /home/robotdad/Work/distro/amplifierd && uv run pytest tests/test_security_middleware.py -v
```

Expected output includes:

```
tests/test_security_middleware.py::TestSessionAuthMiddlewareProxyAware::test_remote_client_via_proxy_requires_session PASSED
tests/test_security_middleware.py::TestSessionAuthMiddlewareProxyAware::test_genuine_localhost_bypasses_session_auth PASSED
tests/test_security_middleware.py::TestSessionAuthMiddlewareProxyAware::test_public_paths_bypass_for_remote_via_proxy PASSED

20 passed
```
