"""Telegram command handlers.

Implements /start, /list, /new, /whoami plus the picker and permission
callback query handlers.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import aiosqlite

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from tgclaude.claude_bridge import pending_permissions, waiting_for_reason
from tgclaude.sessions import SessionInfo, list_sessions

logger = logging.getLogger(__name__)

_MAX_SESSIONS_IN_PICKER = 10


# ---------------------------------------------------------------------------
# /start
# ---------------------------------------------------------------------------


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Welcome on first interaction, then show session picker.

    Read-only per §5 — does not change the active session.
    """
    if update.message is None or update.effective_user is None:
        return

    user_id = update.effective_user.id
    config = context.bot_data["config"]

    if user_id not in config.allowed_user_ids:
        logger.debug("Ignoring /start from unlisted user %d", user_id)
        return

    db = context.bot_data["db"]
    # welcomed_{user_id}: per-user first-run flag (intentional extension to the settings table)
    welcomed_key = f"welcomed_{user_id}"
    already_welcomed = await db.get_setting(welcomed_key)

    if not already_welcomed:
        await db.set_setting(welcomed_key, "1")
        greeting = (
            "👋 Welcome to <b>tgclaude</b>!\n"
            "I bridge your Claude Max plan to Telegram. "
            "Pick a session below or start a new one.\n\n"
        )
    else:
        greeting = ""

    active_uuid = await db.get_active_session(user_id)
    picker_text, keyboard = await _build_picker(config, active_uuid, prefix=greeting)
    await update.message.reply_html(picker_text, reply_markup=keyboard)


# ---------------------------------------------------------------------------
# /list
# ---------------------------------------------------------------------------


async def list_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Re-show the session picker.  Read-only per §5."""
    if update.message is None or update.effective_user is None:
        return

    user_id = update.effective_user.id
    config = context.bot_data["config"]

    if user_id not in config.allowed_user_ids:
        logger.debug("Ignoring /list from unlisted user %d", user_id)
        return

    db = context.bot_data["db"]
    active_uuid = await db.get_active_session(user_id)
    picker_text, keyboard = await _build_picker(config, active_uuid)
    await update.message.reply_html(picker_text, reply_markup=keyboard)


# ---------------------------------------------------------------------------
# /new
# ---------------------------------------------------------------------------


async def new_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Clear the active session and dequeue pending messages.

    Per §5 turn serialization:
    1. Purge user's pending message queue.
    2. db.clear_active_session() immediately.
    3. If a turn is in-flight: set detach_after_turn, clear reattach_after_turn.
    4. Clear any WAITING_FOR_REASON state and resolve pending permission as deny.
    """
    if update.message is None or update.effective_user is None:
        return

    user_id = update.effective_user.id
    config = context.bot_data["config"]

    if user_id not in config.allowed_user_ids:
        logger.debug("Ignoring /new from unlisted user %d", user_id)
        return

    from tgclaude.handlers.messages import (
        _user_locks,
        _user_queues,
        detach_after_turn,
        reattach_after_turn,
    )

    db = context.bot_data["db"]

    # Read BEFORE clearing (fix for dead code bug)
    current_session = await db.get_active_session(user_id)

    # 1. Purge the message queue
    _purge_queue(user_id)

    # 2. Immediately detach in DB
    await db.clear_active_session(user_id)

    # 3. Resolve any pending "waiting for reason" permission as plain deny
    _cancel_waiting_for_reason(user_id)

    # 4. Deferred flag only when a turn is in-flight
    lock = _user_locks.get(user_id)
    turn_in_flight = lock is not None and lock.locked()

    if turn_in_flight:
        detach_after_turn[user_id] = True
        reattach_after_turn.pop(user_id, None)
        reply = "Starting a new conversation after Claude finishes the current turn."
    else:
        detach_after_turn.pop(user_id, None)
        reattach_after_turn.pop(user_id, None)
        if current_session is None:
            reply = "Already detached \u2014 send a message to start a new session."
        else:
            reply = "Session cleared. Send a message to start a new conversation."

    await update.message.reply_text(reply)


# ---------------------------------------------------------------------------
# /whoami
# ---------------------------------------------------------------------------


