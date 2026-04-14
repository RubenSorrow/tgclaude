"""Tests for /delete command flow."""
from __future__ import annotations

import pytest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_callback_query(data: str, user_id: int = 1, message_text: str = "Delete?"):
    """Build a minimal mock CallbackQuery."""
    msg = MagicMock()
    msg.text = message_text
    query = MagicMock()
    query.data = data
    query.from_user = MagicMock()
    query.from_user.id = user_id
    query.message = msg
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()
    return query


def _make_context(user_id: int, tmp_path: Path):
    """Build a minimal mock context with bot_data."""
    config = MagicMock()
    config.allowed_user_ids = [user_id]
    config.claude_home = tmp_path / ".claude"
    config.claude_project_cwd = tmp_path

    db = MagicMock()
    db.delete_permission_grants_for_session = AsyncMock()
    db.clear_active_session_by_uuid = AsyncMock(return_value=None)

    ctx = MagicMock()
    ctx.bot_data = {"config": config, "db": db}
    return ctx


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_delete_missing_file_treated_as_success(tmp_path):
    """Deleting a session whose JSONL is already gone must succeed (idempotent)."""
    from tgclaude.handlers.commands import delete_confirm_callback

    uuid = "a1b2c3d4-e5f6-7890-abcd-ef1234567890"
    query = _make_callback_query(f"delconfirm:yes:{uuid}")
    update = MagicMock()
    update.callback_query = query
    update.effective_user = query.from_user
    ctx = _make_context(1, tmp_path)

    with patch("tgclaude.claude_bridge._active_sessions", {}):
        await delete_confirm_callback(update, ctx)

    query.edit_message_text.assert_called_once()
    call_kwargs = query.edit_message_text.call_args
    text = call_kwargs[0][0] if call_kwargs[0] else call_kwargs[1].get("text", "")
    assert "Session deleted." in text


@pytest.mark.asyncio
async def test_delete_blocked_when_session_in_use(tmp_path):
    """Delete must be rejected when the session is currently active in _active_sessions."""
    from tgclaude.handlers.commands import delete_confirm_callback

    uuid = "a1b2c3d4-e5f6-7890-abcd-ef1234567890"
    query = _make_callback_query(f"delconfirm:yes:{uuid}")
    update = MagicMock()
    update.callback_query = query
    update.effective_user = query.from_user
    ctx = _make_context(1, tmp_path)

    with patch("tgclaude.claude_bridge._active_sessions", {uuid: 1}):
        await delete_confirm_callback(update, ctx)

    query.edit_message_text.assert_called_once()
    call_kwargs = query.edit_message_text.call_args
    text = call_kwargs[0][0] if call_kwargs[0] else call_kwargs[1].get("text", "")
    assert "currently in use" in text
    ctx.bot_data["db"].delete_permission_grants_for_session.assert_not_called()


@pytest.mark.asyncio
async def test_delete_nulls_other_users_active_session(tmp_path):
    """Deleting a session owned by a different user nulls their row without
    cancelling the caller's permissions."""
    from tgclaude.handlers.commands import delete_confirm_callback

    uuid = "a1b2c3d4-e5f6-7890-abcd-ef1234567890"
    query = _make_callback_query(f"delconfirm:yes:{uuid}", user_id=1)
    update = MagicMock()
    update.callback_query = query
    update.effective_user = query.from_user
    ctx = _make_context(1, tmp_path)
    ctx.bot_data["db"].clear_active_session_by_uuid = AsyncMock(return_value=2)

    with patch("tgclaude.claude_bridge._active_sessions", {}), \
         patch("tgclaude.handlers.commands._cancel_all_pending_permissions") as mock_cancel:
        await delete_confirm_callback(update, ctx)

    mock_cancel.assert_not_called()
    call_kwargs = query.edit_message_text.call_args
    text = call_kwargs[0][0] if call_kwargs[0] else call_kwargs[1].get("text", "")
    assert "Session deleted." in text


@pytest.mark.asyncio
async def test_cancel_at_picker_step_makes_no_db_changes(tmp_path):
    """Tapping Cancel at the picker step must not touch the DB or filesystem."""
    from tgclaude.handlers.commands import delete_picker_callback

    query = _make_callback_query("del:cancel")
    update = MagicMock()
    update.callback_query = query
    update.effective_user = query.from_user
    ctx = _make_context(1, tmp_path)

    await delete_picker_callback(update, ctx)

    query.edit_message_text.assert_called_once()
    call_kwargs = query.edit_message_text.call_args
    text = call_kwargs[0][0] if call_kwargs[0] else call_kwargs[1].get("text", "")
    assert "Cancelled." in text
    ctx.bot_data["db"].delete_permission_grants_for_session.assert_not_called()


@pytest.mark.asyncio
async def test_malformed_uuid_silently_dropped(tmp_path):
    """A callback with a malformed UUID must be silently dropped (no edit, no DB)."""
    from tgclaude.handlers.commands import delete_confirm_callback

    query = _make_callback_query("delconfirm:yes:not-a-valid-uuid!!")
    update = MagicMock()
    update.callback_query = query
    update.effective_user = query.from_user
    ctx = _make_context(1, tmp_path)

    with patch("tgclaude.claude_bridge._active_sessions", {}):
        await delete_confirm_callback(update, ctx)

    query.edit_message_text.assert_not_called()
    ctx.bot_data["db"].delete_permission_grants_for_session.assert_not_called()
