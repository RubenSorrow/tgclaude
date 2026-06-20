"""Tests for _extract_tool_result_images and _send_tool_result.

Strategy (Khorikov):
- _extract_tool_result_images is a pure function → output-based tests only.
- _send_tool_result has side effects through bot → communication-based tests
  (mock only the unmanaged dependency: bot.send_message, bot.send_photo,
  bot.send_chat_action).
"""

from __future__ import annotations

import base64
from unittest.mock import AsyncMock, MagicMock

import pytest
import telegram.error

from tgclaude.claude_bridge import (
    ClaudeBridge,
    _extract_tool_result_images,
)


# ---------------------------------------------------------------------------
# Helpers shared across both test groups
# ---------------------------------------------------------------------------


def _make_block(content, tool_use_id: str = "tu1", is_error: bool = False) -> MagicMock:
    """Return a minimal ToolResultBlock-like mock."""
    block = MagicMock()
    block.content = content
    block.tool_use_id = tool_use_id
    block.is_error = is_error
    return block


def _make_minimal_config(permission_mode: str = "bypass") -> MagicMock:
    config = MagicMock()
    config.permission_mode = permission_mode
    config.turn_timeout_s = 30
    config.claude_project_cwd = "/tmp"
    config.claude_binary = None
    return config


def _make_minimal_db() -> MagicMock:
    db = MagicMock()
    db.get_active_session = AsyncMock(return_value=None)
    db.get_setting = AsyncMock(return_value=None)
    db.set_active_session = AsyncMock()
    db.clear_active_session = AsyncMock()
    return db


def _make_minimal_permission_manager() -> MagicMock:
    perm = MagicMock()
    perm.has_grant = AsyncMock(return_value=False)
    perm.add_grant = AsyncMock()
    return perm


def _make_bridge() -> ClaudeBridge:
    return ClaudeBridge(
        config=_make_minimal_config(),
        db=_make_minimal_db(),
        permission_manager=_make_minimal_permission_manager(),
    )


def _make_bot() -> MagicMock:
    bot = MagicMock()
    bot.send_message = AsyncMock()
    bot.send_photo = AsyncMock()
    bot.send_chat_action = AsyncMock()
    return bot


# ---------------------------------------------------------------------------
# _extract_tool_result_images — pure function, output-based tests
# ---------------------------------------------------------------------------


def test_extract_images_empty_when_no_content() -> None:
    """block.content = None must return []."""
    block = _make_block(content=None)
    assert _extract_tool_result_images(block) == []


def test_extract_images_empty_when_content_is_string() -> None:
    """block.content as a plain string must return []."""
    block = _make_block(content="some raw text output")
    assert _extract_tool_result_images(block) == []


def test_extract_images_skips_text_blocks() -> None:
    """A content list containing only text-type dicts must return []."""
    block = _make_block(content=[
        {"type": "text", "text": "hello"},
        {"type": "text", "text": "world"},
    ])
    assert _extract_tool_result_images(block) == []


def test_extract_images_dict_form_base64() -> None:
    """A content list with one base64 image dict must return the extracted entry."""
    block = _make_block(content=[
        {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/png",
                "data": "AAAA",
            },
        }
    ])
    result = _extract_tool_result_images(block)
    assert result == [{"data": "AAAA", "media_type": "image/png"}]


def test_extract_images_object_form_base64() -> None:
    """A content list with a MagicMock object image item must be handled."""
    source = MagicMock()
    source.type = "base64"
    source.data = "BBBB"
    source.media_type = "image/jpeg"
    # Make isinstance(source, dict) return False
    source.__class__ = MagicMock  # not dict

    item = MagicMock()
    item.type = "image"
    item.source = source

    block = _make_block(content=[item])
    result = _extract_tool_result_images(block)
    assert result == [{"data": "BBBB", "media_type": "image/jpeg"}]


