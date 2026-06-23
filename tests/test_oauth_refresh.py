"""Tests for OAuth token refresh functionality.

Tests cover:
- _load_oauth_section (output-based, pure function)
- _is_token_expired (output-based, pure function)
- refresh_access_token (communication-based, async, mock HTTP)
- UsageClient.get_usage refresh integration
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tgclaude.auth import AuthError, _is_token_expired, _load_oauth_section, refresh_access_token
from tgclaude.usage_client import UsageAuthError, UsageClient


# ---------------------------------------------------------------------------
# Setup helper
# ---------------------------------------------------------------------------


def write_credentials(directory: Path, oauth: dict) -> None:
    """Write a minimal .credentials.json to *directory*."""
    cred = {"claudeAiOauth": oauth}
    (directory / ".credentials.json").write_text(json.dumps(cred))


def _make_usage_data():
    """Return a minimal UsageData for mocking _fetch."""
    from tgclaude.usage_client import UsageData
    return UsageData(
        five_hour=None,
        seven_day=None,
        seven_day_sonnet=None,
        extra_usage_enabled=False,
        extra_usage_monthly_limit=None,
        extra_usage_used_credits=None,
        extra_usage_utilization=None,
    )


def _make_http_response(status_code: int = 200, body: dict | None = None, is_success: bool = True) -> MagicMock:
    """Build a mock httpx response."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.is_success = is_success
    resp.json.return_value = body or {}
    resp.text = json.dumps(body or {})[:200]
    return resp


# ---------------------------------------------------------------------------
# Tests for _load_oauth_section
# ---------------------------------------------------------------------------


class TestLoadOauthSection:
    def test_load_oauth_section_returns_dict(self, tmp_path: Path) -> None:
        """Valid credentials with a known key — returned dict should contain it."""
        write_credentials(tmp_path, {"accessToken": "tok123", "refreshToken": "rt456"})
        result = _load_oauth_section(tmp_path)
        assert result["accessToken"] == "tok123"
        assert result["refreshToken"] == "rt456"

    def test_load_oauth_section_raises_on_missing_file(self, tmp_path: Path) -> None:
        """Non-existent credentials file should raise AuthError."""
        non_existent = tmp_path / "no_such_dir"
        with pytest.raises(AuthError):
            _load_oauth_section(non_existent)

    def test_load_oauth_section_raises_on_missing_oauth_key(self, tmp_path: Path) -> None:
        """Credentials file without claudeAiOauth section should raise AuthError."""
        (tmp_path / ".credentials.json").write_text(json.dumps({"someOtherKey": {}}))
        with pytest.raises(AuthError):
            _load_oauth_section(tmp_path)


# ---------------------------------------------------------------------------
# Tests for _is_token_expired
# ---------------------------------------------------------------------------


class TestIsTokenExpired:
    def test_is_token_expired_returns_true_when_expired(self, tmp_path: Path) -> None:
        """Token 120 seconds in the past should be considered expired."""
        expires_at_ms = (time.time() - 120) * 1000
        write_credentials(tmp_path, {"expiresAt": expires_at_ms})
        assert _is_token_expired(tmp_path) is True

    def test_is_token_expired_returns_false_when_valid(self, tmp_path: Path) -> None:
        """Token 1 hour in the future should not be considered expired."""
        expires_at_ms = (time.time() + 3600) * 1000
        write_credentials(tmp_path, {"expiresAt": expires_at_ms})
        assert _is_token_expired(tmp_path) is False

    def test_is_token_expired_returns_false_on_missing_field(self, tmp_path: Path) -> None:
        """Credentials without expiresAt should return False (fail-open)."""
        write_credentials(tmp_path, {"accessToken": "tok"})
        assert _is_token_expired(tmp_path) is False


# ---------------------------------------------------------------------------
# Tests for refresh_access_token
# ---------------------------------------------------------------------------


