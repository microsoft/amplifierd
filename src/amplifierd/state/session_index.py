from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from dataclasses import asdict, dataclass
from dataclasses import fields as dc_fields
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class SessionIndexEntry:
    session_id: str
    status: str
    bundle: str
    created_at: str
    last_activity: str
    parent_session_id: str | None = None
    project_id: str = ""


_ENTRY_FIELDS = {f.name for f in dc_fields(SessionIndexEntry)}


class SessionIndex:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._entries: dict[str, SessionIndexEntry] = {}
        self._save_lock = threading.Lock()

    def add(self, entry: SessionIndexEntry) -> None:
        self._entries[entry.session_id] = entry

    def update(self, session_id: str, **fields: object) -> bool:
        unknown = set(fields) - _ENTRY_FIELDS
        if unknown:
            raise ValueError(f"Unknown SessionIndexEntry fields: {unknown}")
        if session_id not in self._entries:
            return False
        for k, v in fields.items():
            setattr(self._entries[session_id], k, v)
        return True

    def remove(self, session_id: str) -> None:
        self._entries.pop(session_id, None)

    def get(self, session_id: str) -> SessionIndexEntry | None:
        return self._entries.get(session_id)

    def list_entries(self) -> list[SessionIndexEntry]:
        return list(self._entries.values())

    def save(self) -> None:
        with self._save_lock:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            # Snapshot entries to avoid "dictionary changed size during iteration"
            # when add() is called concurrently from the event loop.
            entries_snapshot = list(self._entries.values())
            data = [asdict(e) for e in entries_snapshot]
            # Use a unique temp file to prevent races between concurrent save() callers
            # (the lock serializes within one process, but unique names are safer).
            fd, tmp_name = tempfile.mkstemp(dir=self._path.parent, suffix=".tmp", prefix=".index-")
            try:
                with os.fdopen(fd, "w") as f:
                    json.dump(data, f, indent=2)
                os.replace(tmp_name, str(self._path))
            except BaseException:
                try:
                    os.unlink(tmp_name)
                except OSError:
                    pass
                raise

    @classmethod
    def load(cls, path: Path) -> SessionIndex:
        index = cls(path)
        if not path.exists():
            return index
        try:
            data = json.loads(path.read_text())
            for item in data:
                # Tolerate old entries that pre-date project_id field
                item.setdefault("project_id", "")
                index._entries[item["session_id"]] = SessionIndexEntry(**item)
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            logger.warning("Session index corrupted at %s, starting empty", path)
        return index

    @classmethod
    def rebuild(cls, projects_dir: Path) -> SessionIndex:
        """Rebuild index by walking projects_dir/<project>/ sessions/<session>/ layout."""
        index_path = projects_dir / "index.json"
        index = cls(index_path)
        if not projects_dir.exists():
            return index
        for project_dir in projects_dir.iterdir():
            if not project_dir.is_dir():
                continue
            sessions_subdir = project_dir / "sessions"
            if not sessions_subdir.is_dir():
                continue
            for session_dir in sessions_subdir.iterdir():
                if not session_dir.is_dir():
                    continue
                meta_path = session_dir / "metadata.json"
                if not meta_path.exists():
                    continue
                try:
                    meta = json.loads(meta_path.read_text())
                    index.add(
                        SessionIndexEntry(
                            session_id=session_dir.name,
                            status=meta.get("status", "completed"),
                            bundle=meta.get("bundle", "unknown"),
                            created_at=meta.get("created_at", ""),
                            last_activity=meta.get("last_activity", meta.get("created_at", "")),
                            parent_session_id=meta.get("parent_session_id"),
                            project_id=project_dir.name,
                        )
                    )
                except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                    logger.warning("Skipping unreadable session dir: %s", session_dir)
        return index
