# Driving amplifierd over HTTP and SSE

You don't need an SDK to use amplifierd. Any HTTP client works. This page shows the patterns for building a client in any language.

## Core concepts

**Sessions** are the central abstraction. A session wraps an Amplifier agent with its bundle configuration, context history, and module state. You create a session, send prompts to it, and receive responses.

**Two execution modes:**

| Mode | Endpoint | Returns | Best for |
|------|----------|---------|----------|
| Synchronous | `POST /sessions/{id}/execute` | Blocks until done, returns full response | Scripts, simple integrations |
| Streaming | `POST /sessions/{id}/execute/stream` | Returns `202` immediately, events arrive via SSE | UIs, real-time displays |

**All errors** use RFC 7807 Problem Details:

```json
{
    "type": "https://amplifier.dev/errors/session-not-found",
    "title": "Session Not Found",
    "status": 404,
    "detail": "Session 'abc123' not found",
    "instance": "/sessions/abc123"
}
```

## Session lifecycle

```
Create session  -->  Execute prompts  -->  Destroy session
POST /sessions/{id}  POST .../execute   DELETE /sessions/{id}
                     POST .../execute/stream
```

Sessions are created by the bundle and agent management endpoints. Once you have a session ID, you can send prompts to it repeatedly -- each prompt is a new "turn" in the conversation. The session retains context across turns.

## Authentication

amplifierd uses a layered auth model depending on how you deploy it:

**1. Localhost (default)** — No authentication needed. When the daemon is bound to `127.0.0.1` (the default), all requests are accepted without credentials. Only local processes can reach it, so the bind address is the security boundary.

**2. API Key** (`AMPLIFIERD_API_KEY` set) — Every request must include an `Authorization` header:

```
Authorization: Bearer <your-api-key>
```

Requests without this header return `401 Unauthorized`. Set the key via environment variable before starting the daemon:

```bash
AMPLIFIERD_API_KEY=my-secret-key amplifierd serve --host 0.0.0.0
```

**3. Proxy Auth** (`AMPLIFIERD_TRUST_PROXY_AUTH=true`) — When amplifierd is deployed behind a reverse proxy that handles authentication (nginx, Caddy, Tailscale Serve, etc.), the proxy injects an `X-Authenticated-User` header after verifying the user. No client-side auth is needed; the proxy handles it. amplifierd trusts this header only from IP addresses in `AMPLIFIERD_TRUSTED_PROXIES` (defaults to localhost).

---

## Pattern 1: Synchronous request/response

The simplest integration. Send a prompt, wait for the complete response.

```bash
# Execute a prompt (blocks until complete)
curl -X POST http://127.0.0.1:8410/sessions/$SESSION_ID/execute \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $AMPLIFIERD_API_KEY" \
  -d '{"prompt": "What files are in the current directory?"}'
```

Response:

```json
{
    "response": "Here are the files in the current directory: ..."
}
```

**Python example:**

```python
import httpx

BASE = "http://127.0.0.1:8410"

# Execute synchronously
resp = httpx.post(
    f"{BASE}/sessions/{session_id}/execute",
    json={"prompt": "Explain this codebase"},
    timeout=300.0,  # LLM calls can be slow
)
print(resp.json()["response"])
```

**Error handling:**

```python
resp = httpx.post(f"{BASE}/sessions/{session_id}/execute", json={"prompt": "..."})

if resp.status_code == 409:
    # Session is already executing -- try again later
    print("Session busy")
elif resp.status_code == 404:
    # Session doesn't exist
    print("Session not found")
elif resp.status_code == 200:
    print(resp.json()["response"])
```

## Pattern 2: Fire-and-forget with SSE streaming

For UIs and real-time applications. You fire the execution and consume events as they arrive over a separate SSE connection.

**Step 1: Open an SSE connection (before or after firing the prompt):**

```bash
curl -N http://127.0.0.1:8410/events?session=$SESSION_ID
```

**Step 2: Fire the prompt:**

```bash
curl -X POST http://127.0.0.1:8410/sessions/$SESSION_ID/execute/stream \
  -H "Content-Type: application/json" \
  -d '{"prompt": "Analyze this project"}'
```

Response (immediate, `202 Accepted`):

```json
{
    "correlation_id": "prompt_abc123_1",
    "session_id": "abc123"
}
```

**Step 3: Events arrive on the SSE connection:**