class TestRefreshAccessToken:
    async def test_refresh_access_token_posts_to_correct_endpoint(self, tmp_path: Path) -> None:
        """Successful refresh: POST should go to the correct OAuth endpoint."""
        write_credentials(tmp_path, {"refreshToken": "rt_original", "accessToken": "old"})

        mock_response = _make_http_response(
            status_code=200,
            body={"access_token": "new_tok", "expires_in": 3600},
            is_success=True,
        )
        http_client = MagicMock()
        http_client.post = AsyncMock(return_value=mock_response)

        result = await refresh_access_token(tmp_path, http_client)

        assert result == "new_tok"
        http_client.post.assert_called_once()
        call_args = http_client.post.call_args
        assert call_args[0][0] == "https://platform.claude.com/v1/oauth/token"
        assert call_args[1]["json"]["grant_type"] == "refresh_token"

    async def test_refresh_access_token_uses_claude_home_credentials(self, tmp_path: Path) -> None:
        """POST body must contain the refreshToken from the given claude_home path."""
        write_credentials(tmp_path, {"refreshToken": "rt123", "accessToken": "old"})

        mock_response = _make_http_response(
            status_code=200,
            body={"access_token": "new_tok", "expires_in": 3600},
            is_success=True,
        )
        http_client = MagicMock()
        http_client.post = AsyncMock(return_value=mock_response)

        await refresh_access_token(tmp_path, http_client)

        call_args = http_client.post.call_args
        assert call_args[1]["json"]["refresh_token"] == "rt123"

    async def test_refresh_access_token_updates_credentials_file(self, tmp_path: Path) -> None:
        """After successful refresh, credentials file accessToken should be updated."""
        write_credentials(tmp_path, {"refreshToken": "rt_orig", "accessToken": "old_tok"})

        mock_response = _make_http_response(
            status_code=200,
            body={"access_token": "newtok", "expires_in": 3600},
            is_success=True,
        )
        http_client = MagicMock()
        http_client.post = AsyncMock(return_value=mock_response)

        await refresh_access_token(tmp_path, http_client)

        written = json.loads((tmp_path / ".credentials.json").read_text())
        assert written["claudeAiOauth"]["accessToken"] == "newtok"

    async def test_refresh_access_token_updates_expiry_in_milliseconds(self, tmp_path: Path) -> None:
        """After refresh with expires_in=3600, expiresAt should be ~(now+3600)*1000."""
        write_credentials(tmp_path, {"refreshToken": "rt_orig", "accessToken": "old_tok"})

        mock_response = _make_http_response(
            status_code=200,
            body={"access_token": "new_tok", "expires_in": 3600},
            is_success=True,
        )
        http_client = MagicMock()
        http_client.post = AsyncMock(return_value=mock_response)

        before = time.time()
        await refresh_access_token(tmp_path, http_client)
        after = time.time()

        written = json.loads((tmp_path / ".credentials.json").read_text())
        expires_at_ms = written["claudeAiOauth"]["expiresAt"]

        expected_min = int((before + 3600) * 1000) - 5000
        expected_max = int((after + 3600) * 1000) + 5000
        assert expected_min <= expires_at_ms <= expected_max

    async def test_refresh_access_token_raises_auth_error_on_http_failure(self, tmp_path: Path) -> None:
        """HTTP 400 response should raise AuthError."""
        write_credentials(tmp_path, {"refreshToken": "rt_orig", "accessToken": "old"})

        mock_response = _make_http_response(
            status_code=400,
            body={"error": "bad_request"},
            is_success=False,
        )
        http_client = MagicMock()
        http_client.post = AsyncMock(return_value=mock_response)

        with pytest.raises(AuthError):
            await refresh_access_token(tmp_path, http_client)

    async def test_refresh_access_token_raises_auth_error_on_missing_access_token(
        self, tmp_path: Path
    ) -> None:
        """Successful HTTP response without access_token field should raise AuthError."""
        write_credentials(tmp_path, {"refreshToken": "rt_orig", "accessToken": "old"})

        mock_response = _make_http_response(
            status_code=200,
            body={"error": "oops"},
            is_success=True,
        )
        http_client = MagicMock()
        http_client.post = AsyncMock(return_value=mock_response)

        with pytest.raises(AuthError):
            await refresh_access_token(tmp_path, http_client)

    async def test_refresh_access_token_raises_auth_error_when_no_refresh_token(
        self, tmp_path: Path
    ) -> None:
        """Credentials with no refreshToken should raise AuthError with no HTTP call."""
        write_credentials(tmp_path, {"accessToken": "tok_only"})

        http_client = MagicMock()
        http_client.post = AsyncMock()

        with pytest.raises(AuthError):
            await refresh_access_token(tmp_path, http_client)

        http_client.post.assert_not_called()

    async def test_refresh_updates_expiry_even_when_expires_in_absent(self, tmp_path: Path) -> None:
        """When server omits expires_in, expiresAt should default to now+3600 seconds."""
        write_credentials(tmp_path, {"refreshToken": "rt_orig", "accessToken": "old_tok"})

        mock_response = _make_http_response(
            status_code=200,
            body={"access_token": "tok"},
            is_success=True,
        )
        http_client = MagicMock()
        http_client.post = AsyncMock(return_value=mock_response)

        before = time.time()
        await refresh_access_token(tmp_path, http_client)
        after = time.time()

        written = json.loads((tmp_path / ".credentials.json").read_text())
        expires_at_ms = written["claudeAiOauth"]["expiresAt"]

        expected_min = int((before + 3600) * 1000) - 10000
        expected_max = int((after + 3600) * 1000) + 10000
        assert expected_min <= expires_at_ms <= expected_max


