"""Session discovery.

Uses the SDK's ``list_sessions`` to enumerate Claude sessions for a project
directory and returns sorted ``SessionInfo`` objects for the Telegram picker.
Falls back gracefully to an empty list if the SDK function is unavailable.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

try:
    from claude_agent_sdk import list_sessions as _sdk_list_sessions
    _SDK_AVAILABLE = True
except ImportError:
    _sdk_list_sessions = None  # type: ignore[assignment]
    _SDK_AVAILABLE = False

logger = logging.getLogger(__name__)

_TITLE_MAX_CHARS = 50
_UUID_FALLBACK_CHARS = 8


@dataclass
class SessionInfo:
    """Metadata about a single Claude session."""

    session_uuid: str    # session UUID
    title: str           # display title, truncated to 50 chars, single line
    mtime: datetime      # last-modified time (used for sort order)


def encoded_project_dir(cwd: Path) -> str:
    """Convert an absolute CWD path to the Claude-encoded project directory name.

    Rule: replace every '/' with '-'.  Because absolute paths begin with '/',
    the result always starts with '-'.

    Args:
        cwd: Absolute working directory path.

    Returns:
        Encoded directory name string, e.g. ``Path('/home/foo')`` → ``'-home-foo'``.

    Example::

        >>> encoded_project_dir(Path('/home/alice'))
        '-home-alice'
    """
    return str(cwd).replace("/", "-")


async def list_sessions(claude_home: Path, project_cwd: Path) -> list[SessionInfo]:
    """Return all sessions for project_cwd, sorted newest-first by mtime.

    Delegates to the SDK's ``list_sessions`` function, passing the raw
    project CWD directly — the SDK handles path encoding internally.

    Args:
        claude_home: Path to the Claude home directory.  Unused; kept for
            API compatibility with the caller in commands.py.
        project_cwd: The working directory whose sessions should be listed.

    Returns:
        Sessions sorted newest-first by last-modified time.  Returns an
        empty list if the SDK is unavailable or no sessions exist.
    """
    if not _SDK_AVAILABLE:
        logger.warning("claude_agent_sdk not installed; session listing unavailable")
        return []

    try:
        sdk_sessions = _sdk_list_sessions(directory=str(project_cwd))
    except Exception as exc:
        logger.debug("SDK list_sessions failed: %s", exc)
        return []

    sessions: list[SessionInfo] = []
    for s in sdk_sessions:
        # last_modified is milliseconds since epoch (int)
        mtime = datetime.fromtimestamp(s.last_modified / 1000, tz=timezone.utc)
        title = _normalise_title(s.summary if s.summary else s.session_id[:_UUID_FALLBACK_CHARS])
        sessions.append(SessionInfo(session_uuid=s.session_id, title=title, mtime=mtime))

    sessions.sort(key=lambda s: s.mtime, reverse=True)
    return sessions


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _normalise_title(text: str) -> str:
    """Collapse a message to a single line and truncate to _TITLE_MAX_CHARS."""
    single_line = " ".join(text.split())
    if len(single_line) <= _TITLE_MAX_CHARS:
        return single_line
    return single_line[:_TITLE_MAX_CHARS]
