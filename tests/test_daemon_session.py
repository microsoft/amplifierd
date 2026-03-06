"""Tests for daemon session directory and logging."""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

import pytest


@pytest.mark.unit
class TestCreateSessionDir:
    """Tests for create_session_dir: directory creation and meta.json."""

    def test_creates_directory_and_meta(self, tmp_path: Path):
        """A new UUID directory is created with a valid meta.json."""
        from amplifierd.daemon_session import create_session_dir

        session_path = create_session_dir(
            tmp_path, host="127.0.0.1", port=8410, log_level="info"
        )

        assert session_path.exists()
        assert session_path.parent == tmp_path

        meta_path = session_path / "meta.json"
        assert meta_path.exists()

        meta = json.loads(meta_path.read_text())
        assert meta["host"] == "127.0.0.1"
        assert meta["port"] == 8410
        assert meta["log_level"] == "info"
        assert meta["pid"] > 0
        assert meta["plugins"] == []
        assert "session_id" in meta
        assert "start_time" in meta

    def test_session_id_is_directory_name(self, tmp_path: Path):
        """The session_id in meta.json matches the directory name."""
        from amplifierd.daemon_session import create_session_dir

        session_path = create_session_dir(
            tmp_path, host="0.0.0.0", port=9000, log_level="debug"
        )

        meta = json.loads((session_path / "meta.json").read_text())
        assert meta["session_id"] == session_path.name

    def test_creates_parent_directories(self, tmp_path: Path):
        """Parent directories are created if they don't exist."""
        from amplifierd.daemon_session import create_session_dir

        deep_path = tmp_path / "a" / "b" / "sessions"
        session_path = create_session_dir(
            deep_path, host="127.0.0.1", port=8410, log_level="info"
        )

        assert session_path.exists()

    def test_plugins_recorded_in_meta(self, tmp_path: Path):
        """Plugin names are written to meta.json when provided."""
        from amplifierd.daemon_session import create_session_dir

        session_path = create_session_dir(
            tmp_path,
            host="127.0.0.1",
            port=8410,
            log_level="info",
            plugins=["chat", "metrics"],
        )

        meta = json.loads((session_path / "meta.json").read_text())
        assert meta["plugins"] == ["chat", "metrics"]

    def test_each_call_creates_unique_directory(self, tmp_path: Path):
        """Multiple calls create distinct session directories."""
        from amplifierd.daemon_session import create_session_dir

        kwargs = {"host": "127.0.0.1", "port": 8410, "log_level": "info"}
        p1 = create_session_dir(tmp_path, **kwargs)
        p2 = create_session_dir(tmp_path, **kwargs)

        assert p1 != p2
        assert p1.exists()
        assert p2.exists()


@pytest.mark.unit
class TestUpdateSessionMeta:
    """Tests for update_session_meta: merging updates into meta.json."""

    def test_merges_updates(self, tmp_path: Path):
        """Updates are merged into the existing meta.json."""
        from amplifierd.daemon_session import create_session_dir, update_session_meta

        session_path = create_session_dir(
            tmp_path, host="127.0.0.1", port=8410, log_level="info"
        )

        update_session_meta(session_path, {"plugins": ["chat", "metrics"]})

        meta = json.loads((session_path / "meta.json").read_text())
        assert meta["plugins"] == ["chat", "metrics"]
        # Original fields preserved
        assert meta["host"] == "127.0.0.1"
        assert meta["port"] == 8410

    def test_noop_when_no_meta(self, tmp_path: Path):
        """Does nothing when meta.json doesn't exist (no crash)."""
        from amplifierd.daemon_session import update_session_meta

        # tmp_path exists but has no meta.json
        update_session_meta(tmp_path, {"plugins": ["chat"]})
        assert not (tmp_path / "meta.json").exists()


@pytest.mark.unit
class TestSetupSessionLog:
    """Tests for setup_session_log: FileHandler and TeeWriter wiring."""

    def test_creates_serve_log(self, tmp_path: Path):
        """setup_session_log creates a serve.log file."""
        from amplifierd.daemon_session import setup_session_log

        # Save originals to restore after test
        orig_stdout = sys.stdout
        orig_stderr = sys.stderr
        orig_handlers = logging.getLogger().handlers[:]

        try:
            setup_session_log(tmp_path)

            log_path = tmp_path / "serve.log"
            assert log_path.exists()
        finally:
            # Restore originals
            sys.stdout = orig_stdout
            sys.stderr = orig_stderr
            root = logging.getLogger()
            root.handlers = orig_handlers

    def test_python_logging_reaches_serve_log(self, tmp_path: Path):
        """Python logging output is written to serve.log via the FileHandler."""
        from amplifierd.daemon_session import setup_session_log

        orig_stdout = sys.stdout
        orig_stderr = sys.stderr
        orig_handlers = logging.getLogger().handlers[:]

        try:
            setup_session_log(tmp_path)

            test_logger = logging.getLogger("test.daemon_session")
            test_logger.setLevel(logging.DEBUG)
            test_logger.info("MARKER_LOG_LINE_12345")

            # Flush all handlers
            for h in logging.getLogger().handlers:
                h.flush()

            log_content = (tmp_path / "serve.log").read_text()
            assert "MARKER_LOG_LINE_12345" in log_content
        finally:
            sys.stdout = orig_stdout
            sys.stderr = orig_stderr
            root = logging.getLogger()
            root.handlers = orig_handlers

    def test_stdout_teed_to_serve_log(self, tmp_path: Path):
        """Raw sys.stdout writes are captured in serve.log."""
        from amplifierd.daemon_session import setup_session_log

        orig_stdout = sys.stdout
        orig_stderr = sys.stderr
        orig_handlers = logging.getLogger().handlers[:]

        try:
            setup_session_log(tmp_path)

            sys.stdout.write("MARKER_STDOUT_67890\n")
            sys.stdout.flush()

            log_content = (tmp_path / "serve.log").read_text()
            assert "MARKER_STDOUT_67890" in log_content
        finally:
            sys.stdout = orig_stdout
            sys.stderr = orig_stderr
            root = logging.getLogger()
            root.handlers = orig_handlers