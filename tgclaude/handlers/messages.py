"""Free-text and image message handlers.

Implements per-user turn serialization, backpressure queuing, and the
WAITING_FOR_REASON state machine bypass (§5, §6).

Image handling stories implemented here:
  IMG-01 Single photo
  IMG-02 Image-as-document
  IMG-03 Caption as prompt
  IMG-04 Album debounce (2 s, max 10 images)
  IMG-07 Permission-mode agnosticism
  IMG-08 Error handling and feedback
"""

from __future__ import annotations

import asyncio
import base64
import logging
from dataclasses import dataclass, field
from typing import Any

from telegram import Update
from telegram.ext import ContextTypes

logger = logging.getLogger(__name__)

_MAX_QUEUE_SIZE = 5
_ALBUM_DEBOUNCE_S = 2.0
_ALBUM_MAX_IMAGES = 10

_IMAGE_MIME_TYPES: frozenset[str] = frozenset(
    {"image/png", "image/jpeg", "image/jpg", "image/webp"}
)

# ---------------------------------------------------------------------------
# Turn payload
# ---------------------------------------------------------------------------


@dataclass
class PendingTurn:
    """Carries the content of a single user turn: text, images, or both.

    image content blocks follow the Claude API shape:
        {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": "<b64>"}}
    """

    text: str | None = None
    images: list[dict] | None = None  # list of image content blocks
    caption: str | None = None        # text accompanying images


# ---------------------------------------------------------------------------
# Per-user state (module-level dicts as documented in the architecture)
# ---------------------------------------------------------------------------

# Per-user async locks: held for the duration of an in-flight SDK turn
_user_locks: dict[int, asyncio.Lock] = {}

# Per-user bounded message queues (max 5); carry PendingTurn objects
_user_queues: dict[int, asyncio.Queue[PendingTurn]] = {}

# Cancellation flags: set by _purge_queue when /new fires mid-drain
_drain_cancelled: set[int] = set()

# Deferred session flags — set by /new or picker during an in-flight turn
detach_after_turn: dict[int, bool] = {}
reattach_after_turn: dict[int, str] = {}  # user_id → new session UUID


# ---------------------------------------------------------------------------
# Album accumulator state (IMG-04)
# ---------------------------------------------------------------------------

_album_buffers: dict[str, list[dict]] = {}        # media_group_id → image blocks
_album_captions: dict[str, str | None] = {}       # media_group_id → first caption found
_album_timers: dict[str, asyncio.TimerHandle] = {} # media_group_id → pending timer
_album_meta: dict[str, tuple[int, int]] = {}       # media_group_id → (user_id, chat_id)


# ---------------------------------------------------------------------------
# Main text handler
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

    turn = PendingTurn(text=text)
    await _submit_turn(user_id, chat_id, turn, context.bot, context)


# ---------------------------------------------------------------------------
# Photo handler (IMG-01, IMG-03, IMG-04, IMG-07, IMG-08)
# ---------------------------------------------------------------------------


async def photo_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle photo messages from allow-listed users."""
    if update.message is None or update.effective_user is None:
        return

    user_id = update.effective_user.id
    config = context.bot_data["config"]

    if user_id not in config.allowed_user_ids:
        logger.debug("Dropping photo from unlisted user %d", user_id)
        return

    chat_id: int = update.effective_chat.id  # type: ignore[union-attr]
    bot = context.bot

    await bot.send_chat_action(chat_id=chat_id, action="typing")

    file_id: str = update.message.photo[-1].file_id
    media_group_id: str | None = update.message.media_group_id
    caption: str | None = update.message.caption

    try:
        block = await _download_and_normalize(file_id, bot)
    except RuntimeError as exc:
        logger.warning("Photo download failed for user %d: %s", user_id, exc)
        await bot.send_message(
            chat_id=chat_id,
            text="Could not download the image. Please try again.",
        )
        return
    except ValueError as exc:
        logger.warning("Photo normalization failed for user %d: %s", user_id, exc)
        await _send_image_error(exc, chat_id, bot)
        return

    if media_group_id:
        await _accumulate_album_image(
            media_group_id=media_group_id,
            block=block,
            caption=caption,
            user_id=user_id,
            chat_id=chat_id,
            bot=bot,
            context=context,
        )
        return

    turn = PendingTurn(images=[block], caption=caption)
    await _submit_turn(user_id, chat_id, turn, bot, context)


# ---------------------------------------------------------------------------
# Document handler (IMG-02, IMG-03, IMG-04, IMG-07, IMG-08)
# ---------------------------------------------------------------------------


async def document_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle document messages — pass image documents to vision pipeline."""
    if update.message is None or update.effective_user is None:
        return

    user_id = update.effective_user.id
    config = context.bot_data["config"]

    if user_id not in config.allowed_user_ids:
        logger.debug("Dropping document from unlisted user %d", user_id)
        return

    doc = update.message.document
    if doc is None:
        return

    mime_type: str = doc.mime_type or ""
    if mime_type not in _IMAGE_MIME_TYPES:
        # Non-image documents fall through to the unsupported handler response
        await unsupported_message_handler(update, context)
        return

    chat_id: int = update.effective_chat.id  # type: ignore[union-attr]
    bot = context.bot

    await bot.send_chat_action(chat_id=chat_id, action="typing")

    file_id: str = doc.file_id
    media_group_id: str | None = update.message.media_group_id
    caption: str | None = update.message.caption

    try:
        block = await _download_and_normalize(file_id, bot)
    except RuntimeError as exc:
        logger.warning("Document download failed for user %d: %s", user_id, exc)
        await bot.send_message(
            chat_id=chat_id,
            text="Could not download the image. Please try again.",
        )
        return
    except ValueError as exc:
        logger.warning("Document normalization failed for user %d: %s", user_id, exc)
        await _send_image_error(exc, chat_id, bot)
        return

    if media_group_id:
        await _accumulate_album_image(
            media_group_id=media_group_id,
            block=block,
            caption=caption,
            user_id=user_id,
            chat_id=chat_id,
            bot=bot,
            context=context,
        )
        return

    turn = PendingTurn(images=[block], caption=caption)
    await _submit_turn(user_id, chat_id, turn, bot, context)


