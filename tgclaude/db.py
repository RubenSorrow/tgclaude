"""Async SQLite wrapper.

Provides a thin, domain-oriented interface over the four tables that make
up tgclaude's persistent state:

  active_sessions   — which session each user is currently attached to
  permission_grants — per-(user, session) "always allow" tool allow-list
  alert_state       — dedup store for the background usage alert poller
  settings          — mutable runtime overrides of env-var defaults
"""

from __future__ import annotations

import logging
from pathlib import Path

import aiosqlite

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# DDL — kept verbatim from §11 of the design document
# ---------------------------------------------------------------------------

_DDL = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS active_sessions (
  telegram_user_id  INTEGER PRIMARY KEY,
  session_uuid      TEXT UNIQUE,
  updated_at        TIMESTAMP
);

CREATE TABLE IF NOT EXISTS permission_grants (
  telegram_user_id  INTEGER,
  session_uuid      TEXT,
  tool_name         TEXT,
  PRIMARY KEY (telegram_user_id, session_uuid, tool_name)
);

CREATE TABLE IF NOT EXISTS alert_state (
  bucket          TEXT,
  threshold       INTEGER,
  last_fired_for  TEXT,
  PRIMARY KEY (bucket, threshold)
);

CREATE TABLE IF NOT EXISTS settings (
  -- Known key patterns:
  --   alerts_enabled      — boolean flag for background usage alerts
  --   alert_thresholds    — JSON list of usage percentage thresholds
  --   welcomed_{user_id}  — per-user first-run flag set by start_command
  key    TEXT PRIMARY KEY,
  value  TEXT NOT NULL
);
"""


class Database:
    """Thin async wrapper around the SQLite connection.

    Obtain an instance via :func:`init_db`; do not instantiate directly.
    """

    def __init__(self, connection: aiosqlite.Connection) -> None:
        self._conn = connection

    # ------------------------------------------------------------------
    # active_sessions
    # ------------------------------------------------------------------

    async def get_active_session(self, user_id: int) -> str | None:
        """Return the session UUID for user_id, or None if detached."""
        async with self._conn.execute(
            "SELECT session_uuid FROM active_sessions WHERE telegram_user_id = ?",
            (user_id,),
        ) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else None

    async def set_active_session(self, user_id: int, session_uuid: str) -> None:
        """Attach user_id to session_uuid, replacing any prior attachment."""
        await self._conn.execute(
            """
            INSERT INTO active_sessions (telegram_user_id, session_uuid, updated_at)
            VALUES (?, ?, datetime('now'))
            ON CONFLICT(telegram_user_id) DO UPDATE
              SET session_uuid = excluded.session_uuid,
                  updated_at   = excluded.updated_at
            """,
            (user_id, session_uuid),
        )
        await self._conn.commit()

    async def get_user_for_session(self, session_uuid: str) -> int | None:
        """Return the telegram_user_id currently attached to session_uuid, or None."""
        async with self._conn.execute(
            "SELECT telegram_user_id FROM active_sessions WHERE session_uuid = ?",
            (session_uuid,),
        ) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else None

    async def clear_active_session(self, user_id: int) -> None:
        """Detach user_id (set session_uuid to NULL)."""
        await self._conn.execute(
            """
            INSERT INTO active_sessions (telegram_user_id, session_uuid, updated_at)
            VALUES (?, NULL, datetime('now'))
            ON CONFLICT(telegram_user_id) DO UPDATE
              SET session_uuid = NULL,
                  updated_at   = excluded.updated_at
            """,
            (user_id,),
        )
        await self._conn.commit()

    # ------------------------------------------------------------------
    # permission_grants
    # ------------------------------------------------------------------

    async def get_permission_grants(
        self, user_id: int, session_uuid: str
    ) -> frozenset[str]:
        """Return the set of tool names the user has always-allowed in session."""
        async with self._conn.execute(
            """
            SELECT tool_name FROM permission_grants
            WHERE telegram_user_id = ? AND session_uuid = ?
            """,
            (user_id, session_uuid),
        ) as cursor:
            rows = await cursor.fetchall()
            return frozenset(row[0] for row in rows)

    async def add_permission_grant(
        self, user_id: int, session_uuid: str, tool_name: str
    ) -> None:
        """Persist an 'always allow' grant for (user_id, session_uuid, tool_name)."""
        await self._conn.execute(
            """
            INSERT OR IGNORE INTO permission_grants
              (telegram_user_id, session_uuid, tool_name)
            VALUES (?, ?, ?)
            """,
            (user_id, session_uuid, tool_name),
        )
        await self._conn.commit()

    # ------------------------------------------------------------------
    # alert_state
    # ------------------------------------------------------------------

    async def get_alert_state(self, bucket: str, threshold: int) -> str | None:
        """Return the resets_at value this (bucket, threshold) last fired for."""
        async with self._conn.execute(
            "SELECT last_fired_for FROM alert_state WHERE bucket = ? AND threshold = ?",
            (bucket, threshold),
        ) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else None

    async def get_any_alert_state_for_bucket(self, bucket: str) -> str | None:
        """Return the resets_at value from any row for bucket, or None if no rows exist."""
        async with self._conn.execute(
            "SELECT last_fired_for FROM alert_state WHERE bucket = ? LIMIT 1",
            (bucket,),
        ) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else None

    async def set_alert_state(
        self, bucket: str, threshold: int, resets_at: str
    ) -> None:
        """Record that an alert for (bucket, threshold) fired against resets_at."""
        await self._conn.execute(
            """
            INSERT INTO alert_state (bucket, threshold, last_fired_for)
            VALUES (?, ?, ?)
            ON CONFLICT(bucket, threshold) DO UPDATE
              SET last_fired_for = excluded.last_fired_for
            """,
            (bucket, threshold, resets_at),
        )
        await self._conn.commit()

    async def clear_alert_state_for_bucket(self, bucket: str) -> None:
        """Remove all alert state rows for bucket (called when the bucket resets)."""
        await self._conn.execute(
            "DELETE FROM alert_state WHERE bucket = ?",
            (bucket,),
        )
        await self._conn.commit()

    # ------------------------------------------------------------------
    # settings
    # ------------------------------------------------------------------

    async def get_setting(self, key: str) -> str | None:
        """Return the stored string value for key, or None if not set."""
        async with self._conn.execute(
            "SELECT value FROM settings WHERE key = ?",
            (key,),
        ) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else None

    async def set_setting(self, key: str, value: str) -> None:
        """Upsert a key-value setting."""
        await self._conn.execute(
            """
            INSERT INTO settings (key, value)
            VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key, value),
        )
        await self._conn.commit()

    async def delete_setting(self, key: str) -> None:
        """Remove a setting row; no-op if the key does not exist."""
        await self._conn.execute(
            "DELETE FROM settings WHERE key = ?",
            (key,),
        )
        await self._conn.commit()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def close(self) -> None:
        """Close the underlying database connection."""
        await self._conn.close()


async def init_db(path: Path) -> Database:
    """Open (or create) the SQLite database, run migrations, and return a Database.

    Args:
        path: Absolute path to the .db file.  Parent directory must exist.

    Returns:
        A fully-initialised :class:`Database` instance backed by a WAL-mode
        SQLite connection.
    """
    conn = await aiosqlite.connect(str(path))
    conn.row_factory = aiosqlite.Row

    async with conn.executescript(_DDL):
        pass

    logger.info("Database initialised at %s", path)
    return Database(conn)