```
event: content_block:start
data: {"event": "content_block:start", "session_id": "abc123", "correlation_id": "prompt_abc123_1", "sequence": 0, ...}

event: content_block:delta
data: {"event": "content_block:delta", "data": {"text": "Here are"}, "session_id": "abc123", "correlation_id": "prompt_abc123_1", "sequence": 1, ...}

event: content_block:delta
data: {"event": "content_block:delta", "data": {"text": " the files..."}, "session_id": "abc123", "correlation_id": "prompt_abc123_1", "sequence": 2, ...}

event: content_block:stop
data: {"event": "content_block:stop", "session_id": "abc123", "correlation_id": "prompt_abc123_1", "sequence": 3, ...}
```

**Python example with SSE:**

```python
import httpx
import json
import threading


def stream_events(base: str, session_id: str):
    """Listen for SSE events in a background thread."""
    with httpx.stream("GET", f"{base}/events?session={session_id}") as resp:
        for line in resp.iter_lines():
            if line.startswith("data: "):
                event = json.loads(line[6:])
                event_name = event.get("event", "")
                if "delta" in event_name:
                    text = event.get("data", {}).get("text", "")
                    print(text, end="", flush=True)
                elif event_name == "content_block:stop":
                    print()  # newline after streaming completes


BASE = "http://127.0.0.1:8410"
session_id = "your-session-id"

# Start listening for events
listener = threading.Thread(
    target=stream_events, args=(BASE, session_id), daemon=True
)
listener.start()

# Fire the prompt (returns immediately)
resp = httpx.post(
    f"{BASE}/sessions/{session_id}/execute/stream",
    json={"prompt": "Explain this codebase"},
)
correlation_id = resp.json()["correlation_id"]
print(f"Execution started: {correlation_id}")

# Wait for streaming to finish
listener.join(timeout=300)
```

## SSE event format

Every SSE frame has a consistent envelope:

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

| Field | Description |
|-------|-------------|
| `event` | Event type name (e.g., `content_block:delta`, `tool:pre`, `tool:post`) |
| `data` | Event-specific payload |
| `session_id` | Which session produced this event |
| `correlation_id` | Ties events to the prompt execution that caused them |
| `sequence` | Monotonically increasing per SSE connection (for ordering and gap detection) |
| `timestamp` | UTC ISO-8601 timestamp |

## SSE filtering

Filter the event stream with query parameters:

```bash
# Events from one session (and its child sessions automatically)
GET /events?session=abc123

# Events matching glob patterns
GET /events?filter=content_block:*,tool:*

# Combine both
GET /events?session=abc123&filter=content_block:*
```

When you filter by `session`, you automatically receive events from all child sessions (spawned agents) -- the session-tree propagation is built in.

## Pattern 3: Agent delegation (spawn/resume)

Spawn child agent sessions from a parent, just like the `delegate` tool does:

```bash
# Spawn a child agent
curl -X POST http://127.0.0.1:8410/sessions/$PARENT_ID/spawn \
  -H "Content-Type: application/json" \
  -d '{"agent": "foundation:explorer", "prompt": "Survey the codebase"}'

# Response includes the child session ID
# {"session_id": "child-123", "agent": "foundation:explorer", ...}

# Resume the child with follow-up
curl -X POST http://127.0.0.1:8410/sessions/$PARENT_ID/spawn/child-123/resume \
  -H "Content-Type: application/json" \
  -d '{"prompt": "Now check the tests too"}'
```

Child session events automatically propagate to the parent's SSE stream, so a single `GET /events?session=$PARENT_ID` captures the entire agent tree.

## Pattern 4: Bundle management

Register, load, and prepare bundles before creating sessions:

```bash
# List registered bundles
curl http://127.0.0.1:8410/bundles

# Register a bundle by name and URI
curl -X POST http://127.0.0.1:8410/bundles/register \
  -H "Content-Type: application/json" \
  -d '{"name": "my-bundle", "uri": "file:///path/to/bundle.md"}'

# Load and inspect a bundle
curl -X POST http://127.0.0.1:8410/bundles/load \
  -H "Content-Type: application/json" \
  -d '{"name": "my-bundle"}'

# Check for updates
curl -X POST http://127.0.0.1:8410/bundles/my-bundle/check-updates
```

## Pattern 5: Context and module management

Read and manipulate a session's conversation history:

