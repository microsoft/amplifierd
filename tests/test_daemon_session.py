"""Tests for daemon session directory and logging."""

from __future__ import annotations

import io
import json
import logging
import os
import sys
from pathlib import Path

import pytest


@pytest.mark.unit
class TestCreateSessionDir:
    """Tests for create_session_dir: directory creation and meta.json."""

    def test_creates_directory_and_meta(self, tmp_path: Path):
        """A new UUID directory is created with a valid meta.json."""
        from amplifierd.daemon_session import create_session_dir

        session_path = create_session_dir(tmp_path, host="127.0.0.1", port=8410, log_level="info")

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

        session_path = create_session_dir(tmp_path, host="0.0.0.0", port=9000, log_level="debug")

        meta = json.loads((session_path / "meta.json").read_text())
        assert meta["session_id"] == session_path.name

    def test_creates_parent_directories(self, tmp_path: Path):
        """Parent directories are created if they don't exist."""
        from amplifierd.daemon_session import create_session_dir

        deep_path = tmp_path / "a" / "b" / "sessions"
        session_path = create_session_dir(deep_path, host="127.0.0.1", port=8410, log_level="info")

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

    def test_meta_contains_status_starting(self, tmp_path: Path):
        """meta.json includes status='starting' immediately after creation."""
        from amplifierd.daemon_session import create_session_dir

        session_path = create_session_dir(tmp_path, host="127.0.0.1", port=8410, log_level="info")

        meta = json.loads((session_path / "meta.json").read_text())
        assert meta["status"] == "starting"

    def test_meta_contains_version_field(self, tmp_path: Path):
        """meta.json includes a 'version' key (even if 'unknown')."""
        from amplifierd.daemon_session import create_session_dir

        session_path = create_session_dir(tmp_path, host="127.0.0.1", port=8410, log_level="info")

        meta = json.loads((session_path / "meta.json").read_text())
        assert "version" in meta


@pytest.mark.unit
class TestUpdateSessionMeta:
    """Tests for update_session_meta: merging updates into meta.json."""

    def test_merges_updates(self, tmp_path: Path):
        """Updates are merged into the existing meta.json."""
        from amplifierd.daemon_session import create_session_dir, update_session_meta

        session_path = create_session_dir(tmp_path, host="127.0.0.1", port=8410, log_level="info")

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

    def test_noop_when_meta_is_corrupt(self, tmp_path: Path):
        """Does nothing when meta.json contains invalid JSON (no crash)."""
        from amplifierd.daemon_session import update_session_meta

        meta_path = tmp_path / "meta.json"
        meta_path.write_text("not valid json", encoding="utf-8")

        # Should not raise
        update_session_meta(tmp_path, {"status": "running"})

        # File is left as-is (atomic replace only happens on success)
        assert meta_path.read_text() == "not valid json"

    def test_atomic_write_uses_tmp_then_replace(self, tmp_path: Path):
        """Update writes to .tmp first then renames (no .tmp left behind)."""
        from amplifierd.daemon_session import create_session_dir, update_session_meta

        session_path = create_session_dir(tmp_path, host="127.0.0.1", port=8410, log_level="info")
        update_session_meta(session_path, {"status": "running"})

        # No stale .tmp file
        assert not (session_path / "meta.tmp").exists()

        meta = json.loads((session_path / "meta.json").read_text())
        assert meta["status"] == "running"


@pytest.mark.unit
class TestPruneOldSessions:
    """Tests for prune_old_sessions: removing oldest session directories."""

    def test_prunes_excess_sessions(self, tmp_path: Path):
        """Keeps only the most recent `keep` dirs; removes older ones."""
        from amplifierd.daemon_session import prune_old_sessions

        # Create 55 directories with distinct mtimes
        for i in range(55):
            d = tmp_path / f"session_{i:02d}"
            d.mkdir()
            mtime = 1_000_000 + i
            os.utime(d, (mtime, mtime))

        removed = prune_old_sessions(tmp_path, keep=50)

        assert removed == 5
        remaining = [p.name for p in tmp_path.iterdir() if p.is_dir()]
        assert len(remaining) == 50
        # Oldest five (session_00..04) should be gone; newest (session_05..54) kept
        for i in range(5):
            assert f"session_{i:02d}" not in remaining
        for i in range(5, 55):
            assert f"session_{i:02d}" in remaining

    def test_noop_when_dir_missing(self, tmp_path: Path):
        """Returns 0 without error when the directory doesn't exist."""
        from amplifierd.daemon_session import prune_old_sessions

        missing = tmp_path / "nonexistent"
        removed = prune_old_sessions(missing)
        assert removed == 0

    def test_noop_when_under_limit(self, tmp_path: Path):
        """Does not remove anything when count is within the keep limit."""
        from amplifierd.daemon_session import prune_old_sessions

        for i in range(5):
            (tmp_path / f"session_{i}").mkdir()

        removed = prune_old_sessions(tmp_path, keep=50)
        assert removed == 0
        assert len(list(tmp_path.iterdir())) == 5


