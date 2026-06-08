"""Tests for tgclaude.telegram_utils.send_typing_action."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
import telegram.error

from tgclaude import telegram_utils
from tgclaude.telegram_utils import send_typing_action


@pytest.fixture(autouse=True)
def reset_throttle():
    telegram_utils._typing_last_sent.clear()
    yield
    telegram_utils._typing_last_sent.clear()


@pytest.mark.asyncio
async def test_within_throttle_window_sends_once():
    """Two calls within 4.5 s for the same chat_id result in one send_chat_action."""
    bot = AsyncMock()
    # Start at 100.0 so 100.0 - 0.0 (default last) > 4.5 → first call sends.
    # Second call at 102.0: 102.0 - 100.0 = 2.0 < 4.5 → throttled.
    with patch("tgclaude.telegram_utils.time") as mock_time:
        mock_time.monotonic.side_effect = [100.0, 102.0]
        await send_typing_action(bot, chat_id=1)
        await send_typing_action(bot, chat_id=1)
    bot.send_chat_action.assert_awaited_once_with(chat_id=1, action="typing")


@pytest.mark.asyncio
async def test_outside_throttle_window_sends_twice():
    """Two calls 5 s apart (mocked) for the same chat_id both call send_chat_action."""
    bot = AsyncMock()
    # First call at 100.0 passes gate (100.0 - 0.0 > 4.5).
    # Second call at 105.0: 105.0 - 100.0 = 5.0 > 4.5 → also sends.
    with patch("tgclaude.telegram_utils.time") as mock_time:
        mock_time.monotonic.side_effect = [100.0, 105.0]
        await send_typing_action(bot, chat_id=2)
        await send_typing_action(bot, chat_id=2)
    assert bot.send_chat_action.await_count == 2


@pytest.mark.asyncio
async def test_different_chat_ids_have_independent_buckets():
    """Two calls within 1 s but to different chat_ids both send."""
    bot = AsyncMock()
    # Both calls at times > 4.5 from the default last (0.0) → both pass gate.
    with patch("tgclaude.telegram_utils.time") as mock_time:
        mock_time.monotonic.side_effect = [100.0, 100.5]
        await send_typing_action(bot, chat_id=10)
        await send_typing_action(bot, chat_id=20)
    assert bot.send_chat_action.await_count == 2
    calls = bot.send_chat_action.call_args_list
    assert calls[0].kwargs["chat_id"] == 10
    assert calls[1].kwargs["chat_id"] == 20


@pytest.mark.asyncio
async def test_telegram_error_is_swallowed():
    """TelegramError from send_chat_action must not propagate."""
    bot = AsyncMock()
    bot.send_chat_action.side_effect = telegram.error.NetworkError("connection lost")
    with patch("tgclaude.telegram_utils.time") as mock_time:
        # Use a value well above 0.0 so the gate is open (100.0 - 0.0 > 4.5).
        mock_time.monotonic.return_value = 100.0
        # Must not raise
        await send_typing_action(bot, chat_id=3)


@pytest.mark.asyncio
async def test_timestamp_stamped_before_await():
    """_typing_last_sent[chat_id] is set to the monotonic value from the START of the call."""
    bot = AsyncMock()
    # Use a time > 4.5 above the default last (0.0) so the gate is open.
    fixed_time = 100.0
    with patch("tgclaude.telegram_utils.time") as mock_time:
        mock_time.monotonic.return_value = fixed_time
        await send_typing_action(bot, chat_id=4)
    assert telegram_utils._typing_last_sent[4] == fixed_time