# ---------------------------------------------------------------------------
# Tests for UsageClient.get_usage refresh integration
# ---------------------------------------------------------------------------


class TestGetUsageRefreshIntegration:
    def _make_client(self, tmp_path: Path) -> UsageClient:
        """Create a UsageClient with a real tmp_path claude_home and mocked HTTP."""
        write_credentials(tmp_path, {"accessToken": "tok", "refreshToken": "rt"})
        http_client = MagicMock()
        client = UsageClient(http_client=http_client, claude_home=tmp_path)
        client._token = "tok"
        return client

    async def test_get_usage_refreshes_on_401_and_succeeds(self, tmp_path: Path) -> None:
        """On 401 from _fetch, refresh succeeds and second fetch returns data."""
        client = self._make_client(tmp_path)
        good_data = _make_usage_data()

        # First _fetch raises UsageAuthError; second succeeds
        client._fetch = AsyncMock(side_effect=[UsageAuthError("401"), good_data])

        with patch(
            "tgclaude.usage_client.refresh_access_token",
            new=AsyncMock(return_value="new_token"),
        ) as mock_refresh, patch(
            "tgclaude.usage_client._is_token_expired",
            return_value=False,
        ):
            result = await client.get_usage(bypass_cache=True)

        assert result is good_data
        mock_refresh.assert_called_once()

    async def test_get_usage_raises_usage_auth_error_when_refresh_also_fails(
        self, tmp_path: Path
    ) -> None:
        """On 401, if refresh raises AuthError and second fetch also fails, raise UsageAuthError."""
        client = self._make_client(tmp_path)

        # Both _fetch calls raise UsageAuthError
        client._fetch = AsyncMock(side_effect=UsageAuthError("401"))

        with patch(
            "tgclaude.usage_client.refresh_access_token",
            new=AsyncMock(side_effect=AuthError("refresh failed")),
        ), patch(
            "tgclaude.usage_client._is_token_expired",
            return_value=False,
        ):
            with pytest.raises(UsageAuthError):
                await client.get_usage(bypass_cache=True)