def test_extract_images_skips_non_base64_source() -> None:
    """An image block with source.type == 'url' must be skipped."""
    block = _make_block(content=[
        {
            "type": "image",
            "source": {
                "type": "url",
                "url": "https://example.com/img.png",
            },
        }
    ])
    assert _extract_tool_result_images(block) == []


def test_extract_images_mixed_list() -> None:
    """A list with a text dict and an image dict returns only the image entry."""
    block = _make_block(content=[
        {"type": "text", "text": "here is the screenshot"},
        {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/png",
                "data": "CCCC",
            },
        },
    ])
    result = _extract_tool_result_images(block)
    assert result == [{"data": "CCCC", "media_type": "image/png"}]


# ---------------------------------------------------------------------------
# _send_tool_result — communication-based tests (mock bot)
# ---------------------------------------------------------------------------


async def test_send_tool_result_skips_send_message_for_whitespace_only() -> None:
    """Whitespace-only content must not trigger bot.send_message."""
    bridge = _make_bridge()
    bot = _make_bot()
    block = _make_block(content="\n  \n")

    await bridge._send_tool_result(block, user_id=1, bot=bot, chat_id=100)

    bot.send_message.assert_not_called()


async def test_send_tool_result_sends_text_when_content_has_text() -> None:
    """Non-empty text content must trigger exactly one bot.send_message call."""
    bridge = _make_bridge()
    bot = _make_bot()
    block = _make_block(content="hello")

    await bridge._send_tool_result(block, user_id=1, bot=bot, chat_id=100)

    bot.send_message.assert_called_once()


async def test_send_tool_result_does_not_abort_on_telegram_error() -> None:
    """A TelegramError on send_message must be caught; function returns normally."""
    bridge = _make_bridge()
    bot = _make_bot()
    bot.send_message.side_effect = telegram.error.BadRequest("Text must be non-empty")
    block = _make_block(content="x")

    # Must not raise
    await bridge._send_tool_result(block, user_id=1, bot=bot, chat_id=100)

    bot.send_message.assert_called()


async def test_send_tool_result_sends_image_via_send_photo() -> None:
    """A block with a base64 image and empty text must call bot.send_photo once."""
    bridge = _make_bridge()
    bot = _make_bot()
    # "AAAA" is valid base64 (decodes to 3 bytes: 0x00 0x00 0x00)
    block = _make_block(content=[
        {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/png",
                "data": base64.b64encode(b"\x00\x00\x00").decode(),
            },
        }
    ])

    await bridge._send_tool_result(block, user_id=1, bot=bot, chat_id=100)

    bot.send_photo.assert_called_once()
    call_kwargs = bot.send_photo.call_args
    assert call_kwargs.kwargs.get("chat_id") == 100 or (
        len(call_kwargs.args) > 0 and call_kwargs.args[0] == 100
    )


async def test_send_tool_result_send_photo_error_does_not_abort() -> None:
    """A TelegramError on send_photo must be caught; function returns normally."""
    bridge = _make_bridge()
    bot = _make_bot()
    bot.send_photo.side_effect = telegram.error.TelegramError("fail")
    block = _make_block(content=[
        {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/png",
                "data": base64.b64encode(b"\x01\x02\x03").decode(),
            },
        }
    ])

    # Must not raise
    await bridge._send_tool_result(block, user_id=1, bot=bot, chat_id=100)

    bot.send_photo.assert_called_once()


async def test_send_tool_result_sends_both_text_and_image() -> None:
    """block.content as a list with text and image triggers both send_message and send_photo."""
    bridge = _make_bridge()
    bot = _make_bot()
    block = _make_block(content=[
        {"type": "text", "text": "result"},
        {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/png",
                "data": base64.b64encode(b"\xff\xfe\xfd").decode(),
            },
        },
    ])

    await bridge._send_tool_result(block, user_id=1, bot=bot, chat_id=100)

    bot.send_message.assert_called_once()
    bot.send_photo.assert_called_once()


