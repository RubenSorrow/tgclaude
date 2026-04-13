"""Free-text message handler.

Implements per-user turn serialization, backpressure queuing, and the
WAITING_FOR_REASON state machine bypass (§5, §6).
"""

from __future__ import annotations

import asyncio
import logging

from telegram import Update
from telegram.ext import ContextTypes

logger = logging.getLogger(__name__)

_MAX_QUEUE_SIZE = 5

# ---------------------------------------------------------------------------
# Per-user state (module-level dicts as documented in the architecture)
# ---------------------------------------------------------------------------

# Per-user async locks: held for the duration of an in-flight SDK turn
_user_locks: dict[int, asyncio.Lock] = {}

# Per-user bounded message queues (max 5)
_user_queues: dict[int, asyncio.Queue] = {}

# Deferred session flags — set by /new or picker during an in-flight turn
detach_after_turn: dict[int, bool] = {}
reattach_after_turn: dict[int, str] = {}  # user_id → new session UUID


# ---------------------------------------------------------------------------
# Main handler
# ---------------------------------------------------------------------------


async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle all free-text messages from allow-listed users.

    Per §5 turn serialization:
    1. Allow-list check (silent drop for unknown users).
    2. WAITING_FOR_REASON bypass: resolve pending permission, skip queue.
    3. Non-text message rejection.
    4. Lock check: enqueue or reject on contention.
    5. Acquire lock, execute turn, drain queue, release.
    """
    if update.message is None or update.effective_user is None:
        return

    user_id = update.effective_user.id
    config = context.bot_data["config"]

    # 1. Allow-list enforcement (silent drop per §10)
    if user_id not in config.allowed_user_ids:
        logger.debug("Dropping message from unlisted user %d", user_id)
        return

    chat_id: int = update.effective_chat.id  # type: ignore[union-attr]

    # 2. WAITING_FOR_REASON bypass — must happen before the non-text check
    from tgclaude.claude_bridge import waiting_for_reason, pending_permissions

    if user_id in waiting_for_reason:
        tool_use_id = waiting_for_reason.pop(user_id)
        key = (user_id, tool_use_id)
        future = pending_permissions.get(key)
        if future and not future.done():
            # Only text messages become the denial reason (§5)
            if update.message.text:
                reason = update.message.text.strip()
                future.set_result({"allow": False, "message": reason})
                await update.message.reply_text(
                    "Denial reason sent to Claude."
                )
            else:
                # Non-text in WAITING_FOR_REASON: reject, keep state
                waiting_for_reason[user_id] = tool_use_id
                await update.message.reply_text(
                    "I only understand text messages."
                )
        return

    # 3. Non-text messages
    if not update.message.text:
        await update.message.reply_text("I only understand text messages.")
        return

    text = update.message.text.strip()
    if not text:
        return

    # Ensure per-user structures exist
    if user_id not in _user_locks:
        _user_locks[user_id] = asyncio.Lock()
    if user_id not in _user_queues:
        _user_queues[user_id] = asyncio.Queue(maxsize=_MAX_QUEUE_SIZE)

    lock = _user_locks[user_id]
    queue = _user_queues[user_id]

    # 4. Contention handling
    if lock.locked():
        if queue.full():
            # Determine correct rejection message
            if user_id in waiting_for_reason:
                reply = "Still waiting for your permission tap above."
            else:
                reply = "Claude is still working on your previous message."
            await update.message.reply_text(reply)
            return
        # Enqueue for deferred processing
        await queue.put(text)
        logger.debug(
            "Enqueued message for user %d (queue size: %d)", user_id, queue.qsize()
        )
        return

    # 5. Acquire lock and process
    async with lock:
        bridge = context.bot_data["claude_bridge"]
        bot = context.bot

        # Process the immediate message
        await bridge.run_turn(
            user_id=user_id,
            text=text,
            bot=bot,
            chat_id=chat_id,
        )

        # Drain the queue sequentially
        await _drain_queue(user_id, chat_id, bridge, bot, queue)


# ---------------------------------------------------------------------------
# Queue drain
# ---------------------------------------------------------------------------


async def _drain_queue(
    user_id: int,
    chat_id: int,
    bridge,
    bot,
    queue: asyncio.Queue,
) -> None:
    """Process all queued messages for user_id, sequentially, while lock is held."""
    while not queue.empty():
        try:
            queued_text: str = queue.get_nowait()
        except asyncio.QueueEmpty:
            break

        logger.debug("Draining queued message for user %d", user_id)
        await bridge.run_turn(
            user_id=user_id,
            text=queued_text,
            bot=bot,
            chat_id=chat_id,
        )