@pytest.mark.unit
class TestTeeWriterRobustness:
    """Tests for _TeeWriter: thread safety, error resilience, interface."""

    def test_write_survives_closed_log_file(self, tmp_path: Path):
        """write() does not raise even when the log file is already closed."""
        from amplifierd.daemon_session import _TeeWriter

        captured = io.StringIO()
        log_file = open(tmp_path / "test.log", "w", encoding="utf-8")
        tee = _TeeWriter(captured, log_file)
        log_file.close()  # Force an OSError on next write

        # Must not raise; original stream still receives the data
        tee.write("hello")
        assert "hello" in captured.getvalue()

    def test_flush_survives_closed_log_file(self, tmp_path: Path):
        """flush() does not raise even when the log file is already closed."""
        from amplifierd.daemon_session import _TeeWriter

        captured = io.StringIO()
        log_file = open(tmp_path / "test.log", "w", encoding="utf-8")
        tee = _TeeWriter(captured, log_file)
        log_file.close()

        tee.flush()  # Should not raise

    def test_writable_returns_true(self, tmp_path: Path):
        """writable() always returns True."""
        from amplifierd.daemon_session import _TeeWriter

        captured = io.StringIO()
        log_file = open(tmp_path / "test.log", "w", encoding="utf-8")
        tee = _TeeWriter(captured, log_file)

        assert tee.writable() is True
        log_file.close()

    def test_fileno_raises_unsupported(self, tmp_path: Path):
        """fileno() raises io.UnsupportedOperation."""
        from amplifierd.daemon_session import _TeeWriter

        captured = io.StringIO()
        log_file = open(tmp_path / "test.log", "w", encoding="utf-8")
        tee = _TeeWriter(captured, log_file)

        with pytest.raises(io.UnsupportedOperation):
            tee.fileno()

        log_file.close()

    def test_encoding_property(self, tmp_path: Path):
        """encoding property delegates to the original stream."""
        from amplifierd.daemon_session import _TeeWriter

        original = io.StringIO()
        log_file = open(tmp_path / "test.log", "w", encoding="utf-8")
        tee = _TeeWriter(original, log_file)

        # StringIO has no encoding attribute, so falls back to 'utf-8'
        assert tee.encoding == "utf-8"
        log_file.close()

    def test_name_property(self, tmp_path: Path):
        """name property falls back to '<tee>' when original has no name."""
        from amplifierd.daemon_session import _TeeWriter

        original = io.StringIO()
        log_file = open(tmp_path / "test.log", "w", encoding="utf-8")
        tee = _TeeWriter(original, log_file)

        assert tee.name == "<tee>"
        log_file.close()


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

    def test_stderr_teed_to_serve_log(self, tmp_path: Path):
        """Raw sys.stderr writes are captured in serve.log."""
        from amplifierd.daemon_session import setup_session_log

        orig_stdout = sys.stdout
        orig_stderr = sys.stderr
        orig_handlers = logging.getLogger().handlers[:]

        try:
            setup_session_log(tmp_path)

            sys.stderr.write("MARKER_STDERR_99999\n")
            sys.stderr.flush()

            log_content = (tmp_path / "serve.log").read_text()
            assert "MARKER_STDERR_99999" in log_content
        finally:
            sys.stdout = orig_stdout
            sys.stderr = orig_stderr
            root = logging.getLogger()
            root.handlers = orig_handlers
