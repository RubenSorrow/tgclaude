"""Session discovery.

Reads Claude's JSONL session files from disk and returns a sorted list of
session metadata.  All I/O is synchronous file reads (the files are local
and small); the function is declared async only to fit the async calling
convention of the rest of the codebase.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

_TITLE_MAX_CHARS = 50
_UUID_FALLBACK_CHARS = 8


@dataclass
class SessionInfo:
    """Metadata about a single Claude session."""

    session_uuid: str    # filename without the .jsonl extension
    title: str           # first user message, truncated to 50 chars, single line
    mtime: datetime      # file modification time (used for sort order)


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

    Scans ``$CLAUDE_HOME/projects/<encoded_project_dir(project_cwd)>/*.jsonl``.

    Args:
        claude_home: Path to the Claude home directory (typically ``~/.claude``).
        project_cwd: The working directory whose sessions should be listed.

    Returns:
        Sessions sorted newest-first by file modification time.  Returns an
        empty list (not an error) if the project directory does not exist yet.
        Corrupt or unreadable JSONL files are silently skipped.
    """
    project_dir = claude_home / "projects" / encoded_project_dir(project_cwd)

    if not project_dir.exists():
        logger.debug("Project directory %s does not exist yet; returning empty list", project_dir)
        return []

    sessions: list[SessionInfo] = []
    for jsonl_path in project_dir.glob("*.jsonl"):
        info = _load_session_info(jsonl_path)
        if info is not None:
            sessions.append(info)

    sessions.sort(key=lambda s: s.mtime, reverse=True)
    return sessions


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _load_session_info(path: Path) -> SessionInfo | None:
    """Parse a single JSONL file into SessionInfo, or return None on failure."""
    session_uuid = path.stem

    try:
        mtime = datetime.fromtimestamp(path.stat().st_mtime)
    except OSError as exc:
        logger.debug("Cannot stat %s: %s — skipping", path, exc)
        return None

    title = _extract_title(path, session_uuid)
    return SessionInfo(session_uuid=session_uuid, title=title, mtime=mtime)


def _extract_title(path: Path, session_uuid: str) -> str:
    """Read the first user message from the JSONL as the session title.

    Falls back to the first 8 characters of the UUID if no user message is
    found or if the file cannot be read.
    """
    fallback = session_uuid[:_UUID_FALLBACK_CHARS]

    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        logger.debug("Cannot read %s: %s — using fallback title", path, exc)
        return fallback

    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        content = _parse_user_content(line)
        if content is not None:
            return _normalise_title(content)

    return fallback


def _parse_user_content(line: str) -> str | None:
    """Extract the text content of the first user message in a JSONL line.

    Tries two known schema shapes Claude Code has used:
      1. ``{"message": {"role": "user", "content": ...}}``
      2. ``{"role": "user", "content": ...}``

    Returns the extracted text, or None if this line is not a user message.
    """
    try:
        entry = json.loads(line)
    except json.JSONDecodeError:
        return None

    if not isinstance(entry, dict):
        return None

    # Shape 1: nested under 'message'
    message = entry.get("message")
    if isinstance(message, dict) and message.get("role") == "user":
        return _extract_content_text(message.get("content"))

    # Shape 2: flat entry
    if entry.get("role") == "user":
        return _extract_content_text(entry.get("content"))

    return None


def _extract_content_text(content: object) -> str | None:
    """Extract a plain string from a content field that may be str or list."""
    if isinstance(content, str):
        return content or None

    if isinstance(content, list):
        # Content may be a list of blocks: [{"type": "text", "text": "..."}]
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text", "")
                if isinstance(text, str) and text:
                    return text

    return None


def _normalise_title(text: str) -> str:
    """Collapse a message to a single line and truncate to _TITLE_MAX_CHARS."""
    single_line = " ".join(text.split())
    if len(single_line) <= _TITLE_MAX_CHARS:
        return single_line
    return single_line[:_TITLE_MAX_CHARS]
