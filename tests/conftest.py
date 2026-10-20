"""Shared pytest configuration."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _allow_testclient_host(monkeypatch: pytest.MonkeyPatch) -> None:
    """Let Starlette's TestClient through Host validation.

    TestClient sends ``Host: testserver`` (and some tests use httpx with
    ``base_url="http://test"``); neither is a name the daemon answers to in
    production.  Tests that exercise Host validation itself
    build their own settings or middleware.
    """
    monkeypatch.setenv("AMPLIFIERD_ALLOWED_HOSTS", '["testserver", "test"]')
