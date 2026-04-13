from __future__ import annotations

import logging

from tgclaude.db import Database

log = logging.getLogger(__name__)

READONLY_TOOLS: frozenset[str] = frozenset({"Read", "Grep", "Glob", "WebFetch"})


class PermissionManager:
    """Manages per-(user, session) tool allow-lists backed by SQLite.

    Grants persist across bot restarts and detach/reattach cycles.
    Scope: (telegram_user_id, session_uuid, tool_name).
    """

    def __init__(self, db: Database) -> None:
        self._db = db

    async def has_grant(self, user_id: int, session_uuid: str, tool_name: str) -> bool:
        """Return True if this tool is on the always-allow list for this (user, session)."""
        grants = await self._db.get_permission_grants(user_id, session_uuid)
        return tool_name in grants

    async def add_grant(self, user_id: int, session_uuid: str, tool_name: str) -> None:
        """Persist an always-allow grant for this (user, session, tool)."""
        log.info(
            "Adding permission grant: user_id=%d session=%s tool=%s",
            user_id,
            session_uuid,
            tool_name,
        )
        await self._db.add_permission_grant(user_id, session_uuid, tool_name)

    async def get_grants(self, user_id: int, session_uuid: str) -> frozenset[str]:
        """Return all granted tool names for this (user, session)."""
        return await self._db.get_permission_grants(user_id, session_uuid)