```bash
# Get conversation messages
curl http://127.0.0.1:8410/sessions/$SESSION_ID/context/messages

# Inject a system message
curl -X POST http://127.0.0.1:8410/sessions/$SESSION_ID/context/messages \
  -H "Content-Type: application/json" \
  -d '{"role": "system", "content": "You are a code reviewer."}'

# Clear conversation history
curl -X DELETE http://127.0.0.1:8410/sessions/$SESSION_ID/context/messages
```

Hot-mount modules into a running session:

```bash
# List available modules
curl http://127.0.0.1:8410/modules

# Mount a module into a session
curl -X POST http://127.0.0.1:8410/sessions/$SESSION_ID/modules/mount \
  -H "Content-Type: application/json" \
  -d '{"module_id": "tool-bash"}'

# List what's mounted
curl http://127.0.0.1:8410/sessions/$SESSION_ID/modules
```

## Pattern 6: Approval gates

Some agent workflows pause for human approval. `approval:required` events arrive on the SSE stream (`GET /events?session={id}`), and you respond via REST:

```bash
# Check for pending approvals
curl http://127.0.0.1:8410/sessions/$SESSION_ID/approvals

# Approve a pending request
curl -X POST http://127.0.0.1:8410/sessions/$SESSION_ID/approvals/$REQUEST_ID \
  -H "Content-Type: application/json" \
  -d '{"approved": true, "message": "Looks good, proceed"}'
```

## Building a client wrapper

Here's a minimal Python wrapper showing the core patterns. This is not an SDK -- it's a reference for building your own integration in any language.

```python
"""Minimal amplifierd client wrapper."""

import json
from collections.abc import Iterator

import httpx


class AmplifierdClient:
    def __init__(
        self,
        base_url: str = "http://127.0.0.1:8410",
        api_key: str | None = None,
    ):
        self.base = base_url
        headers = {}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        self.http = httpx.Client(base_url=base_url, timeout=300.0, headers=headers)

    # -- Health --------------------------------------------------------

    def health(self) -> dict:
        return self.http.get("/health").json()

    # -- Sessions ------------------------------------------------------

    def list_sessions(self) -> list[dict]:
        return self.http.get("/sessions").json()["sessions"]

    def get_session(self, session_id: str) -> dict:
        return self.http.get(f"/sessions/{session_id}").json()

    def delete_session(self, session_id: str) -> None:
        self.http.delete(f"/sessions/{session_id}")

    # -- Synchronous execution -----------------------------------------

    def execute(self, session_id: str, prompt: str) -> str | None:
        resp = self.http.post(
            f"/sessions/{session_id}/execute",
            json={"prompt": prompt},
        )
        resp.raise_for_status()
        return resp.json().get("response")

    # -- Streaming execution -------------------------------------------

    def execute_stream(self, session_id: str, prompt: str) -> str:
        """Fire a prompt and return the correlation_id."""
        resp = self.http.post(
            f"/sessions/{session_id}/execute/stream",
            json={"prompt": prompt},
        )
        resp.raise_for_status()
        return resp.json()["correlation_id"]

    def stream_events(
        self,
        session_id: str | None = None,
        filter: str | None = None,
    ) -> Iterator[dict]:
        """Yield parsed SSE events. Blocks until the connection closes."""
        params = {}
        if session_id:
            params["session"] = session_id
        if filter:
            params["filter"] = filter

        with httpx.stream(
            "GET", f"{self.base}/events", params=params
        ) as resp:
            for line in resp.iter_lines():
                if line.startswith("data: "):
                    yield json.loads(line[6:])

    # -- Cancel --------------------------------------------------------

    def cancel(self, session_id: str, immediate: bool = False) -> dict:
        resp = self.http.post(
            f"/sessions/{session_id}/cancel",
            json={"immediate": immediate},
        )
        return resp.json()

    def close(self) -> None:
        self.http.close()
```

Usage:

```python
# Localhost (no auth needed)
client = AmplifierdClient()

# Network-exposed with API key
client = AmplifierdClient("http://my-server:8410", api_key="my-secret-key")

# Synchronous
response = client.execute(session_id, "What is this project?")
print(response)

# Streaming
correlation_id = client.execute_stream(session_id, "Analyze this codebase")
for event in client.stream_events(session_id=session_id):
    if event.get("event") == "content_block:delta":
        print(event["data"].get("text", ""), end="")
    if event.get("correlation_id") != correlation_id:
        continue  # filter to our execution
```

## Tips for client implementations in other languages

