"""Shared Telegram API helpers."""

from __future__ import annotations

import logging
import time
from typing import Any

import telegram.error

logger = logging.getLogger(__name__)

# chat_id → monotonic timestamp of the last successful send_chat_action call
_typing_last_sent: dict[int, float] = {}

# Minimum interval between send_chat_action calls for the same chat_id.
# Telegram shows "typing…" for ~5 s after each call; 4.5 s avoids redundant sends.
_TYPING_INTERVAL_S: float = 4.5


async def send_typing_action(bot: Any, chat_id: int) -> None:
    """Send a typing chat action with per-chat throttling and TelegramError swallowing.

    At most one call per _TYPING_INTERVAL_S per chat_id is forwarded to
    Telegram; calls within the window are silently skipped.
    TelegramError (including RetryAfter) is caught and logged at DEBUG —
    a cosmetic indicator must never propagate and abort a turn or image handler.
    Timestamp is stamped before the await so concurrent callers also see the gate.
    """
    now = time.monotonic()
    last = _typing_last_sent.get(chat_id, 0.0)
    if now - last < _TYPING_INTERVAL_S:
        return
    _typing_last_sent[chat_id] = now
    try:
        await bot.send_chat_action(chat_id=chat_id, action="typing")
    except telegram.error.TelegramError as exc:
        logger.debug("Typing action suppressed for chat %d: %s", chat_id, exc)
