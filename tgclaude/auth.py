"""OAuth credential reader.

Reads the Claude Max-plan OAuth access token from the credentials file
written by the Claude CLI.  The same token is used by both the
claude-agent-sdk and the /api/oauth/usage endpoint.
"""

from __future__ import annotations

import json
import logging
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
    credentials_path = claude_home / _CREDENTIALS_FILENAME

    raw = _read_file(credentials_path)
    data = _parse_json(credentials_path, raw)
    token = _extract_token(credentials_path, data)

    return token


# ---------------------------------------------------------------------------
# Private helpers — each handles exactly one failure mode
# ---------------------------------------------------------------------------


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


def _extract_token(path: Path, data: dict) -> str:
    oauth_section = data.get(_OAUTH_KEY)
    if not isinstance(oauth_section, dict):
        raise AuthError(
            f"{path} is missing the '{_OAUTH_KEY}' section. "
            "Run 'claude' interactively once on the VPS to complete login, "
            "then restart tgclaude."
        )

    token = oauth_section.get(_TOKEN_KEY)
    if not token:
        raise AuthError(
            f"{path} has '{_OAUTH_KEY}' but '{_TOKEN_KEY}' is absent or empty. "
            "Run 'claude' interactively once on the VPS to refresh credentials, "
            "then restart tgclaude."
        )

    return str(token)
