"""Session persistence — transcript and metadata hooks.

Registers hooks on tool:post and orchestrator:complete that write
transcript.jsonl and metadata.json incrementally during execution.
Ported from distro-server's transcript_persistence.py and
metadata_persistence.py.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_PRIORITY = 900
_EXCLUDED_ROLES = frozenset({"system", "developer"})
_TRANSCRIPT_FILENAME = "transcript.jsonl"
_METADATA_FILENAME = "metadata.json"

# Resolve sanitize_message once at import time.
try:
    from amplifier_foundation import sanitize_message as _foundation_sanitize
except ImportError:
    _foundation_sanitize = None  # type: ignore[assignment]

# Resolve write_with_backup once at import time.
try:
    from amplifier_foundation import write_with_backup as _write_with_backup
except ImportError:
    _write_with_backup = None  # type: ignore[assignment]


def _sanitize(msg: dict[str, Any]) -> dict[str, Any]:
    """Sanitize a message for JSON persistence.

    Preserves content:null on tool-call messages (providers need it).
    """
    had_content_null = "content" in msg and msg["content"] is None
    sanitized = _foundation_sanitize(msg) if _foundation_sanitize is not None else msg
    if had_content_null and "content" not in sanitized:
        sanitized["content"] = None
    return sanitized


def _atomic_write(path: Path, content: str) -> None:
    """Write content atomically using foundation's write_with_backup or fallback."""
    if _write_with_backup is not None:
        _write_with_backup(path, content)
    else:
        tmp = path.with_suffix(".tmp")
        tmp.write_text(content, encoding="utf-8")
        tmp.replace(path)


def write_transcript(session_dir: Path, messages: list[dict[str, Any]]) -> None:
    """Write messages to transcript.jsonl, filtering system/developer roles.

    Full rewrite (not append) — context compaction can change earlier messages.
    """
    lines: list[str] = []
    for msg in messages:
        try:
            msg_dict = (
                msg if isinstance(msg, dict) else getattr(msg, "model_dump", lambda _m=msg: _m)()
            )
            if msg_dict.get("role") in _EXCLUDED_ROLES:
                continue
            sanitized = _sanitize(msg_dict)
            lines.append(json.dumps(sanitized, ensure_ascii=False))
        except Exception:  # noqa: BLE001
            logger.debug("Skipping unserializable message", exc_info=True)

    content = "\n".join(lines) + "\n" if lines else ""
    session_dir.mkdir(parents=True, exist_ok=True)
    _atomic_write(session_dir / _TRANSCRIPT_FILENAME, content)


def write_metadata(session_dir: Path, metadata: dict[str, Any]) -> None:
    """Write metadata dict to metadata.json, merging with existing content."""
    if not session_dir.exists():
        return
    metadata_path = session_dir / _METADATA_FILENAME

    existing: dict[str, Any] = {}
    if metadata_path.exists():
        try:
            existing = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass

    merged = {**existing, **metadata}
    content = json.dumps(merged, indent=2, ensure_ascii=False)
    _atomic_write(metadata_path, content)


def load_transcript(session_dir: Path) -> list[dict[str, Any]]:
    """Load messages from transcript.jsonl in a session directory.

    Returns a list of message dicts.  Raises :class:`FileNotFoundError`
    if the transcript file does not exist.
    """
    transcript_path = session_dir / _TRANSCRIPT_FILENAME
    if not transcript_path.exists():
        raise FileNotFoundError(f"No transcript at {transcript_path}")
    messages: list[dict[str, Any]] = []
    for line in transcript_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                messages.append(json.loads(line))
            except json.JSONDecodeError:
                logger.debug("Skipping unreadable transcript line")
    return messages


def load_metadata(session_dir: Path) -> dict[str, Any]:
    """Load metadata.json from a session directory.

    Returns an empty dict if the file doesn't exist or is unreadable.
    """
    metadata_path = session_dir / _METADATA_FILENAME
    if not metadata_path.exists():
        return {}
    try:
        return json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