# ---------------------------------------------------------------------------
# Image download and normalization (IMG-01, IMG-08)
# ---------------------------------------------------------------------------


async def _download_and_normalize(file_id: str, bot: Any) -> dict:
    """Download a Telegram file and return an image content block.

    Raises RuntimeError on download failure.
    Raises ValueError on corrupt/unsupported image or size limit exceeded.
    Returns a dict: {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": "<b64>"}}
    """
    from tgclaude.image_processor import normalize_image

    raw = await _download_telegram_file(file_id, bot)
    jpeg_bytes, media_type = await normalize_image(raw)
    data = base64.b64encode(jpeg_bytes).decode()
    return {
        "type": "image",
        "source": {"type": "base64", "media_type": media_type, "data": data},
    }


async def _download_telegram_file(file_id: str, bot: Any) -> bytes:
    """Fetch raw bytes for a Telegram file by ID.

    Raises RuntimeError if the download fails.
    """
    try:
        telegram_file = await bot.get_file(file_id)
        raw_bytearray = await telegram_file.download_as_bytearray()
        return bytes(raw_bytearray)
    except Exception as exc:
        raise RuntimeError(f"Download failed for file_id={file_id}") from exc


async def _send_image_error(exc: ValueError, chat_id: int, bot: Any) -> None:
    """Send the appropriate error message for a ValueError from normalize_image."""
    msg_lower = str(exc).lower()
    if "exceeding" in msg_lower or "limit" in msg_lower:
        text = "The image is too large to process even after resizing."
    else:
        text = "This image format is not supported or the file is corrupt."
    await bot.send_message(chat_id=chat_id, text=text)


# ---------------------------------------------------------------------------
# Album accumulator (IMG-04)
# ---------------------------------------------------------------------------


