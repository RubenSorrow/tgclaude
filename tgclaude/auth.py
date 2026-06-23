"""OAuth credential reader.

Reads the Claude Max-plan OAuth access token from the credentials file
written by the Claude CLI.  The same token is used by both the
claude-agent-sdk and the /api/oauth/usage endpoint.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from pathlib import Path

logger = logging.getLogger(__name__)

_CREDENTIALS_FILENAME = ".credentials.json"
_OAUTH_KEY = "claudeAiOauth"
_TOKEN_KEY = "accessToken"


class AuthError(Exception):
    """Raised when credentials cannot be loaded or are unusable."""


def read_access_token(claude_home: Path) -> str:
    """Read the claudeAiOauth.accessToken from credentials.json.

    Args:
        claude_home: Path to the Claude home directory (typically ~/.claude).

    Returns:
        The raw OAuth access token string.

    Raises:
        AuthError: When the credentials file is missing, unreadable,
            structurally invalid, or the token field is absent or empty.
            Always raises AuthError rather than the underlying I/O or
            parse exception so callers need only one except clause.
    """
    oauth = _load_oauth_section(claude_home)
    credentials_path = claude_home / _CREDENTIALS_FILENAME

    token = oauth.get(_TOKEN_KEY)
    if not token:
        raise AuthError(
            f"{credentials_path} has '{_OAUTH_KEY}' but '{_TOKEN_KEY}' is absent or empty. "
            "Run 'claude' interactively once on the VPS to refresh credentials, "
            "then restart tgclaude."
        )

    return str(token)


# ---------------------------------------------------------------------------
# Private helpers — each handles exactly one failure mode
# ---------------------------------------------------------------------------


def _load_oauth_section(claude_home: Path) -> dict:
    """Read and return the claudeAiOauth section from credentials.json.

    Raises:
        AuthError: on any read/parse/structure failure.
    """
    path = claude_home / _CREDENTIALS_FILENAME
    raw = _read_file(path)
    data = _parse_json(path, raw)
    oauth = data.get(_OAUTH_KEY)
    if not isinstance(oauth, dict):
        raise AuthError(
            f"{path} is missing the '{_OAUTH_KEY}' section. "
            "Run 'claude' interactively once on the VPS to complete login, "
            "then restart tgclaude."
        )
    return oauth


def _read_file(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise AuthError(
            f"{path} does not exist. "
            "Run 'claude' interactively once on the VPS to complete login, "
            "then restart tgclaude."
        )
    except OSError as exc:
        raise AuthError(
            f"Cannot read {path}: {exc}. "
            "Check that the bot process runs as the same user that owns the Claude install."
        )


def _parse_json(path: Path, raw: str) -> dict:
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise AuthError(
            f"{path} is not valid JSON: {exc}. "
            "The file may be corrupt; try running 'claude' again to regenerate it."
        )


def _is_token_expired(claude_home: Path) -> bool:
    """Return True if the OAuth token is expired or expiring within 60 seconds.

    Reads ``claudeAiOauth.expiresAt`` from credentials.json.  That field is a
    Unix timestamp in **milliseconds**.  Returns False on any read/parse error
    so the caller can still attempt the request (fail-open).
    """
    try:
        oauth = _load_oauth_section(claude_home)
        expires_at_ms = oauth.get("expiresAt")
        if expires_at_ms is None:
            return False
        expires_at_s = float(expires_at_ms) / 1000.0
        # Consider expired if within 60 seconds of expiry
        return time.time() >= (expires_at_s - 60.0)
    except Exception:
        return False


async def refresh_access_token(claude_home: Path, http_client: object) -> str:
    """Refresh the OAuth access token via the Anthropic token endpoint.

    Uses the refreshToken from credentials.json to obtain a new accessToken,
    updates credentials.json with the new token + new expiresAt (ms) + new
    refreshToken (if the server rotated it), and returns the new accessToken.

    Args:
        claude_home: Path to the Claude home directory (e.g. ~/.claude).
        http_client: An httpx.AsyncClient instance to use for the POST.

    Raises:
        AuthError: on any failure (network, HTTP error, missing fields, file write).
    """
    credentials_path = claude_home / _CREDENTIALS_FILENAME
    oauth = _load_oauth_section(claude_home)

    refresh_token = oauth.get("refreshToken")
    if not refresh_token:
        raise AuthError(
            f"{credentials_path} has no 'refreshToken' — cannot refresh automatically. "
            "SSH in and run `claude` once to re-authenticate."
        )

    try:
        response = await http_client.post(  # type: ignore[union-attr]
            "https://platform.claude.com/v1/oauth/token",
            json={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": "9d1c250a-e61b-44d9-88ed-5944d1962f5e",
            },
            headers={
                "anthropic-beta": "oauth-2025-04-20",
            },
            timeout=30.0,
        )
    except Exception as exc:
        raise AuthError(f"OAuth token refresh request failed: {exc}") from exc

    if not response.is_success:
        raise AuthError(
            f"OAuth token refresh returned HTTP {response.status_code}: "
            f"{response.text[:200]}"
        )

    try:
        body = response.json()
    except Exception as exc:
        raise AuthError(f"OAuth token refresh response is not JSON: {exc}") from exc

    new_token = body.get("access_token")
    if not new_token:
        raise AuthError(
            f"OAuth token refresh response missing 'access_token': {str(body)[:200]}"
        )

    expires_in = body.get("expires_in")
    new_refresh_token = body.get("refresh_token", refresh_token)  # servers may or may not rotate

    # Update credentials file: read current, patch, write back atomically.
    try:
        raw = _read_file(credentials_path)
        data = _parse_json(credentials_path, raw)
        data[_OAUTH_KEY]["accessToken"] = new_token
        data[_OAUTH_KEY]["refreshToken"] = new_refresh_token
        ttl = int(expires_in) if expires_in is not None else 3600
        data[_OAUTH_KEY]["expiresAt"] = int((time.time() + ttl) * 1000)
        # Atomic write: write to temp in same dir, then os.replace
        dir_ = credentials_path.parent
        fd, tmp_path_str = tempfile.mkstemp(dir=dir_, prefix=".creds_", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(json.dumps(data, indent=2))
            try:
                original_mode = credentials_path.stat().st_mode
                os.chmod(tmp_path_str, original_mode)
            except OSError:
                pass
            os.replace(tmp_path_str, str(credentials_path))
        except Exception:
            try:
                os.unlink(tmp_path_str)
            except OSError:
                pass
            raise
    except (OSError, AuthError) as exc:
        raise AuthError(
            f"OAuth refresh succeeded but could not update {credentials_path}: {exc}"
        ) from exc

    logger.info("OAuth access token refreshed successfully")
    return new_token
