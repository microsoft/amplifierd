"""Daemon session directory — per-run server logs and metadata.

Each ``amplifierd serve`` invocation creates a new session directory under
``~/.amplifierd/sessions/{uuid}/`` containing:

- ``meta.json`` — daemon run metadata (pid, port, host, start_time, plugins)
- ``serve.log`` — all server output for this run (Python logging + stdout/stderr)

This is distinct from Amplifier *conversation* sessions stored at
``~/.amplifier/sessions/``, which hold ``transcript.jsonl`` and ``metadata.json``.

Follows the pattern established by amplifier-distro's ``session_dir.py``.

Related: https://github.com/microsoft/amplifier-distro/issues/176
"""

from __future__ import annotations

import io
import json
import logging
import os
import shutil
import sys
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

META_FILENAME = "meta.json"
LOG_FILENAME = "serve.log"


class _TeeWriter(io.TextIOBase):
    """File-like wrapper that writes to both the original stream and a log file.

    Ensures all raw output (uvicorn access logs, tracebacks, print() calls)
    lands in the per-session ``serve.log`` alongside Python logging output.
    """

    def __init__(self, original: io.TextIOBase, log_file: io.TextIOBase) -> None:
        super().__init__()
        self._original = original
        self._log_file = log_file
        self._lock = threading.Lock()

    def write(self, s: str) -> int:
        with self._lock:
            self._original.write(s)
            self._original.flush()
            try:
                self._log_file.write(s)
                self._log_file.flush()
            except (OSError, ValueError):
                pass
        return len(s)

    def flush(self) -> None:
        with self._lock:
            self._original.flush()
            try:
                self._log_file.flush()
            except (OSError, ValueError):
                pass

    def fileno(self) -> int:
        raise io.UnsupportedOperation("fileno")

    def isatty(self) -> bool:
        return self._original.isatty()

    def writable(self) -> bool:
        return True

    @property
    def encoding(self) -> str:
        return getattr(self._original, "encoding", None) or "utf-8"

    @property
    def name(self) -> str:
        return getattr(self._original, "name", "<tee>")

    def close(self) -> None:
        self._log_file.close()


def prune_old_sessions(daemon_run_dir: Path, keep: int = 50) -> int:
    """Remove oldest session directories, keeping the most recent `keep`."""
    if not daemon_run_dir.exists():
        return 0
    sessions = sorted(
        [p for p in daemon_run_dir.iterdir() if p.is_dir()],
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    removed = 0
    for old in sessions[keep:]:
        shutil.rmtree(old, ignore_errors=True)
        removed += 1
    if removed:
        logger.info("Pruned %d old daemon session(s)", removed)
    return removed


def create_session_dir(
    daemon_run_dir: Path,
    *,
    host: str,
    port: int,
    log_level: str,
    plugins: list[str] | None = None,
) -> Path:
    """Create a new daemon session directory with ``meta.json``.

    Args:
        daemon_run_dir: Parent directory (e.g. ``~/.amplifierd/sessions/``).
        host: Bind address.
        port: Bind port.
        log_level: Effective log level.
        plugins: List of discovered plugin names (updated later if not known yet).

    Returns:
        Path to the created session directory.
    """
    prune_old_sessions(daemon_run_dir)

    session_id = str(uuid.uuid4())
    session_path = daemon_run_dir / session_id
    session_path.mkdir(parents=True, exist_ok=True)
    session_path.chmod(0o700)

    try:
        import amplifierd

        version = amplifierd.__version__
    except Exception:
        version = "unknown"

    meta: dict[str, Any] = {
        "session_id": session_id,
        "pid": os.getpid(),
        "host": host,
        "port": port,
        "log_level": log_level,
        "start_time": datetime.now(tz=UTC).isoformat(),
        "plugins": plugins or [],
        "status": "starting",
        "version": version,
    }

    meta_path = session_path / META_FILENAME
    meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
    meta_path.chmod(0o600)

    logger.info("Daemon session created: %s", session_path)
    return session_path


def update_session_meta(session_path: Path, updates: dict[str, Any]) -> None:
    """Merge updates into an existing ``meta.json`` atomically."""
    meta_path = session_path / META_FILENAME
    try:
        existing = json.loads(meta_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return
    existing.update(updates)
    tmp_path = meta_path.with_suffix(".tmp")
    tmp_path.write_text(json.dumps(existing, indent=2), encoding="utf-8")
    tmp_path.chmod(0o600)
    os.replace(tmp_path, meta_path)


def setup_session_log(session_path: Path, log_level: int = logging.DEBUG) -> None:
    """Attach a file handler and tee writers to capture all output for this run.

    After calling this function:

    1. All Python ``logging`` output goes to ``serve.log`` via a StreamHandler
       on the root logger.
    2. All raw ``sys.stdout`` writes (uvicorn access logs, ``click.echo()``)
       go to ``serve.log`` via a TeeWriter.
    3. All raw ``sys.stderr`` writes (tracebacks, uvicorn errors) go to
       ``serve.log`` via a TeeWriter.

    Args:
        session_path: The daemon session directory containing ``serve.log``.
        log_level: Minimum level for the file handler (default: DEBUG to
            capture everything regardless of console level).
    """
    log_path = session_path / LOG_FILENAME

    # Open one shared file handle for all output
    shared_file = open(log_path, "a", encoding="utf-8")  # noqa: SIM115
    os.chmod(log_path, 0o600)

    # 1. Python logging StreamHandler → serve.log (shared file handle)
    handler = logging.StreamHandler(shared_file)
    handler.setLevel(log_level)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-8s [%(name)s] %(message)s"))
    logging.getLogger().addHandler(handler)

    # 2. Tee stdout → serve.log
    sys.stdout = _TeeWriter(sys.stdout, shared_file)  # type: ignore[assignment]

    # 3. Tee stderr → serve.log
    sys.stderr = _TeeWriter(sys.stderr, shared_file)  # type: ignore[assignment]

    logger.info("Session log active: %s", log_path)
