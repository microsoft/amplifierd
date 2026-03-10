"""Tests for dynamic CORS origin builder."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from amplifierd.security.origins import build_allowed_origins, is_origin_allowed


@pytest.mark.unit
class TestBuildAllowedOrigins:
    """Tests for build_allowed_origins()."""

    def test_always_includes_localhost(self):
        with patch("amplifierd.security.origins.get_dns_name", return_value=None):
            origins = build_allowed_origins()
        assert "localhost" in origins
        assert "127.0.0.1" in origins

    def test_includes_tailscale_dns(self):
        with patch(
            "amplifierd.security.origins.get_dns_name",
            return_value="myhost.tail1234.ts.net",
        ):
            origins = build_allowed_origins()
        assert "myhost.tail1234.ts.net" in origins

    def test_includes_extras(self):
        with patch("amplifierd.security.origins.get_dns_name", return_value=None):
            origins = build_allowed_origins(extra=["https://custom.example.com"])
        assert "https://custom.example.com" in origins

    def test_deduplicates(self):
        with patch("amplifierd.security.origins.get_dns_name", return_value=None):
            origins = build_allowed_origins(extra=["localhost"])
        assert origins.count("localhost") == 1


@pytest.mark.unit
class TestIsOriginAllowed:
    """Tests for is_origin_allowed()."""

    def test_none_origin_always_allowed(self):
        assert is_origin_allowed(None, {"localhost"}) is True

    def test_matching_origin_allowed(self):
        assert is_origin_allowed("https://myhost.tail1234.ts.net", {"tail1234.ts.net"}) is True

    def test_non_matching_origin_rejected(self):
        assert is_origin_allowed("https://evil.com", {"localhost", "tail1234.ts.net"}) is False