async def whoami_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Debug: show user_id and active session UUID."""
    if update.message is None or update.effective_user is None:
        return

    user_id = update.effective_user.id
    config = context.bot_data["config"]

    if user_id not in config.allowed_user_ids:
        logger.debug("Ignoring /whoami from unlisted user %d", user_id)
        return

    db = context.bot_data["db"]
    session_uuid = await db.get_active_session(user_id)
    session_display = session_uuid or "<i>none (detached)</i>"

    await update.message.reply_html(
        f"<b>User ID:</b> <code>{user_id}</code>\n"
        f"<b>Active session:</b> <code>{session_display}</code>"
    )


# ---------------------------------------------------------------------------
# Picker callback
# ---------------------------------------------------------------------------


async def picker_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle pick:{uuid} and pick:new callback_data.

    Per §5:
    - Purge queue.
    - Set active session immediately.
    - If a turn is in-flight: set reattach_after_turn (or detach if new).
    """
    query = update.callback_query
    if query is None or update.effective_user is None:
        return

    user_id = update.effective_user.id
    config = context.bot_data["config"]

    if user_id not in config.allowed_user_ids:
        logger.debug("Ignoring picker callback from unlisted user %d", user_id)
        await query.answer()
        return

    from tgclaude.handlers.messages import (
        _user_locks,
        _user_queues,
        detach_after_turn,
        reattach_after_turn,
    )

    db = context.bot_data["db"]

    data: str = query.data or ""
    # data format: "pick:<uuid>" or "pick:new"
    _, _, payload = data.partition(":")

    # Purge message queue
    _purge_queue(user_id)

    # Cancel any "waiting for reason" state — changing session invalidates the prompt
    _cancel_waiting_for_reason(user_id)

    lock = _user_locks.get(user_id)
    turn_in_flight = lock is not None and lock.locked()

    if payload == "new":
        await db.clear_active_session(user_id)
        if turn_in_flight:
            detach_after_turn[user_id] = True
            reattach_after_turn.pop(user_id, None)
            reply_text = "Will start a new conversation after the current turn finishes."
        else:
            detach_after_turn.pop(user_id, None)
            reattach_after_turn.pop(user_id, None)
            reply_text = "Ready for a new conversation. Send a message to start."
    else:
        new_uuid = payload
        existing_user = await db.get_user_for_session(new_uuid)
        if existing_user is not None and existing_user != user_id:
            await query.answer("This session is already in use by another user.")
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text="That session is currently attached to another user. Choose a different one.",
            )
            return
        # Guard against the check-then-act race: two concurrent users could
        # both pass the get_user_for_session check above and then race here.
        # The UNIQUE constraint on session_uuid will reject the second write.
        try:
            await db.set_active_session(user_id, new_uuid)
        except aiosqlite.IntegrityError:
            await query.answer("This session is already in use by another user.")
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text="That session was just attached by another user. Choose a different one.",
            )
            return
        if turn_in_flight:
            reattach_after_turn[user_id] = new_uuid
            detach_after_turn.pop(user_id, None)
            reply_text = (
                f"Will switch to session <code>{new_uuid[:8]}</code> "
                "after the current turn finishes."
            )
        else:
            detach_after_turn.pop(user_id, None)
            reattach_after_turn.pop(user_id, None)
            reply_text = (
                f"Attached to session <code>{new_uuid[:8]}</code>. "
                "Send a message to continue."
            )

    await query.answer()
    await query.edit_message_reply_markup(reply_markup=None)
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=reply_text,
        parse_mode="HTML",
    )


# ---------------------------------------------------------------------------
# Permission callback
# ---------------------------------------------------------------------------


