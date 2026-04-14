"""Configuration loader.

Loads environment variables from a .env file (via python-dotenv) and
validates them at import time.  Raises SystemExit with a clear message
on any validation failure so the bot never starts in a degraded state.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

logger = logging.getLogger(__name__)

# Tools that are always allowed in 'readonly' permission mode.
READONLY_TOOLS: frozenset[str] = frozenset({"Read", "Grep", "Glob", "WebFetch"})

_VALID_PERMISSION_MODES = frozenset({"interactive", "bypass", "readonly"})
_VALID_LOG_LEVELS = frozenset({"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"})

_BOT_TOKEN_RE = re.compile(r'^\d{1,20}:[A-Za-z0-9_-]{30,60}$')


@dataclass(frozen=True)
class Config:
    """Immutable, validated snapshot of all runtime configuration."""

    bot_token: str
    allowed_user_ids: frozenset[int]
    claude_home: Path
    claude_project_cwd: Path
    claude_binary: Path | None    # path to the system claude CLI; None = SDK bundled fallback
    database_path: Path
    alert_thresholds: list[int]
    permission_mode: str          # 'interactive' | 'bypass' | 'readonly'
    display_tz: str | None        # IANA tz name or None → use system TZ
    permission_timeout_s: int     # default 600
    turn_timeout_s: int           # default 300
    log_level: str                # default 'INFO'


def load_config() -> Config:
    """Load and validate configuration from environment variables.

    Searches for a .env file in the current working directory and its
    parents (python-dotenv default behaviour).  Missing or invalid values
    cause an immediate SystemExit with an operator-friendly message.
    """
    load_dotenv()

    bot_token = _parse_bot_token()
    allowed_user_ids = _parse_allowed_user_ids()
    claude_home = _parse_path("CLAUDE_HOME", default="~/.claude")
    claude_project_cwd = _parse_path("CLAUDE_PROJECT_CWD", default="~")
    claude_binary = _parse_claude_binary()
    database_path = _parse_database_path()
    alert_thresholds = _parse_alert_thresholds()
    permission_mode = _parse_permission_mode()
    display_tz = _parse_display_tz()
    permission_timeout_s = _parse_positive_int("PERMISSION_TIMEOUT_S", default=600)
    turn_timeout_s = _parse_positive_int("TURN_TIMEOUT_S", default=300)
    log_level = _parse_log_level(os.getenv("LOG_LEVEL", "INFO"))

    return Config(
        bot_token=bot_token,
        allowed_user_ids=allowed_user_ids,
        claude_home=claude_home,
        claude_project_cwd=claude_project_cwd,
        claude_binary=claude_binary,
        database_path=database_path,
        alert_thresholds=alert_thresholds,
        permission_mode=permission_mode,
        display_tz=display_tz,
        permission_timeout_s=permission_timeout_s,
        turn_timeout_s=turn_timeout_s,
        log_level=log_level,
    )


# ---------------------------------------------------------------------------
# Private helpers — each validates exactly one variable
# ---------------------------------------------------------------------------


def _require_str(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        _die(f"{name} is required but missing or empty.")
    return value


def _parse_bot_token() -> str:
    value = os.getenv("BOT_TOKEN", "").strip()
    if not value:
        _die("BOT_TOKEN is required but missing or empty.")
    if not _BOT_TOKEN_RE.match(value):
        _die(
            "BOT_TOKEN format is invalid. "
            "Expected format: 1234567890:ABCdefGHIJklmnopqrsTUVwxyz-… "
            "(digits, colon, 30-60 alphanumeric/underscore/hyphen characters)."
        )
    return value


def _parse_allowed_user_ids() -> frozenset[int]:
    raw = os.getenv("ALLOWED_USER_IDS", "").strip()
    if not raw:
        _die(
            "ALLOWED_USER_IDS is required but missing or empty. "
            "Set it to a comma-separated list of Telegram numeric user IDs. "
            "Refusing to start with an open allow-list."
        )

    ids: list[int] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            ids.append(int(part))
        except ValueError:
            _die(
                f"ALLOWED_USER_IDS contains a non-integer value: {part!r}. "
                "Each entry must be a numeric Telegram user ID."
            )

    if not ids:
        _die(
            "ALLOWED_USER_IDS is set but contains no valid user IDs. "
            "Refusing to start with an open allow-list."
        )

    return frozenset(ids)


def _parse_path(name: str, *, default: str) -> Path:
    raw = os.getenv(name, default).strip()
    return Path(raw).expanduser()


def _parse_database_path() -> Path:
    path = _parse_path("DATABASE_PATH", default="~/.local/state/tgclaude/bot.db")
    parent = path.parent
    try:
        parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    except OSError as exc:
        _die(
            f"Cannot create DATABASE_PATH parent directory {parent}: {exc}. "
            "Check filesystem permissions."
        )
    return path


def _parse_claude_binary() -> Path | None:
    raw = os.getenv("CLAUDE_BINARY", "").strip()
    if raw:
        p = Path(raw).expanduser()
        if not p.is_file():
            _die(f"CLAUDE_BINARY={raw!r} does not point to an existing file.")
        return p
    # Auto-detect from PATH
    found = shutil.which("claude")
    if found:
        return Path(found)
    return None


def _parse_alert_thresholds() -> list[int]:
    raw = os.getenv("ALERT_THRESHOLDS", "50,80,95").strip()
    thresholds: list[int] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            value = int(part)
        except ValueError:
            _die(
                f"ALERT_THRESHOLDS contains a non-integer value: {part!r}. "
                "Expected comma-separated integers in [0, 100]."
            )
        if not (0 <= value <= 100):
            _die(
                f"ALERT_THRESHOLDS value {value} is out of range. "
                "Each threshold must be an integer in [0, 100]."
            )
        thresholds.append(value)

    if not thresholds:
        _die("ALERT_THRESHOLDS is set but contains no valid values.")

    return thresholds


def _parse_permission_mode() -> str:
    raw = os.getenv("PERMISSION_MODE", "interactive").strip().lower()
    if raw not in _VALID_PERMISSION_MODES:
        _die(
            f"PERMISSION_MODE={raw!r} is not valid. "
            f"Must be one of: {', '.join(sorted(_VALID_PERMISSION_MODES))}."
        )
    return raw


def _parse_log_level(raw: str) -> str:
    val = raw.upper()
    if val not in _VALID_LOG_LEVELS:
        _die(f"LOG_LEVEL must be one of {', '.join(sorted(_VALID_LOG_LEVELS))}; got {raw!r}")
    return val


def _parse_display_tz() -> str | None:
    raw = os.getenv("DISPLAY_TZ", "").strip()
    if not raw:
        return None

    try:
        import zoneinfo  # stdlib in Python 3.9+; conditional dep for 3.8
        zoneinfo.ZoneInfo(raw)
    except (ImportError, zoneinfo.ZoneInfoNotFoundError):
        _die(
            f"DISPLAY_TZ={raw!r} is not a valid IANA timezone name. "
            "Examples: America/Phoenix, Europe/Berlin, UTC."
        )

    return raw


def _parse_positive_int(name: str, *, default: int) -> int:
    raw = os.getenv(name, str(default)).strip()
    try:
        value = int(raw)
    except ValueError:
        _die(f"{name}={raw!r} is not a valid integer.")

    if value <= 0:
        _die(f"{name} must be a positive integer, got {value}.")

    return value


def _die(message: str) -> None:
    """Print a configuration error and exit with a non-zero status."""
    sys.exit(f"[tgclaude] Configuration error: {message}")
