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

# Cancellation flags: set by _purge_queue when /new fires mid-drain
_drain_cancelled: set[int] = set()

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
            reason = update.message.text.strip()
            future.set_result({"allow": False, "message": reason})
            await update.message.reply_text("Denial reason sent to Claude.")
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
            has_pending_perm = any(uid == user_id for (uid, _) in pending_permissions)
            if user_id in waiting_for_reason or has_pending_perm:
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

        # Clear any stale cancellation from a previous /new before starting a
        # fresh drain so it does not spuriously abort the new turn.
        _drain_cancelled.discard(user_id)

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
# Unsupported media handler
# ---------------------------------------------------------------------------


async def unsupported_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Reject non-text, non-command messages with an explanatory reply.

    When the user is in WAITING_FOR_REASON, the rejection message acknowledges
    that state so the user knows to send text instead.
    """
    if update.effective_user is None or update.message is None:
        return
    user_id = update.effective_user.id
    config = context.bot_data["config"]
    if user_id not in config.allowed_user_ids:
        return  # silent drop per §10
    from tgclaude.claude_bridge import waiting_for_reason
    if user_id in waiting_for_reason:
        await update.message.reply_text(
            "Please send a text message explaining why Claude should not use this tool."
        )
    else:
        await update.message.reply_text("I only understand text messages.")


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
    """Process all queued messages for user_id, sequentially, while lock is held.

    Checks _drain_cancelled after every turn so that a concurrent /new command
    can abort the drain loop without waiting for all remaining turns to finish.
    """
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

        if user_id in _drain_cancelled:
            _drain_cancelled.discard(user_id)
            logger.debug("Drain cancelled for user %d after /new", user_id)
            break