async def _accumulate_album_image(
    media_group_id: str,
    block: dict,
    caption: str | None,
    user_id: int,
    chat_id: int,
    bot: Any,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Buffer one image block into the album accumulator for media_group_id.

    If this is the first image in the group, arm the debounce timer.
    Drops extra images beyond _ALBUM_MAX_IMAGES and notifies the user once.
    """
    is_new_group = media_group_id not in _album_buffers

    if is_new_group:
        _album_buffers[media_group_id] = []
        _album_captions[media_group_id] = None
        _album_meta[media_group_id] = (user_id, chat_id)

    buffer = _album_buffers[media_group_id]

    if len(buffer) >= _ALBUM_MAX_IMAGES:
        if len(buffer) == _ALBUM_MAX_IMAGES:
            # First overflow: notify exactly once, then keep dropping
            await bot.send_message(
                chat_id=chat_id,
                text=f"Album has more than {_ALBUM_MAX_IMAGES} images; extras will be ignored.",
            )
        logger.debug(
            "Album %s exceeds max images (%d); dropping block",
            media_group_id, _ALBUM_MAX_IMAGES,
        )
        return

    buffer.append(block)

    if caption and _album_captions[media_group_id] is None:
        _album_captions[media_group_id] = caption

    if is_new_group:
        _arm_album_timer(media_group_id, bot, context)


def _arm_album_timer(
    media_group_id: str,
    bot: Any,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Schedule _flush_album to fire after _ALBUM_DEBOUNCE_S seconds."""
    loop = asyncio.get_event_loop()
    handle = loop.call_later(
        _ALBUM_DEBOUNCE_S,
        _flush_album,
        media_group_id,
        bot,
        loop,
        context,
    )
    _album_timers[media_group_id] = handle


def _flush_album(
    media_group_id: str,
    bot: Any,
    loop: asyncio.AbstractEventLoop,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Submit the buffered album as a single PendingTurn. Called by call_later.

    Schedules _flush_album_async as a coroutine task on the running loop.
    """
    loop.create_task(_flush_album_async(media_group_id, bot, context))


async def _flush_album_async(
    media_group_id: str,
    bot: Any,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Async body of the album flush: build a PendingTurn and submit it."""
    images = _album_buffers.pop(media_group_id, [])
    caption = _album_captions.pop(media_group_id, None)
    _album_timers.pop(media_group_id, None)
    meta = _album_meta.pop(media_group_id, None)

    if not images or meta is None:
        logger.warning("Album flush for %s had no images or metadata; skipping", media_group_id)
        return

    user_id, chat_id = meta
    turn = PendingTurn(images=images, caption=caption)
    logger.debug(
        "Flushing album %s: %d image(s) for user %d",
        media_group_id, len(images), user_id,
    )
    await _submit_turn(user_id, chat_id, turn, bot, context)


# ---------------------------------------------------------------------------
# Per-user turn submission (shared by text, single-image, and album flush)
# ---------------------------------------------------------------------------


async def _submit_turn(
    user_id: int,
    chat_id: int,
    turn: PendingTurn,
    bot: Any,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Enqueue or immediately execute a PendingTurn for user_id.

    Ensures per-user lock and queue exist, then either:
    - enqueues the turn if the lock is already held, or
    - acquires the lock, executes the turn, and drains the queue.
    """
    _ensure_user_state(user_id)
    lock = _user_locks[user_id]
    queue = _user_queues[user_id]

    if lock.locked():
        await _enqueue_or_reject(user_id, chat_id, turn, queue, bot)
        return

    async with lock:
        bridge = context.bot_data["claude_bridge"]
        _drain_cancelled.discard(user_id)

        await bridge.run_turn(
            user_id=user_id,
            turn=turn,
            bot=bot,
            chat_id=chat_id,
        )
        await _drain_queue(user_id, chat_id, bridge, bot, queue)


def _ensure_user_state(user_id: int) -> None:
    """Initialise per-user lock and queue if not already present."""
    if user_id not in _user_locks:
        _user_locks[user_id] = asyncio.Lock()
    if user_id not in _user_queues:
        _user_queues[user_id] = asyncio.Queue(maxsize=_MAX_QUEUE_SIZE)


async def _enqueue_or_reject(
    user_id: int,
    chat_id: int,
    turn: PendingTurn,
    queue: asyncio.Queue[PendingTurn],
    bot: Any,
) -> None:
    """Enqueue a turn when the lock is held, or reject it if the queue is full."""
    from tgclaude.claude_bridge import pending_permissions

    if queue.full():
        has_pending_perm = any(uid == user_id for (uid, _) in pending_permissions)
        if has_pending_perm:
            reply = "Still waiting for your permission tap above."
        else:
            reply = "Claude is still working on your previous message."
        await bot.send_message(chat_id=chat_id, text=reply)
        return

    await queue.put(turn)
    logger.debug("Enqueued turn for user %d (queue size: %d)", user_id, queue.qsize())


# ---------------------------------------------------------------------------
# Unsupported media handler
# ---------------------------------------------------------------------------


async def unsupported_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Reject non-text, non-command messages with a uniform one-liner."""
    if update.effective_user is None or update.message is None:
        return
    user_id = update.effective_user.id
    config = context.bot_data["config"]
    if user_id not in config.allowed_user_ids:
        return  # silent drop per §10
    await update.message.reply_text("I only understand text messages.")


# ---------------------------------------------------------------------------
# Queue drain
# ---------------------------------------------------------------------------


async def _drain_queue(
    user_id: int,
    chat_id: int,
    bridge: Any,
    bot: Any,
    queue: asyncio.Queue[PendingTurn],
) -> None:
    """Process all queued turns for user_id, sequentially, while lock is held.

    Checks _drain_cancelled after every turn so that a concurrent /new command
    can abort the drain loop without waiting for all remaining turns to finish.
    """
    while not queue.empty():
        try:
            queued_turn: PendingTurn = queue.get_nowait()
        except asyncio.QueueEmpty:
            break

        # Pre-turn check: /new may have fired after get_nowait() but before run_turn.
        if user_id in _drain_cancelled:
            _drain_cancelled.discard(user_id)
            logger.debug("Drain cancelled for user %d (pre-turn check)", user_id)
            break

        logger.debug("Draining queued turn for user %d", user_id)
        await bridge.run_turn(
            user_id=user_id,
            turn=queued_turn,
            bot=bot,
            chat_id=chat_id,
        )

        # Post-turn check: /new may have fired during run_turn.
        if user_id in _drain_cancelled:
            _drain_cancelled.discard(user_id)
            logger.debug("Drain cancelled for user %d (post-turn check)", user_id)
            break