async def permission_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle perm:yes/always/no/why callback_data.

    Per §6:
    - Verify user is in ALLOWED_USER_IDS.
    - Verify a pending future exists.
    - Resolve the future.
    - For 'always': mark in result dict (bridge adds grant).
    - For 'why': enter WAITING_FOR_REASON state.
    """
    query = update.callback_query
    if query is None or update.effective_user is None:
        return

    user_id = update.effective_user.id
    config = context.bot_data["config"]

    if user_id not in config.allowed_user_ids:
        logger.debug("Ignoring permission callback from unlisted user %d", user_id)
        await query.answer()
        return

    data: str = query.data or ""
    # format: "perm:<choice>:<tool_use_id>"
    parts = data.split(":", 2)
    if len(parts) != 3:
        logger.debug("Malformed permission callback_data: %r", data)
        await query.answer()
        return

    _, choice, tool_use_id = parts
    key = (user_id, tool_use_id)
    future = pending_permissions.get(key)

    if future is None or future.done():
        logger.debug(
            "No pending future for (user=%d, tool_use_id=%s)", user_id, tool_use_id
        )
        await query.answer("This prompt has already been resolved.")
        return

    if choice == "yes":
        future.set_result({"allow": True})
        await query.answer("Allowed.")

    elif choice == "always":
        future.set_result({"allow": True, "always": True})
        await query.answer("Always allowing this tool for this session.")

    elif choice == "no":
        future.set_result({"allow": False})
        await query.answer("Denied.")

    elif choice == "why":
        # Don't resolve yet — wait for the next text message
        if user_id in waiting_for_reason:
            # Cancel any prior pending reason
            old_id = waiting_for_reason[user_id]
            old_key = (user_id, old_id)
            old_future = pending_permissions.get(old_key)
            if old_future and not old_future.done():
                old_future.set_result({"allow": False})

        waiting_for_reason[user_id] = tool_use_id
        await query.answer("Send a message explaining why Claude should not use this tool.")
        # Keep the keyboard — do not edit yet, the message handler will
        return
    else:
        logger.warning("Unknown permission choice %r in callback_data", choice)
        await query.answer()
        return


# ---------------------------------------------------------------------------
# Picker builder
# ---------------------------------------------------------------------------


async def _build_picker(
    config,
    active_uuid: str | None,
    prefix: str = "",
) -> tuple[str, InlineKeyboardMarkup]:
    """Build the session picker text + keyboard."""
    sessions = await list_sessions(config.claude_home, config.claude_project_cwd)
    sessions = sessions[:_MAX_SESSIONS_IN_PICKER]

    now = datetime.now(timezone.utc)

    if not sessions:
        text = prefix + "No sessions yet \u2014 tap <b>New</b> to start one."
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("\u25b6 New conversation", callback_data="pick:new"),
        ]])
        return text, keyboard

    rows: list[list[InlineKeyboardButton]] = []
    for s in sessions:
        label = f"\U0001f4dd {s.title}  \u00b7  {_relative_time(s.mtime, now)}"
        rows.append([
            InlineKeyboardButton(label, callback_data=f"pick:{s.session_uuid}")
        ])

    rows.append([
        InlineKeyboardButton("\u25b6 New conversation", callback_data="pick:new")
    ])

    text = prefix + "Choose a session:"
    return text, InlineKeyboardMarkup(rows)


def _relative_time(mtime: datetime, now: datetime) -> str:
    """Format mtime relative to now as 'Xm ago', 'Xh ago', 'yesterday', or 'Xd ago'."""
    # Make mtime timezone-aware (it may be naive local time from os.stat).
    # datetime.fromtimestamp(st_mtime) returns naive local time; .timestamp()
    # converts it back to a Unix epoch treating it as local, then
    # fromtimestamp(..., tz=utc) gives the correct UTC-aware datetime.
    if mtime.tzinfo is None:
        mtime = datetime.fromtimestamp(mtime.timestamp(), tz=timezone.utc)

    delta_s = (now - mtime).total_seconds()
    if delta_s < 0:
        return "just now"

    minutes = int(delta_s // 60)
    hours = int(delta_s // 3600)
    days = int(delta_s // 86400)

    if minutes < 60:
        return f"{max(minutes, 1)}m ago"
    if hours < 24:
        return f"{hours}h ago"
    if days == 1:
        return "yesterday"
    return f"{days}d ago"


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _purge_queue(user_id: int) -> None:
    """Drain all pending messages from the user's queue and cancel any active drain.

    Sets _drain_cancelled so that an in-progress _drain_queue loop aborts after
    its current run_turn completes rather than processing stale queued messages.
    """
    from tgclaude.handlers.messages import _user_queues, _drain_cancelled

    queue = _user_queues.get(user_id)
    if queue:
        while not queue.empty():
            try:
                queue.get_nowait()
            except Exception:
                break
    _drain_cancelled.add(user_id)
    logger.debug("Purged and cancelled drain for user %d", user_id)


def _cancel_waiting_for_reason(user_id: int) -> None:
    """If the user is waiting to type a denial reason, cancel it as a plain deny."""
    tool_use_id = waiting_for_reason.pop(user_id, None)
    if tool_use_id is None:
        return

    future = pending_permissions.get((user_id, tool_use_id))
    if future and not future.done():
        logger.debug(
            "Cancelling waiting_for_reason for user %d tool_use_id %s",
            user_id,
            tool_use_id,
        )
        future.set_result({"allow": False})