class TranscriptSaveHook:
    """Persists transcript.jsonl incrementally during execution.

    Registered on tool:post (mid-turn durability) and
    orchestrator:complete (end-of-turn, catches no-tool turns).
    Debounces by message count.
    """

    def __init__(self, session: Any, session_dir: Path) -> None:
        self._session = session
        self._session_dir = session_dir
        self._last_count = 0

    async def __call__(self, event: str, data: dict[str, Any]) -> Any:
        from amplifier_core.models import HookResult

        try:
            # Workaround: tool:post fires before context update.
            # Yielding one tick lets the orchestrator add the result first.
            if event == "tool:post":
                await asyncio.sleep(0)

            context = self._session.coordinator.get("context")
            if not context or not hasattr(context, "get_messages"):
                return HookResult(action="continue")

            messages = await context.get_messages()
            count = len(messages)

            if count <= self._last_count:
                return HookResult(action="continue")

            await asyncio.to_thread(write_transcript, self._session_dir, list(messages))
            self._last_count = count

        except Exception:  # noqa: BLE001
            logger.warning("Transcript save failed", exc_info=True)

        return HookResult(action="continue")


class MetadataSaveHook:
    """Writes metadata.json on orchestrator:complete.

    Flushes initial metadata on first fire, then updates turn_count
    and last_updated on every subsequent turn.
    """

    def __init__(
        self,
        session: Any,
        session_dir: Path,
        initial_metadata: dict[str, Any] | None = None,
    ) -> None:
        self._session = session
        self._session_dir = session_dir
        self._initial_metadata = initial_metadata

    async def __call__(self, event: str, data: dict[str, Any]) -> Any:
        from amplifier_core.models import HookResult

        try:
            context = self._session.coordinator.get("context")
            if not context or not hasattr(context, "get_messages"):
                return HookResult(action="continue")

            messages = await context.get_messages()
            turn_count = sum(1 for m in messages if isinstance(m, dict) and m.get("role") == "user")

            updates: dict[str, Any] = {
                "turn_count": turn_count,
                "last_updated": datetime.now(tz=UTC).isoformat(),
            }

            if self._initial_metadata is not None:
                updates = {**self._initial_metadata, **updates}
                self._initial_metadata = None

            write_metadata(self._session_dir, updates)

            # Bridge: emit prompt:complete so hooks-session-naming fires.
            # Some orchestrators (e.g. loop-streaming) only emit
            # orchestrator:complete but not prompt:complete.
            session_id = getattr(self._session, "session_id", None)
            if session_id:
                await self._session.coordinator.hooks.emit(
                    "prompt:complete",
                    {**data, "session_id": session_id},
                )
        except Exception:  # noqa: BLE001
            logger.warning("Metadata save failed", exc_info=True)

        return HookResult(action="continue")


def register_persistence_hooks(
    session: Any,
    session_dir: Path,
    initial_metadata: dict[str, Any] | None = None,
) -> None:
    """Register transcript and metadata persistence hooks on a session.

    Silently no-ops if hooks API is unavailable.
    """
    try:
        transcript_hook = TranscriptSaveHook(session, session_dir)
        metadata_hook = MetadataSaveHook(session, session_dir, initial_metadata)
        hooks = session.coordinator.hooks

        hooks.register(
            event="tool:post",
            handler=transcript_hook,
            priority=_PRIORITY,
            name="amplifierd-transcript:tool:post",
        )
        hooks.register(
            event="orchestrator:complete",
            handler=transcript_hook,
            priority=_PRIORITY,
            name="amplifierd-transcript:orchestrator:complete",
        )
        hooks.register(
            event="orchestrator:complete",
            handler=metadata_hook,
            priority=_PRIORITY,
            name="amplifierd-metadata:orchestrator:complete",
        )
        logger.debug("Persistence hooks registered -> %s", session_dir)
    except Exception:  # noqa: BLE001
        logger.debug("Could not register persistence hooks", exc_info=True)
