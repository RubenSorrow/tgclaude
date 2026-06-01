"""Tests for photo and document routing in tgclaude.handlers.messages.

These tests verify observable behaviour: what turn payload reaches _submit_turn,
and what messages are sent back to the user on failure paths.

Mocking strategy (Khorikov):
- _download_and_normalize is an I/O boundary (Telegram + Pillow) → patched.
- _submit_turn is the observable output boundary → patched to capture the turn.
- bot, update, context are unmanaged dependencies (Telegram SDK) → AsyncMock/MagicMock.
- Internal state dicts (_album_buffers etc.) are NEVER asserted on directly.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tgclaude.handlers.messages import PendingTurn


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
    """When _download_and_normalize raises ValueError (corrupt), the user sees
    a message mentioning 'not supported or the file is corrupt'."""
    from tgclaude.handlers.messages import photo_handler

    update = _make_update(_make_photo_message())
    ctx = _make_context()

    with patch(
        "tgclaude.handlers.messages._download_and_normalize",
        new=AsyncMock(side_effect=ValueError("unsupported or corrupt image")),
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
    """document_handler must send the corrupt-image message on ValueError."""
    from tgclaude.handlers.messages import document_handler

    update = _make_update(_make_document_message(mime_type="image/png"))
    ctx = _make_context()

    with patch(
        "tgclaude.handlers.messages._download_and_normalize",
        new=AsyncMock(side_effect=ValueError("unsupported or corrupt image")),
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
