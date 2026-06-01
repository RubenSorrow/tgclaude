"""Tests for photo and document routing in tgclaude.handlers.messages.

These tests verify observable behaviour: what turn payload reaches _submit_turn,
and what messages are sent back to the user on failure paths.

Mocking strategy (Khorikov):
- _download_and_normalize is an I/O boundary (Telegram + Pillow) → patched.
- _submit_turn is the observable output boundary → patched to capture the turn.
- bot, update, context are unmanaged dependencies (Telegram SDK) → AsyncMock/MagicMock.
- Internal state dicts (_album_buffers etc.) are asserted on ONLY for cleanup
  verification (test_album_state_cleaned_up_after_flush) and test seeding
  (_seed_album), where no public API exists to drive the state otherwise.
  Business-logic assertions target the PendingTurn passed to _submit_turn.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from tgclaude.handlers.messages import PendingTurn, _AlbumState


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

_USER_ID = 42
_CHAT_ID = 99
_FILE_ID = "file-abc-123"

# A minimal image block as _download_and_normalize would return.
_IMAGE_BLOCK = {
    "type": "image",
    "source": {
        "type": "base64",
        "media_type": "image/jpeg",
        "data": "AAAA",
    },
}


def _make_photo_message(
    caption: str | None = None,
    media_group_id: str | None = None,
) -> MagicMock:
    """Build a mock Telegram Message carrying a photo."""
    photo_size = MagicMock()
    photo_size.file_id = _FILE_ID

    msg = MagicMock()
    msg.photo = [photo_size]
    msg.caption = caption
    msg.media_group_id = media_group_id
    msg.reply_text = AsyncMock()
    return msg


def _make_document_message(
    mime_type: str,
    caption: str | None = None,
    media_group_id: str | None = None,
) -> MagicMock:
    """Build a mock Telegram Message carrying a document."""
    doc = MagicMock()
    doc.file_id = _FILE_ID
    doc.mime_type = mime_type

    msg = MagicMock()
    msg.document = doc
    msg.caption = caption
    msg.media_group_id = media_group_id
    msg.reply_text = AsyncMock()
    return msg


def _make_update(message: MagicMock) -> MagicMock:
    """Build a mock Update wrapping the given message."""
    user = MagicMock()
    user.id = _USER_ID

    chat = MagicMock()
    chat.id = _CHAT_ID

    update = MagicMock()
    update.message = message
    update.effective_user = user
    update.effective_chat = chat
    return update


def _make_context() -> MagicMock:
    """Build a minimal mock Context with bot and bot_data."""
    config = MagicMock()
    config.allowed_user_ids = [_USER_ID]

    bot = MagicMock()
    bot.send_chat_action = AsyncMock()
    bot.send_message = AsyncMock()

    bridge = MagicMock()
    bridge.run_turn = AsyncMock()

    ctx = MagicMock()
    ctx.bot = bot
    ctx.bot_data = {"config": config, "claude_bridge": bridge}
    return ctx


# ---------------------------------------------------------------------------
# photo_handler — happy paths
# ---------------------------------------------------------------------------


async def test_photo_accepted_submits_turn_with_image_block() -> None:
    """A single photo (no album) must reach _submit_turn with the image block."""
    from tgclaude.handlers.messages import photo_handler

    update = _make_update(_make_photo_message())
    ctx = _make_context()

    with patch(
        "tgclaude.handlers.messages._download_and_normalize",
        new=AsyncMock(return_value=_IMAGE_BLOCK),
    ), patch(
        "tgclaude.handlers.messages._submit_turn", new=AsyncMock()
    ) as mock_submit:
        await photo_handler(update, ctx)

    mock_submit.assert_called_once()
    _user_id, _chat_id, turn, *_ = mock_submit.call_args.args
    assert isinstance(turn, PendingTurn)
    assert turn.images == [_IMAGE_BLOCK]
    assert turn.text is None


async def test_photo_with_caption_submits_turn_with_caption() -> None:
    """A photo with caption must deliver caption in the PendingTurn."""
    from tgclaude.handlers.messages import photo_handler

    update = _make_update(_make_photo_message(caption="what is this?"))
    ctx = _make_context()

    with patch(
        "tgclaude.handlers.messages._download_and_normalize",
        new=AsyncMock(return_value=_IMAGE_BLOCK),
    ), patch(
        "tgclaude.handlers.messages._submit_turn", new=AsyncMock()
    ) as mock_submit:
        await photo_handler(update, ctx)

    mock_submit.assert_called_once()
    _user_id, _chat_id, turn, *_ = mock_submit.call_args.args
    assert turn.caption == "what is this?"
    assert turn.images == [_IMAGE_BLOCK]


# ---------------------------------------------------------------------------
# document_handler — happy paths
# ---------------------------------------------------------------------------


async def test_image_document_accepted_submits_turn() -> None:
    """An image/jpeg document must reach _submit_turn with the image block."""
    from tgclaude.handlers.messages import document_handler

    update = _make_update(_make_document_message(mime_type="image/jpeg"))
    ctx = _make_context()

    with patch(
        "tgclaude.handlers.messages._download_and_normalize",
        new=AsyncMock(return_value=_IMAGE_BLOCK),
    ), patch(
        "tgclaude.handlers.messages._submit_turn", new=AsyncMock()
    ) as mock_submit:
        await document_handler(update, ctx)

    mock_submit.assert_called_once()
    _user_id, _chat_id, turn, *_ = mock_submit.call_args.args
    assert isinstance(turn, PendingTurn)
    assert turn.images == [_IMAGE_BLOCK]


async def test_png_document_accepted_submits_turn() -> None:
    """An image/png document must also be accepted and submitted."""
    from tgclaude.handlers.messages import document_handler

    update = _make_update(_make_document_message(mime_type="image/png"))
    ctx = _make_context()

    with patch(
        "tgclaude.handlers.messages._download_and_normalize",
        new=AsyncMock(return_value=_IMAGE_BLOCK),
    ), patch(
        "tgclaude.handlers.messages._submit_turn", new=AsyncMock()
    ) as mock_submit:
        await document_handler(update, ctx)

    mock_submit.assert_called_once()


# ---------------------------------------------------------------------------
# document_handler — non-image rejection
# ---------------------------------------------------------------------------


async def test_non_image_document_rejected_with_unsupported_message() -> None:
    """A PDF document must be rejected; reply must contain 'I only understand text messages.'."""
    from tgclaude.handlers.messages import document_handler

    msg = _make_document_message(mime_type="application/pdf")
    update = _make_update(msg)
    ctx = _make_context()

    with patch(
        "tgclaude.handlers.messages._submit_turn", new=AsyncMock()
    ) as mock_submit:
        await document_handler(update, ctx)

    mock_submit.assert_not_called()
    msg.reply_text.assert_called_once()
    reply_text = msg.reply_text.call_args.args[0]
    assert "I only understand text messages." in reply_text


# ---------------------------------------------------------------------------
# Error handling — download failure
# ---------------------------------------------------------------------------


async def test_photo_download_failure_sends_could_not_download_message() -> None:
    """When _download_and_normalize raises RuntimeError, the user sees 'Could not download'."""
    from tgclaude.handlers.messages import photo_handler

    update = _make_update(_make_photo_message())
    ctx = _make_context()

    with patch(
        "tgclaude.handlers.messages._download_and_normalize",
        new=AsyncMock(side_effect=RuntimeError("network error")),
    ), patch(
        "tgclaude.handlers.messages._submit_turn", new=AsyncMock()
    ) as mock_submit:
        await photo_handler(update, ctx)

    mock_submit.assert_not_called()
    ctx.bot.send_message.assert_called_once()
    sent_text = ctx.bot.send_message.call_args.kwargs.get(
        "text", ctx.bot.send_message.call_args.args[0] if ctx.bot.send_message.call_args.args else ""
    )
    assert "Could not download" in sent_text


async def test_document_download_failure_sends_could_not_download_message() -> None:
    """document_handler must also send 'Could not download' on RuntimeError."""
    from tgclaude.handlers.messages import document_handler

    update = _make_update(_make_document_message(mime_type="image/jpeg"))
    ctx = _make_context()

    with patch(
        "tgclaude.handlers.messages._download_and_normalize",
        new=AsyncMock(side_effect=RuntimeError("timeout")),
    ), patch(
        "tgclaude.handlers.messages._submit_turn", new=AsyncMock()
    ) as mock_submit:
        await document_handler(update, ctx)

    mock_submit.assert_not_called()
    ctx.bot.send_message.assert_called_once()
    sent_text = ctx.bot.send_message.call_args.kwargs.get(
        "text", ctx.bot.send_message.call_args.args[0] if ctx.bot.send_message.call_args.args else ""
    )
    assert "Could not download" in sent_text


# ---------------------------------------------------------------------------
# Error handling — corrupt image
# ---------------------------------------------------------------------------


async def test_photo_corrupt_image_sends_not_supported_message() -> None:
    """When _download_and_normalize raises UnsupportedImageError (corrupt), the user sees
    a message mentioning 'not supported or the file is corrupt'."""
    from tgclaude.handlers.messages import photo_handler
    from tgclaude.image_processor import UnsupportedImageError

    update = _make_update(_make_photo_message())
    ctx = _make_context()

    with patch(
        "tgclaude.handlers.messages._download_and_normalize",
        new=AsyncMock(side_effect=UnsupportedImageError("unsupported or corrupt image")),
    ), patch(
        "tgclaude.handlers.messages._submit_turn", new=AsyncMock()
    ) as mock_submit:
        await photo_handler(update, ctx)

    mock_submit.assert_not_called()
    ctx.bot.send_message.assert_called_once()
    sent_text = ctx.bot.send_message.call_args.kwargs.get(
        "text", ctx.bot.send_message.call_args.args[0] if ctx.bot.send_message.call_args.args else ""
    )
    assert "not supported or the file is corrupt" in sent_text


async def test_document_corrupt_image_sends_not_supported_message() -> None:
    """document_handler must send the corrupt-image message on UnsupportedImageError."""
    from tgclaude.handlers.messages import document_handler
    from tgclaude.image_processor import UnsupportedImageError

    update = _make_update(_make_document_message(mime_type="image/png"))
    ctx = _make_context()

    with patch(
        "tgclaude.handlers.messages._download_and_normalize",
        new=AsyncMock(side_effect=UnsupportedImageError("unsupported or corrupt image")),
    ), patch(
        "tgclaude.handlers.messages._submit_turn", new=AsyncMock()
    ) as mock_submit:
        await document_handler(update, ctx)

    mock_submit.assert_not_called()
    ctx.bot.send_message.assert_called_once()
    sent_text = ctx.bot.send_message.call_args.kwargs.get(
        "text", ctx.bot.send_message.call_args.args[0] if ctx.bot.send_message.call_args.args else ""
    )
    assert "not supported or the file is corrupt" in sent_text


# ---------------------------------------------------------------------------
# Error handling — typed domain exceptions (IMG-08, GAP-2b)
# ---------------------------------------------------------------------------


async def test_image_too_large_error_sends_too_large_message() -> None:
    """When normalize_image raises ImageTooLargeError, the user sees 'too large to process'."""
    from tgclaude.handlers.messages import photo_handler
    from tgclaude.image_processor import ImageTooLargeError

    update = _make_update(_make_photo_message())
    ctx = _make_context()

    with patch(
        "tgclaude.handlers.messages._download_and_normalize",
        new=AsyncMock(side_effect=ImageTooLargeError("image too big")),
    ), patch(
        "tgclaude.handlers.messages._submit_turn", new=AsyncMock()
    ) as mock_submit:
        await photo_handler(update, ctx)

    mock_submit.assert_not_called()
    ctx.bot.send_message.assert_called_once()
    sent_text = ctx.bot.send_message.call_args.kwargs.get(
        "text", ctx.bot.send_message.call_args.args[0] if ctx.bot.send_message.call_args.args else ""
    )
    assert "too large" in sent_text


async def test_unsupported_image_error_sends_not_supported_message() -> None:
    """When normalize_image raises UnsupportedImageError, the user sees 'not supported or corrupt'."""
    from tgclaude.handlers.messages import photo_handler
    from tgclaude.image_processor import UnsupportedImageError

    update = _make_update(_make_photo_message())
    ctx = _make_context()

    with patch(
        "tgclaude.handlers.messages._download_and_normalize",
        new=AsyncMock(side_effect=UnsupportedImageError("bad format")),
    ), patch(
        "tgclaude.handlers.messages._submit_turn", new=AsyncMock()
    ) as mock_submit:
        await photo_handler(update, ctx)

    mock_submit.assert_not_called()
    ctx.bot.send_message.assert_called_once()
    sent_text = ctx.bot.send_message.call_args.kwargs.get(
        "text", ctx.bot.send_message.call_args.args[0] if ctx.bot.send_message.call_args.args else ""
    )
    assert "not supported or the file is corrupt" in sent_text


# ---------------------------------------------------------------------------
# Album accumulator tests — IMG-04 (GAP-1)
#
# Two styles are used:
# - Direct state seeding via _seed_album / _AlbumState: tests flush logic,
#   ordering, and overflow handling without waiting for real timers.
# - Concurrent asyncio tasks calling _handle_incoming_image directly: tests
#   the real handler path including inflight tracking and error-path cleanup.
# ---------------------------------------------------------------------------

import tgclaude.handlers.messages as _msgs  # noqa: E402  (import after main body)

_ALBUM_ID = "album-group-1"
_ALBUM_ID_2 = "album-group-2"
_IMAGE_BLOCK_2 = {
    "type": "image",
    "source": {
        "type": "base64",
        "media_type": "image/jpeg",
        "data": "BBBB",
    },
}


def _seed_album(
    media_group_id: str,
    blocks: list[dict],
    caption: str | None = None,
    user_id: int = _USER_ID,
    chat_id: int = _CHAT_ID,
) -> None:
    """Pre-populate module-level album state as if _accumulate_album_image had run.

    Blocks are stored as (arrival_index, block) tuples matching the production
    format. arrival_counter is set to len(blocks) as if all arrivals were logged.
    inflight_count is set to 0: all downloads completed before flush was seeded.
    """
    _msgs._albums[media_group_id] = _AlbumState(
        buffer=[(i, b) for i, b in enumerate(blocks)],
        caption=caption,
        timer=MagicMock(),  # No real timer handle needed — we call _flush_album_async directly.
        user_id=user_id,
        chat_id=chat_id,
        overflow_notified=False,
        arrival_counter=len(blocks),
        inflight_count=0,
    )


def _cleanup_album(media_group_id: str) -> None:
    """Remove any leftover album state after a test."""
    _msgs._albums.pop(media_group_id, None)


async def test_album_flush_submits_both_images_in_single_turn() -> None:
    """Two images buffered for the same album must be submitted as one PendingTurn
    with both blocks in the images list."""
    from tgclaude.handlers.messages import _flush_album_async

    _seed_album(_ALBUM_ID, [_IMAGE_BLOCK, _IMAGE_BLOCK_2])
    bot = MagicMock()
    ctx = _make_context()

    with patch(
        "tgclaude.handlers.messages._submit_turn", new=AsyncMock()
    ) as mock_submit:
        await _flush_album_async(_ALBUM_ID, bot, ctx)

    mock_submit.assert_called_once()
    _user_id, _chat_id, turn, *_ = mock_submit.call_args.args
    assert isinstance(turn, PendingTurn)
    assert turn.images == [_IMAGE_BLOCK, _IMAGE_BLOCK_2]


async def test_album_flush_preserves_caption_from_first_image() -> None:
    """The caption set on the first album image must appear in the flushed PendingTurn.
    A second image with no caption must not overwrite it."""
    from tgclaude.handlers.messages import _flush_album_async

    _seed_album(_ALBUM_ID, [_IMAGE_BLOCK, _IMAGE_BLOCK_2], caption="describe this album")
    bot = MagicMock()
    ctx = _make_context()

    with patch(
        "tgclaude.handlers.messages._submit_turn", new=AsyncMock()
    ) as mock_submit:
        await _flush_album_async(_ALBUM_ID, bot, ctx)

    mock_submit.assert_called_once()
    _user_id, _chat_id, turn, *_ = mock_submit.call_args.args
    assert turn.caption == "describe this album"


async def test_album_overflow_notification_fires_exactly_once() -> None:
    """Sending _ALBUM_MAX_IMAGES + 3 images must trigger the overflow bot.send_message
    exactly once — not three times."""
    from tgclaude.handlers.messages import _accumulate_album_image

    media_group_id = "overflow-group"
    bot = MagicMock()
    bot.send_message = AsyncMock()
    ctx = _make_context()

    # Pre-fill the buffer to _ALBUM_MAX_IMAGES using _seed_album so the timer
    # is a mock and won't fire.
    full_blocks = [_IMAGE_BLOCK] * _msgs._ALBUM_MAX_IMAGES
    _seed_album(media_group_id, full_blocks, user_id=_USER_ID, chat_id=_CHAT_ID)

    try:
        # Add 3 more images — each should hit the overflow branch.
        # arrival_index is irrelevant here: all hit the overflow path and are dropped.
        for i in range(3):
            await _accumulate_album_image(
                media_group_id=media_group_id,
                arrival_index=_msgs._ALBUM_MAX_IMAGES + i,
                block=_IMAGE_BLOCK_2,
                caption=None,
                user_id=_USER_ID,
                chat_id=_CHAT_ID,
                bot=bot,
                context=ctx,
            )
    finally:
        _cleanup_album(media_group_id)

    overflow_calls = [
        call for call in bot.send_message.call_args_list
        if "Album has more than" in (call.kwargs.get("text", "") or "")
    ]
    assert len(overflow_calls) == 1


async def test_album_flush_only_submits_up_to_max_images() -> None:
    """Even if the buffer somehow holds more than _ALBUM_MAX_IMAGES entries,
    the flushed turn must contain exactly _ALBUM_MAX_IMAGES images."""
    from tgclaude.handlers.messages import _flush_album_async

    media_group_id = "max-images-group"
    # Build exactly _ALBUM_MAX_IMAGES blocks (overflow protection is in accumulate,
    # but we verify flush passes through whatever is actually in the buffer).
    exactly_max_blocks = [_IMAGE_BLOCK] * _msgs._ALBUM_MAX_IMAGES
    _seed_album(media_group_id, exactly_max_blocks)
    bot = MagicMock()
    ctx = _make_context()

    with patch(
        "tgclaude.handlers.messages._submit_turn", new=AsyncMock()
    ) as mock_submit:
        await _flush_album_async(media_group_id, bot, ctx)

    mock_submit.assert_called_once()
    _user_id, _chat_id, turn, *_ = mock_submit.call_args.args
    assert len(turn.images) == _msgs._ALBUM_MAX_IMAGES


async def test_album_flush_reorders_out_of_order_arrivals() -> None:
    """Images buffered with reversed arrival indices must be sorted into send order.

    Seeds the buffer with (1, img_block_B) before (0, img_block_A) — the reverse
    of arrival order — then verifies the flushed PendingTurn reorders them so
    img_block_A (index 0) comes first. This test FAILS if the sort is removed.
    """
    from tgclaude.handlers.messages import _flush_album_async

    media_group_id = "out-of-order-group"
    img_block_A = _IMAGE_BLOCK       # arrival index 0 — the "first" image
    img_block_B = _IMAGE_BLOCK_2     # arrival index 1 — the "second" image

    # Seed with intentionally reversed insertion order: B then A.
    _msgs._albums[media_group_id] = _AlbumState(
        buffer=[(1, img_block_B), (0, img_block_A)],
        caption=None,
        timer=MagicMock(),
        user_id=_USER_ID,
        chat_id=_CHAT_ID,
        overflow_notified=False,
        arrival_counter=2,
        inflight_count=0,
    )

    bot = MagicMock()
    ctx = _make_context()

    with patch(
        "tgclaude.handlers.messages._submit_turn", new=AsyncMock()
    ) as mock_submit:
        await _flush_album_async(media_group_id, bot, ctx)

    mock_submit.assert_called_once()
    _user_id, _chat_id, turn, *_ = mock_submit.call_args.args
    assert isinstance(turn, PendingTurn)
    # Must be sorted by arrival index: A (0) before B (1), not insertion order.
    assert turn.images == [img_block_A, img_block_B]


async def test_album_state_cleaned_up_after_flush() -> None:
    """After _flush_album_async runs, the _albums entry for the flushed
    media_group_id must be gone — the whole state object is removed at once."""
    from tgclaude.handlers.messages import _flush_album_async

    _seed_album(_ALBUM_ID_2, [_IMAGE_BLOCK])
    bot = MagicMock()
    ctx = _make_context()

    with patch("tgclaude.handlers.messages._submit_turn", new=AsyncMock()):
        await _flush_album_async(_ALBUM_ID_2, bot, ctx)

    assert _ALBUM_ID_2 not in _msgs._albums


async def test_failed_download_while_sibling_inflight_preserves_counter() -> None:
    """Early download failure must NOT remove the album state while a sibling
    coroutine is still in-flight.

    Reproduces the race: A (index 0) and B (index 1) are both indexed before
    either download completes. A's download raises RuntimeError; B's succeeds.
    The correct inflight-aware guard in _release_album_inflight must keep the
    _albums entry alive while B is still in-flight so B can buffer its image
    at the correct arrival index (1).

    This test FAILS if the cleanup guard only checks `not state.buffer` without
    checking `inflight_count <= 0` — because when A fails, the buffer is empty
    and a naive guard would pop the state, discarding B's pending arrival index.

    Determinism note: the mock is keyed on file_id (not call order) so A always
    fails and B always succeeds regardless of which task's download resolves first.
    Both tasks assign their arrival indices synchronously before their first `await`,
    so the arrival_counter reaches 2 regardless of interleaving order.
    """
    import asyncio as _asyncio

    from tgclaude.handlers.messages import _handle_incoming_image

    gid = "album-race-test"

    # image B block — what the successful download returns
    _IMAGE_BLOCK_B = {
        "type": "image",
        "source": {"type": "base64", "media_type": "image/jpeg", "data": "BBBB"},
    }

    bot = MagicMock()
    bot.send_chat_action = AsyncMock()
    bot.send_message = AsyncMock()
    ctx = _make_context()

    # Keyed on file_id so outcome is independent of call order.
    # The asyncio.sleep(0) forces a genuine event-loop yield so both tasks advance
    # past their index assignment and send_chat_action BEFORE either download
    # resolves — reproducing the real concurrent-download race.
    async def download_by_file_id(file_id: str, _bot: object) -> dict:
        await _asyncio.sleep(0)  # yield so sibling tasks can advance to their await points
        if file_id == "file-A":
            raise RuntimeError("download failed")
        return _IMAGE_BLOCK_B

    try:
        with patch("tgclaude.handlers.messages._download_and_normalize", side_effect=download_by_file_id), \
             patch("tgclaude.handlers.messages._submit_turn", new=AsyncMock()):
            task_a = _asyncio.ensure_future(
                _handle_incoming_image(
                    file_id="file-A",
                    caption=None,
                    media_group_id=gid,
                    user_id=_USER_ID,
                    chat_id=_CHAT_ID,
                    bot=bot,
                    context=ctx,
                )
            )
            task_b = _asyncio.ensure_future(
                _handle_incoming_image(
                    file_id="file-B",
                    caption=None,
                    media_group_id=gid,
                    user_id=_USER_ID,
                    chat_id=_CHAT_ID,
                    bot=bot,
                    context=ctx,
                )
            )
            await _asyncio.gather(task_a, task_b)

        # After both tasks complete:
        # - A failed → its inflight slot released via _release_album_inflight.
        # - B succeeded → its image is in the buffer; inflight decremented.
        # The core invariant: _albums[gid] must survive because B buffered an image,
        # even though A failed before buffering anything.  If the inflight guard were
        # absent (checking only `not state.buffer`), A's failure would pop the state
        # while B was still in-flight, and B would re-create a fresh _AlbumState with
        # a reset arrival_counter — breaking ordering context for any later flush.
        assert gid in _msgs._albums, (
            "_albums entry must survive: B buffered an image even though A failed"
        )
        state = _msgs._albums[gid]
        # Exactly one image (B's) must be in the buffer; A failed so it contributed nothing.
        assert len(state.buffer) == 1, "exactly one image (B) must be buffered"
        _arrival_idx, buffered_block = state.buffer[0]
        assert buffered_block == _IMAGE_BLOCK_B, "the buffered block must be B's image"
        # Both tasks incremented arrival_counter before any await, so the counter
        # reflects two indexed arrivals regardless of which task ran first.
        assert state.arrival_counter == 2, "arrival_counter must reflect both indexed tasks"
    finally:
        _cleanup_album(gid)