**JavaScript/TypeScript:** Use the native `EventSource` API for SSE, or `fetch` with `ReadableStream` for more control. The `correlation_id` in the 202 response lets you filter your event handler to only process events from your execution.

**Go:** Use `bufio.Scanner` on the response body from a `GET /events` request. Parse lines starting with `data: ` as JSON.

**Any language:** The pattern is always the same:
1. Open a long-lived `GET /events` connection (SSE).
2. `POST .../execute/stream` to fire a prompt (get back `correlation_id`).
3. Match incoming SSE events by `correlation_id`.
4. Assemble deltas into the full response on the client side.

---

## Endpoint Reference

The full OpenAPI schema is always available at `GET /openapi.json`. Here is a quick reference:

### Health & Info

| Method | Path | Description |
|--------|------|-------------|
| GET | `/health` | Health status, uptime, active session count |
| GET | `/info` | Version, capabilities, module types |
| GET | `/openapi.json` | OpenAPI 3.1 schema |
| GET | `/docs` | Swagger UI |
| GET | `/redoc` | ReDoc |

### Sessions

| Method | Path | Description |
|--------|------|-------------|
| GET | `/sessions` | List all sessions |
| GET | `/sessions/{id}` | Session detail |
| PATCH | `/sessions/{id}` | Update session (e.g., working_dir) |
| DELETE | `/sessions/{id}` | Destroy session |
| POST | `/sessions/{id}/execute` | Synchronous execution |
| POST | `/sessions/{id}/execute/stream` | Fire-and-forget (202 + SSE) |
| POST | `/sessions/{id}/cancel` | Cancel current execution |
| POST | `/sessions/{id}/stale` | Mark for bundle reload |
| GET | `/sessions/{id}/tree` | Recursive session tree |

### Events

| Method | Path | Description |
|--------|------|-------------|
| GET | `/events` | Global SSE stream (filterable) |

### Approvals

| Method | Path | Description |
|--------|------|-------------|
| GET | `/sessions/{id}/approvals` | List pending approvals |
| POST | `/sessions/{id}/approvals/{req_id}` | Respond to an approval |

### Agents

| Method | Path | Description |
|--------|------|-------------|
| POST | `/sessions/{id}/spawn` | Spawn child agent session |
| POST | `/sessions/{id}/spawn/stream` | Spawn with SSE streaming |
| POST | `/sessions/{id}/spawn/{child_id}/resume` | Resume child agent |
| GET | `/sessions/{id}/agents` | List available agents |

### Bundles

| Method | Path | Description |
|--------|------|-------------|
| GET | `/bundles` | List registered bundles |
| POST | `/bundles/register` | Register name to URI |
| DELETE | `/bundles/{name}` | Unregister |
| POST | `/bundles/load` | Load and inspect |
| POST | `/bundles/prepare` | Prepare for session creation |
| POST | `/bundles/compose` | Compose multiple bundles |
| POST | `/bundles/{name}/check-updates` | Check for updates |
| POST | `/bundles/{name}/update` | Update to latest |

### Context

| Method | Path | Description |
|--------|------|-------------|
| GET | `/sessions/{id}/context/messages` | Get conversation messages |
| POST | `/sessions/{id}/context/messages` | Inject a message |
| PUT | `/sessions/{id}/context/messages` | Replace all messages |
| DELETE | `/sessions/{id}/context/messages` | Clear context |

### Modules

| Method | Path | Description |
|--------|------|-------------|
| GET | `/modules` | Discover available modules |
| GET | `/modules/{id}` | Module detail |
| POST | `/sessions/{id}/modules/mount` | Hot-mount a module |
| POST | `/sessions/{id}/modules/unmount` | Unmount a module |
| GET | `/sessions/{id}/modules` | List mounted modules |

### Fork & History

| Method | Path | Description |
|--------|------|-------------|
| POST | `/sessions/{id}/fork` | Fork session at a turn |
| GET | `/sessions/{id}/fork/preview` | Preview fork result |
| GET | `/sessions/{id}/turns` | List turn boundaries |
| GET | `/sessions/{id}/lineage` | Fork ancestry chain |
| GET | `/sessions/{id}/forks` | List child forks |

### Validation & Reload

| Method | Path | Description |
|--------|------|-------------|
| POST | `/validate/mount-plan` | Validate mount plan |
| POST | `/validate/module` | Validate module compliance |
| POST | `/validate/bundle` | Validate a bundle |
| POST | `/reload/bundles` | Reload all bundles |
| GET | `/reload/status` | Check for available updates |