# ---------------------------------------------------------------------------
# New tests: _send_tool_result — bad base64 (Fix #1)
# ---------------------------------------------------------------------------


async def test_send_tool_result_bad_base64_does_not_abort() -> None:
    """binascii.Error (a ValueError) from bad base64 must be caught; returns normally."""
    from unittest.mock import patch

    bridge = _make_bridge()
    bot = _make_bot()
    block = _make_block(content=None)  # no text content

    with patch(
        "tgclaude.claude_bridge._extract_tool_result_images",
        return_value=[{"data": "not-valid-base64!!!", "media_type": "image/png"}],
    ):
        # Must not raise despite malformed base64
        await bridge._send_tool_result(block, user_id=1, bot=bot, chat_id=100)

    bot.send_photo.assert_not_called()


# ---------------------------------------------------------------------------
# New tests: send_screenshot_bytes (unit tests for media.py)
# ---------------------------------------------------------------------------


async def test_send_screenshot_bytes_sends_photo_on_success() -> None:
    """Happy path: normalize_image succeeds, send_photo is called once, send_document is not."""
    from unittest.mock import patch, AsyncMock
    from tgclaude.media import send_screenshot_bytes

    bot = _make_bot()
    bot.send_document = AsyncMock()

    with patch(
        "tgclaude.image_processor.normalize_image",
        new=AsyncMock(return_value=(b"jpeg_data", "image/jpeg")),
    ):
        await send_screenshot_bytes(b"raw", bot, chat_id=100)

    bot.send_photo.assert_called_once()
    call_kwargs = bot.send_photo.call_args
    assert call_kwargs.kwargs.get("chat_id") == 100
    assert call_kwargs.kwargs.get("caption") == "📸 Screenshot"
    bot.send_document.assert_not_called()


async def test_send_screenshot_bytes_falls_back_to_document_on_photo_error() -> None:
    """send_photo raises TelegramError → falls back to send_document; returns normally."""
    from unittest.mock import patch, AsyncMock
    from tgclaude.media import send_screenshot_bytes

    bot = _make_bot()
    bot.send_photo.side_effect = telegram.error.TelegramError("too large")
    bot.send_document = AsyncMock()

    with patch(
        "tgclaude.image_processor.normalize_image",
        new=AsyncMock(return_value=(b"jpeg_data", "image/jpeg")),
    ):
        await send_screenshot_bytes(b"raw", bot, chat_id=100)

    bot.send_document.assert_called_once()
    call_kwargs = bot.send_document.call_args
    assert call_kwargs.kwargs.get("filename") == "screenshot.jpg"
    assert call_kwargs.kwargs.get("caption") == "📸 Screenshot"


async def test_send_screenshot_bytes_raises_when_both_fail() -> None:
    """Both send_photo and send_document fail → re-raises TelegramError."""
    from unittest.mock import patch, AsyncMock
    from tgclaude.media import send_screenshot_bytes

    bot = _make_bot()
    bot.send_photo.side_effect = telegram.error.TelegramError("photo fail")
    bot.send_document = AsyncMock(side_effect=Exception("doc fail"))

    with patch(
        "tgclaude.image_processor.normalize_image",
        new=AsyncMock(return_value=(b"jpeg_data", "image/jpeg")),
    ):
        with pytest.raises(telegram.error.TelegramError):
            await send_screenshot_bytes(b"raw", bot, chat_id=100)


async def test_send_screenshot_bytes_uses_raw_on_normalize_failure() -> None:
    """normalize_image raises ValueError → raw bytes used as fallback; send_photo called."""
    from unittest.mock import patch, AsyncMock
    from tgclaude.media import send_screenshot_bytes

    bot = _make_bot()

    with patch(
        "tgclaude.image_processor.normalize_image",
        new=AsyncMock(side_effect=ValueError("unsupported")),
    ):
        await send_screenshot_bytes(b"raw", bot, chat_id=100)

    bot.send_photo.assert_called_once()
