import json

import pytest

from amplifierd.state.session_index import SessionIndex, SessionIndexEntry


def test_create_and_load_index(tmp_path):
    index = SessionIndex(tmp_path / "index.json")
    entry = SessionIndexEntry(
        session_id="abc-123",
        status="idle",
        bundle="test-bundle",
        created_at="2026-03-03T10:00:00Z",
        last_activity="2026-03-03T10:00:00Z",
    )
    index.add(entry)
    index.save()
    loaded = SessionIndex.load(tmp_path / "index.json")
    assert loaded.get("abc-123") is not None
    assert loaded.get("abc-123").bundle == "test-bundle"


def test_atomic_write(tmp_path):
    """Verify tmp+rename pattern — no .tmp file left behind."""
    index = SessionIndex(tmp_path / "index.json")
    index.add(
        SessionIndexEntry(
            session_id="x",
            status="idle",
            bundle="b",
            created_at="2026-03-03T10:00:00Z",
            last_activity="2026-03-03T10:00:00Z",
        )
    )
    index.save()
    assert list(tmp_path.glob(".index-*.tmp")) == []
    assert (tmp_path / "index.json").exists()


def test_rebuild_from_project_layout(tmp_path):
    """Rebuild index by scanning projects/<project>/sessions/<session>/ layout."""
    projects_dir = tmp_path / "projects"
    slug = "-home-user-myproject"
    sid = "abc-123"

    # Build nested structure: projects/<slug>/sessions/<sid>/metadata.json
    session_dir = projects_dir / slug / "sessions" / sid
    session_dir.mkdir(parents=True)
    (session_dir / "metadata.json").write_text(
        json.dumps(
            {
                "session_id": sid,
                "bundle": "test",
                "created_at": "2026-03-03T10:00:00Z",
            }
        )
    )

    index = SessionIndex.rebuild(projects_dir)
    entry = index.get(sid)
    assert entry is not None
    assert entry.bundle == "test"
    assert entry.created_at == "2026-03-03T10:00:00Z"
    assert entry.project_id == slug


def test_rebuild_multiple_projects(tmp_path):
    """Rebuild index scans sessions across multiple projects."""
    projects_dir = tmp_path / "projects"
    for i, (slug, sid) in enumerate(
        [("-home-user-proj1", "sess-aaa"), ("-home-user-proj2", "sess-bbb")]
    ):
        sdir = projects_dir / slug / "sessions" / sid
        sdir.mkdir(parents=True)
        (sdir / "metadata.json").write_text(
            json.dumps({"bundle": f"bundle-{i}", "created_at": "2026-03-03T10:00:00Z"})
        )

    index = SessionIndex.rebuild(projects_dir)
    assert index.get("sess-aaa") is not None
    assert index.get("sess-bbb") is not None
    assert index.get("sess-aaa").project_id == "-home-user-proj1"
    assert index.get("sess-bbb").project_id == "-home-user-proj2"


def test_rebuild_skips_dirs_without_sessions_subdir(tmp_path):
    """Rebuild ignores project dirs that have no sessions/ subdirectory."""
    projects_dir = tmp_path / "projects"
    # Project dir with no sessions/ sub-dir
    orphan = projects_dir / "some-project"
    orphan.mkdir(parents=True)
    (orphan / "metadata.json").write_text("{}")  # stray file

    index = SessionIndex.rebuild(projects_dir)
    assert index.list_entries() == []


def test_load_tolerates_missing_project_id(tmp_path):
    """Loading an old index file that lacks project_id defaults it to empty string."""
    path = tmp_path / "index.json"
    # Write old-format entry without project_id
    path.write_text(
        json.dumps(
            [
                {
                    "session_id": "old-sess",
                    "status": "completed",
                    "bundle": "distro",
                    "created_at": "2026-01-01T00:00:00Z",
                    "last_activity": "2026-01-01T00:00:00Z",
                    "parent_session_id": None,
                }
            ]
        )
    )
    index = SessionIndex.load(path)
    entry = index.get("old-sess")
    assert entry is not None
    assert entry.project_id == ""


def test_load_corrupted_falls_back_to_empty(tmp_path):
    (tmp_path / "index.json").write_text("NOT VALID JSON")
    index = SessionIndex.load(tmp_path / "index.json")
    assert index.list_entries() == []


def test_update_entry(tmp_path):
    index = SessionIndex(tmp_path / "index.json")
    index.add(
        SessionIndexEntry(
            session_id="x",
            status="idle",
            bundle="b",
            created_at="2026-03-03T10:00:00Z",
            last_activity="2026-03-03T10:00:00Z",
        )
    )
    result = index.update("x", status="running")
    assert result is True
    assert index.get("x").status == "running"


def test_update_project_id(tmp_path):
    index = SessionIndex(tmp_path / "index.json")
    index.add(
        SessionIndexEntry(
            session_id="x",
            status="idle",
            bundle="b",
            created_at="2026-03-03T10:00:00Z",
            last_activity="2026-03-03T10:00:00Z",
        )
    )
    result = index.update("x", project_id="-home-user-proj")
    assert result is True
    assert index.get("x").project_id == "-home-user-proj"


def test_update_unknown_session_returns_false(tmp_path):
    index = SessionIndex(tmp_path / "index.json")
    result = index.update("nonexistent", status="running")
    assert result is False


def test_update_invalid_field_raises(tmp_path):
    index = SessionIndex(tmp_path / "index.json")
    index.add(
        SessionIndexEntry(
            session_id="x",
            status="idle",
            bundle="b",
            created_at="2026-03-03T10:00:00Z",
            last_activity="2026-03-03T10:00:00Z",
        )
    )
    with pytest.raises(ValueError, match="Unknown"):
        index.update("x", bogus_field="bad")


def test_remove_entry(tmp_path):
    index = SessionIndex(tmp_path / "index.json")
    index.add(
        SessionIndexEntry(
            session_id="x",
            status="idle",
            bundle="b",
            created_at="2026-03-03T10:00:00Z",
            last_activity="2026-03-03T10:00:00Z",
        )
    )
    index.remove("x")
    assert index.get("x") is None


def test_get_missing_returns_none(tmp_path):
    index = SessionIndex(tmp_path / "index.json")
    assert index.get("no-such-id") is None
