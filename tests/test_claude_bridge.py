"""Tests for tgclaude.claude_bridge — _build_turn_content and IMG-07 bypass-mode fix.

Strategy (Khorikov):
- _build_turn_content is a pure function → output-based tests only.
- run_turn IMG-07 test: mock claude_agent_sdk.query (unmanaged dependency) and
  verify the prompt passed to it is an AsyncIterable when images are present.
  This directly guards BUG-1 regression (the isinstance(content, list) fix).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from tgclaude.handlers.messages import PendingTurn


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_IMAGE_BLOCK = {
    "type": "image",
    "source": {"type": "base64", "media_type": "image/jpeg", "data": "AAAA"},
}

_IMAGE_BLOCK_2 = {
    "type": "image",
    "source": {"type": "base64", "media_type": "image/jpeg", "data": "BBBB"},
}


# ---------------------------------------------------------------------------
# _build_turn_content — pure function, output-based tests
# ---------------------------------------------------------------------------


def test_build_turn_content_text_only_returns_string() -> None:
    """A PendingTurn with only text must produce a plain str."""
    from tgclaude.claude_bridge import _build_turn_content

    turn = PendingTurn(text="hello world")
    result = _build_turn_content(turn)

    assert isinstance(result, str)
    assert result == "hello world"


def test_build_turn_content_empty_turn_returns_empty_string() -> None:
    """A PendingTurn with no text and no images must return an empty string."""
    from tgclaude.claude_bridge import _build_turn_content

    turn = PendingTurn()
    result = _build_turn_content(turn)

    assert result == ""


def test_build_turn_content_images_only_returns_list() -> None:
    """A PendingTurn with images and no caption must return a list of image dicts."""
    from tgclaude.claude_bridge import _build_turn_content

    turn = PendingTurn(images=[_IMAGE_BLOCK])
    result = _build_turn_content(turn)

    assert isinstance(result, list)
    assert result == [_IMAGE_BLOCK]


def test_build_turn_content_images_only_is_not_a_string() -> None:
    """The result for an image-only turn must not be a string."""
    from tgclaude.claude_bridge import _build_turn_content

    turn = PendingTurn(images=[_IMAGE_BLOCK, _IMAGE_BLOCK_2])
    result = _build_turn_content(turn)

    assert not isinstance(result, str)


def test_build_turn_content_images_with_caption_starts_with_text_block() -> None:
    """When images and a caption are present, the list must start with a text
    block followed by the image blocks."""
    from tgclaude.claude_bridge import _build_turn_content

    turn = PendingTurn(images=[_IMAGE_BLOCK], caption="describe this")
    result = _build_turn_content(turn)

    assert isinstance(result, list)
    assert len(result) == 2
    assert result[0] == {"type": "text", "text": "describe this"}
    assert result[1] == _IMAGE_BLOCK


def test_build_turn_content_multiple_images_with_caption_preserves_order() -> None:
    """With two images and a caption, the list must be [text_block, img1, img2]."""
    from tgclaude.claude_bridge import _build_turn_content

    turn = PendingTurn(images=[_IMAGE_BLOCK, _IMAGE_BLOCK_2], caption="my caption")
    result = _build_turn_content(turn)

    assert isinstance(result, list)
    assert len(result) == 3
    assert result[0]["type"] == "text"
    assert result[0]["text"] == "my caption"
    assert result[1] == _IMAGE_BLOCK
    assert result[2] == _IMAGE_BLOCK_2


def test_build_turn_content_images_without_caption_contains_only_image_blocks() -> None:
    """Images with no caption must NOT prepend a text block."""
    from tgclaude.claude_bridge import _build_turn_content

    turn = PendingTurn(images=[_IMAGE_BLOCK, _IMAGE_BLOCK_2])
    result = _build_turn_content(turn)

    assert isinstance(result, list)
    assert all(item["type"] == "image" for item in result)


# ---------------------------------------------------------------------------
# IMG-07 / BUG-1 regression guard — run_turn in bypass mode with images
# ---------------------------------------------------------------------------


def _make_minimal_config(permission_mode: str = "bypass") -> MagicMock:
    """Build a minimal Config mock with the given permission_mode."""
    config = MagicMock()
    config.permission_mode = permission_mode
    config.turn_timeout_s = 30
    config.claude_project_cwd = "/tmp"
    config.claude_binary = None
    return config


def _make_minimal_db() -> MagicMock:
    """Build a Database mock whose get_active_session returns None."""
    db = MagicMock()
    db.get_active_session = AsyncMock(return_value=None)
    db.set_active_session = AsyncMock()
    db.clear_active_session = AsyncMock()
    return db


def _make_minimal_permission_manager() -> MagicMock:
    perm = MagicMock()
    perm.has_grant = AsyncMock(return_value=False)
    perm.add_grant = AsyncMock()
    return perm


async def test_run_turn_with_images_in_bypass_mode_passes_async_iterable_to_query() -> None:
    """BUG-1 regression guard (IMG-07): run_turn must wrap a list[dict] content
    (produced by an image turn) in _as_user_stream before passing it to query(),
    even in bypass mode. The prompt must be an AsyncIterable, never a bare list."""
    from tgclaude.claude_bridge import ClaudeBridge

    config = _make_minimal_config(permission_mode="bypass")
    db = _make_minimal_db()
    perm = _make_minimal_permission_manager()
    bridge = ClaudeBridge(config=config, db=db, permission_manager=perm)

    bot = MagicMock()
    bot.send_chat_action = AsyncMock()
    bot.send_message = AsyncMock()

    turn = PendingTurn(images=[_IMAGE_BLOCK], caption="what is this?")

    captured_prompts: list[Any] = []

    # _as_user_stream is an async generator that yields one dict then waits on
    # an Event. We need query() to consume it and then let run_turn finish.
    # We mock query to capture the prompt, yield a fake ResultMessage, and return.
    async def fake_query(prompt, options):  # type: ignore[override]
        captured_prompts.append(prompt)
        # Consume the first item from the stream so the generator advances.
        if hasattr(prompt, "__aiter__"):
            async for _ in prompt:
                break  # one item consumed — don't wait for the done event
        # Yield nothing — no blocks to dispatch.
        return
        yield  # make this a generator

    with patch("tgclaude.claude_bridge.query", new=fake_query):
        await bridge.run_turn(
            user_id=1,
            turn=turn,
            bot=bot,
            chat_id=100,
        )

    assert len(captured_prompts) == 1
    prompt_arg = captured_prompts[0]
    # The prompt must be an AsyncIterable, not a plain list or str.
    assert hasattr(prompt_arg, "__aiter__"), (
        f"Expected AsyncIterable prompt but got {type(prompt_arg).__name__!r}. "
        "BUG-1 may have regressed: list[dict] content was passed bare to query()."
    )
    assert not isinstance(prompt_arg, (list, str))


async def test_run_turn_with_text_in_bypass_mode_passes_string_to_query() -> None:
    """In bypass mode with a plain text turn, the prompt passed to query must be
    a str (not wrapped in AsyncIterable) — bypass mode skips the wrapper for str."""
    from tgclaude.claude_bridge import ClaudeBridge

    config = _make_minimal_config(permission_mode="bypass")
    db = _make_minimal_db()
    perm = _make_minimal_permission_manager()
    bridge = ClaudeBridge(config=config, db=db, permission_manager=perm)

    bot = MagicMock()
    bot.send_chat_action = AsyncMock()
    bot.send_message = AsyncMock()

    turn = PendingTurn(text="hello")

    captured_prompts: list[Any] = []

    async def fake_query(prompt, options):  # type: ignore[override]
        captured_prompts.append(prompt)
        return
        yield

    with patch("tgclaude.claude_bridge.query", new=fake_query):
        await bridge.run_turn(
            user_id=1,
            turn=turn,
            bot=bot,
            chat_id=100,
        )

    assert len(captured_prompts) == 1
    assert isinstance(captured_prompts[0], str)
