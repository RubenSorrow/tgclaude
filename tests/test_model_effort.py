"""Tests for Epic B — model/effort selection.

Coverage:
  Group 1 — _build_options model/effort kwargs (output-based)
  Group 2 — _is_effort_unsupported_error (output-based, pure function)
  Group 3 — model/effort persistence via real in-memory SQLite (state-based)
  Group 4 — model_callback / effort_callback handler behaviour (state-based)

Strategy (Khorikov):
- Pure functions → output-based assertions; never inspect internal state.
- Side-effecting handlers → state-based: verify DB mutations after the call.
- Only unmanaged dependencies (Telegram bot API) are mocked.
- Real Database + aiosqlite used for persistence group.
"""

from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Shared helpers (mirror the pattern in test_delete.py / test_claude_bridge.py)
# ---------------------------------------------------------------------------


def _make_minimal_config(permission_mode: str = "bypass") -> MagicMock:
    config = MagicMock()
    config.permission_mode = permission_mode
    config.turn_timeout_s = 30
    config.claude_project_cwd = "/tmp"
    config.claude_binary = None
    return config


def _make_minimal_db_mock() -> MagicMock:
    """Async-capable Database mock; get_setting returns None by default."""
    db = MagicMock()
    db.get_active_session = AsyncMock(return_value=None)
    db.get_setting = AsyncMock(return_value=None)
    db.set_setting = AsyncMock()
    db.delete_setting = AsyncMock()
    db.set_active_session = AsyncMock()
    db.clear_active_session = AsyncMock()
    return db


def _make_minimal_permission_manager() -> MagicMock:
    perm = MagicMock()
    perm.has_grant = AsyncMock(return_value=False)
    perm.add_grant = AsyncMock()
    return perm


def _make_callback_query(data: str, user_id: int = 1) -> MagicMock:
    """Build a minimal mock CallbackQuery (mirrors test_delete.py pattern)."""
    query = MagicMock()
    query.data = data
    query.from_user = MagicMock()
    query.from_user.id = user_id
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()
    return query


def _make_update_from_callback(query: MagicMock) -> MagicMock:
    update = MagicMock()
    update.callback_query = query
    update.effective_user = query.from_user
    return update


def _make_context_with_mock_db(
    user_id: int,
    db: MagicMock | None = None,
) -> MagicMock:
    """Build a minimal mock PTB context with allowed user and mock DB."""
    config = MagicMock()
    config.allowed_user_ids = [user_id]

    ctx = MagicMock()
    ctx.bot_data = {"config": config, "db": db or _make_minimal_db_mock()}
    return ctx


# ---------------------------------------------------------------------------
# Group 1 — _build_options model/effort application (output-based)
# ---------------------------------------------------------------------------


class TestBuildOptionsModelEffort:
    """_build_options is a method on ClaudeBridge; test by patching
    ClaudeAgentOptions and inspecting the kwargs it receives."""

    def _build_bridge(self, permission_mode: str = "bypass") -> "ClaudeBridge":  # type: ignore[name-defined]  # noqa: F821
        from tgclaude.claude_bridge import ClaudeBridge

        config = _make_minimal_config(permission_mode)
        db = _make_minimal_db_mock()
        perm = _make_minimal_permission_manager()
        return ClaudeBridge(config=config, db=db, permission_manager=perm)

    def _call_build_options(
        self,
        bridge: "ClaudeBridge",  # noqa: F821
        *,
        model: str | None = None,
        effort: str | None = None,
    ) -> dict:
        """Call _build_options with dummy values and return captured kwargs."""
        captured: list[dict] = []

        class _CapturingOptions:
            def __init__(self, **kwargs: object) -> None:
                captured.append(kwargs)

        bot = MagicMock()
        with patch("tgclaude.claude_bridge.ClaudeAgentOptions", _CapturingOptions):
            bridge._build_options(
                user_id=1,
                session_uuid=None,
                bot=bot,
                chat_id=100,
                model=model,
                effort=effort,
            )

        assert len(captured) == 1, "ClaudeAgentOptions must be constructed exactly once"
        return captured[0]

    def test_model_opus_present_in_kwargs(self) -> None:
        """Passing model='opus' must result in model='opus' in ClaudeAgentOptions kwargs."""
        bridge = self._build_bridge()
        kwargs = self._call_build_options(bridge, model="opus")
        assert kwargs.get("model") == "opus"

    def test_model_none_absent_from_kwargs(self) -> None:
        """Passing model=None must not add a 'model' key to ClaudeAgentOptions kwargs."""
        bridge = self._build_bridge()
        kwargs = self._call_build_options(bridge, model=None)
        assert "model" not in kwargs

    def test_effort_high_present_in_kwargs(self) -> None:
        """Passing effort='high' must result in effort='high' in ClaudeAgentOptions kwargs."""
        bridge = self._build_bridge()
        kwargs = self._call_build_options(bridge, effort="high")
        assert kwargs.get("effort") == "high"

    def test_effort_none_absent_from_kwargs(self) -> None:
        """Passing effort=None must not add an 'effort' key to ClaudeAgentOptions kwargs."""
        bridge = self._build_bridge()
        kwargs = self._call_build_options(bridge, effort=None)
        assert "effort" not in kwargs

    def test_model_and_effort_set_simultaneously(self) -> None:
        """Both model and effort can be present in the same kwargs."""
        bridge = self._build_bridge()
        kwargs = self._call_build_options(bridge, model="sonnet", effort="max")
        assert kwargs.get("model") == "sonnet"
        assert kwargs.get("effort") == "max"

    def test_effort_xhigh_present_in_kwargs(self) -> None:
        """The xhigh effort value is passed through unchanged."""
        bridge = self._build_bridge()
        kwargs = self._call_build_options(bridge, effort="xhigh")
        assert kwargs.get("effort") == "xhigh"

    def test_model_haiku_present_in_kwargs(self) -> None:
        """Model='haiku' is passed through unchanged."""
        bridge = self._build_bridge()
        kwargs = self._call_build_options(bridge, model="haiku")
        assert kwargs.get("model") == "haiku"


# ---------------------------------------------------------------------------
# Group 2 — _is_effort_unsupported_error (output-based, pure function)
# ---------------------------------------------------------------------------


class TestIsEffortUnsupportedError:
    """Pure function: verify classification of exception messages."""

    def _fn(self, msg: str) -> bool:
        from tgclaude.claude_bridge import _is_effort_unsupported_error

        return _is_effort_unsupported_error(Exception(msg))

    def test_unrecognized_option_effort_returns_true(self) -> None:
        assert self._fn("unrecognized option: --effort") is True

    def test_unknown_option_effort_returns_true(self) -> None:
        assert self._fn("Unknown option: --effort") is True

    def test_unexpected_argument_effort_returns_true(self) -> None:
        assert self._fn("unexpected argument --effort") is True

    def test_bare_effort_flag_returns_true(self) -> None:
        """A message containing only '--effort' still triggers the pattern."""
        assert self._fn("--effort") is True

    def test_invalid_option_effort_returns_true(self) -> None:
        assert self._fn("invalid option: --effort") is True

    def test_generic_failure_returns_false(self) -> None:
        assert self._fn("Something failed") is False

    def test_auth_error_returns_false(self) -> None:
        """Auth errors must be excluded even when '--effort' text is absent."""
        assert self._fn("401 unauthorized") is False

    def test_auth_credential_error_returns_false(self) -> None:
        """Exceptions whose text contains 'credential' are treated as auth errors."""
        assert self._fn("credential expired, please re-login") is False

    def test_unrelated_option_error_returns_false(self) -> None:
        """A message about an unrelated bad option must not match."""
        assert self._fn("unrecognized option: --foo") is False

    def test_option_error_not_mentioning_effort_returns_false(self) -> None:
        """'unrecognized option' without 'effort' must not trigger."""
        assert self._fn("unrecognized option: --model") is False


# ---------------------------------------------------------------------------
# Group 3 — model/effort persistence via real DB (state-based)
# ---------------------------------------------------------------------------


@pytest.fixture
async def real_db(tmp_path: Path):
    """Provide an in-memory-ish (temp file) Database and close it after the test."""
    from tgclaude.db import init_db

    db_path = tmp_path / "test.db"
    db = await init_db(db_path)
    yield db
    await db.close()


class TestSettingsPersistence:
    """Use the real Database implementation — no mocks for managed deps."""

    async def test_set_and_get_model_setting(self, real_db) -> None:
        """set_setting then get_setting returns the stored value."""
        await real_db.set_setting("model_1", "sonnet")
        result = await real_db.get_setting("model_1")
        assert result == "sonnet"

    async def test_delete_model_setting_returns_none(self, real_db) -> None:
        """After delete_setting, get_setting returns None."""
        await real_db.set_setting("model_1", "sonnet")
        await real_db.delete_setting("model_1")
        result = await real_db.get_setting("model_1")
        assert result is None

    async def test_effort_and_model_for_different_users_do_not_interfere(
        self, real_db
    ) -> None:
        """Settings for user 1 and user 2 are stored independently."""
        await real_db.set_setting("model_1", "opus")
        await real_db.set_setting("model_2", "haiku")
        await real_db.set_setting("effort_1", "high")
        await real_db.set_setting("effort_2", "low")

        assert await real_db.get_setting("model_1") == "opus"
        assert await real_db.get_setting("model_2") == "haiku"
        assert await real_db.get_setting("effort_1") == "high"
        assert await real_db.get_setting("effort_2") == "low"

    async def test_overwrite_model_setting(self, real_db) -> None:
        """Writing a model setting twice keeps the latest value."""
        await real_db.set_setting("model_1", "sonnet")
        await real_db.set_setting("model_1", "opus")
        result = await real_db.get_setting("model_1")
        assert result == "opus"

    async def test_delete_nonexistent_setting_is_noop(self, real_db) -> None:
        """Deleting a key that was never set must not raise and must return None."""
        await real_db.delete_setting("model_99")  # should not raise
        result = await real_db.get_setting("model_99")
        assert result is None

    async def test_effort_setting_persists_independently_of_model(
        self, real_db
    ) -> None:
        """Setting effort does not affect model and vice-versa for same user."""
        await real_db.set_setting("model_1", "sonnet")
        await real_db.set_setting("effort_1", "max")
        await real_db.delete_setting("effort_1")

        assert await real_db.get_setting("model_1") == "sonnet"
        assert await real_db.get_setting("effort_1") is None


# ---------------------------------------------------------------------------
# Group 4 — model_callback / effort_callback handler behaviour (state-based)
# ---------------------------------------------------------------------------


class TestModelCallback:
    """Handler side-effects on DB — Telegram is mocked (unmanaged dep)."""

    async def test_cfg_model_haiku_calls_set_setting(self) -> None:
        from tgclaude.handlers.commands import model_callback

        db = _make_minimal_db_mock()
        query = _make_callback_query("cfg:model:haiku", user_id=1)
        update = _make_update_from_callback(query)
        ctx = _make_context_with_mock_db(1, db)

        await model_callback(update, ctx)

        db.set_setting.assert_awaited_once_with("model_1", "haiku")
        db.delete_setting.assert_not_awaited()

    async def test_cfg_model_opus_calls_set_setting(self) -> None:
        from tgclaude.handlers.commands import model_callback

        db = _make_minimal_db_mock()
        query = _make_callback_query("cfg:model:opus", user_id=1)
        update = _make_update_from_callback(query)
        ctx = _make_context_with_mock_db(1, db)

        await model_callback(update, ctx)

        db.set_setting.assert_awaited_once_with("model_1", "opus")

    async def test_cfg_model_sonnet_calls_set_setting(self) -> None:
        from tgclaude.handlers.commands import model_callback

        db = _make_minimal_db_mock()
        query = _make_callback_query("cfg:model:sonnet", user_id=42)
        update = _make_update_from_callback(query)
        ctx = _make_context_with_mock_db(42, db)

        await model_callback(update, ctx)

        db.set_setting.assert_awaited_once_with("model_42", "sonnet")

    async def test_cfg_model_default_calls_delete_setting(self) -> None:
        from tgclaude.handlers.commands import model_callback

        db = _make_minimal_db_mock()
        query = _make_callback_query("cfg:model:default", user_id=1)
        update = _make_update_from_callback(query)
        ctx = _make_context_with_mock_db(1, db)

        await model_callback(update, ctx)

        db.delete_setting.assert_awaited_once_with("model_1")
        db.set_setting.assert_not_awaited()

    async def test_unlisted_user_model_callback_does_not_touch_db(self) -> None:
        """An unlisted user must not trigger any DB mutation."""
        from tgclaude.handlers.commands import model_callback

        db = _make_minimal_db_mock()
        query = _make_callback_query("cfg:model:opus", user_id=999)
        update = _make_update_from_callback(query)

        # Only user 1 is in the allow-list; 999 is not.
        config = MagicMock()
        config.allowed_user_ids = [1]
        ctx = MagicMock()
        ctx.bot_data = {"config": config, "db": db}

        await model_callback(update, ctx)

        db.set_setting.assert_not_awaited()
        db.delete_setting.assert_not_awaited()

    async def test_unknown_model_value_does_not_touch_db(self) -> None:
        """An unrecognised model value must not call set_setting or delete_setting."""
        from tgclaude.handlers.commands import model_callback

        db = _make_minimal_db_mock()
        query = _make_callback_query("cfg:model:gpt4", user_id=1)
        update = _make_update_from_callback(query)
        ctx = _make_context_with_mock_db(1, db)

        await model_callback(update, ctx)

        db.set_setting.assert_not_awaited()
        db.delete_setting.assert_not_awaited()


class TestEffortCallback:
    """Handler side-effects on DB for /effort — Telegram mocked."""

    async def test_cfg_effort_max_calls_set_setting(self) -> None:
        from tgclaude.handlers.commands import effort_callback

        db = _make_minimal_db_mock()
        query = _make_callback_query("cfg:effort:max", user_id=1)
        update = _make_update_from_callback(query)
        ctx = _make_context_with_mock_db(1, db)

        await effort_callback(update, ctx)

        db.set_setting.assert_awaited_once_with("effort_1", "max")
        db.delete_setting.assert_not_awaited()

    async def test_cfg_effort_high_calls_set_setting(self) -> None:
        from tgclaude.handlers.commands import effort_callback

        db = _make_minimal_db_mock()
        query = _make_callback_query("cfg:effort:high", user_id=7)
        update = _make_update_from_callback(query)
        ctx = _make_context_with_mock_db(7, db)

        await effort_callback(update, ctx)

        db.set_setting.assert_awaited_once_with("effort_7", "high")

    async def test_cfg_effort_xhigh_calls_set_setting(self) -> None:
        from tgclaude.handlers.commands import effort_callback

        db = _make_minimal_db_mock()
        query = _make_callback_query("cfg:effort:xhigh", user_id=1)
        update = _make_update_from_callback(query)
        ctx = _make_context_with_mock_db(1, db)

        await effort_callback(update, ctx)

        db.set_setting.assert_awaited_once_with("effort_1", "xhigh")

    async def test_cfg_effort_low_calls_set_setting(self) -> None:
        from tgclaude.handlers.commands import effort_callback

        db = _make_minimal_db_mock()
        query = _make_callback_query("cfg:effort:low", user_id=1)
        update = _make_update_from_callback(query)
        ctx = _make_context_with_mock_db(1, db)

        await effort_callback(update, ctx)

        db.set_setting.assert_awaited_once_with("effort_1", "low")

    async def test_cfg_effort_default_calls_delete_setting(self) -> None:
        from tgclaude.handlers.commands import effort_callback

        db = _make_minimal_db_mock()
        query = _make_callback_query("cfg:effort:default", user_id=1)
        update = _make_update_from_callback(query)
        ctx = _make_context_with_mock_db(1, db)

        await effort_callback(update, ctx)

        db.delete_setting.assert_awaited_once_with("effort_1")
        db.set_setting.assert_not_awaited()

    async def test_unlisted_user_effort_callback_does_not_touch_db(self) -> None:
        """An unlisted user must not trigger any DB mutation."""
        from tgclaude.handlers.commands import effort_callback

        db = _make_minimal_db_mock()
        query = _make_callback_query("cfg:effort:high", user_id=999)
        update = _make_update_from_callback(query)

        config = MagicMock()
        config.allowed_user_ids = [1]
        ctx = MagicMock()
        ctx.bot_data = {"config": config, "db": db}

        await effort_callback(update, ctx)

        db.set_setting.assert_not_awaited()
        db.delete_setting.assert_not_awaited()

    async def test_unknown_effort_value_does_not_touch_db(self) -> None:
        """An unrecognised effort value must not call set_setting or delete_setting."""
        from tgclaude.handlers.commands import effort_callback

        db = _make_minimal_db_mock()
        query = _make_callback_query("cfg:effort:turbo", user_id=1)
        update = _make_update_from_callback(query)
        ctx = _make_context_with_mock_db(1, db)

        await effort_callback(update, ctx)

        db.set_setting.assert_not_awaited()
        db.delete_setting.assert_not_awaited()


# ---------------------------------------------------------------------------
# Group 5 — run_turn CFG-06 effort-unsupported integration (state-based)
# ---------------------------------------------------------------------------


class TestRunTurnEffortFallback:
    """Verify that run_turn sends the CFG-06 user-facing message when the CLI
    raises an effort-related error, and that the bot does not crash."""

    async def test_effort_unsupported_error_sends_user_message_and_does_not_crash(
        self,
    ) -> None:
        """When query raises 'unknown option: --effort', run_turn must send the
        CFG-06 warning message and return normally (no exception propagated)."""
        from tgclaude.claude_bridge import ClaudeBridge
        from tgclaude.handlers.messages import PendingTurn

        # Build minimal collaborators.
        config = _make_minimal_config(permission_mode="bypass")
        db = _make_minimal_db_mock()
        # effort setting returns "high"; model returns None; no active session.
        db.get_setting = AsyncMock(
            side_effect=lambda key: "high" if key.startswith("effort_") else None
        )
        db.get_active_session = AsyncMock(return_value=None)
        perm = _make_minimal_permission_manager()

        bridge = ClaudeBridge(config=config, db=db, permission_manager=perm)

        bot = MagicMock()
        bot.send_chat_action = AsyncMock()
        bot.send_message = AsyncMock()

        turn = PendingTurn(text="hello")

        async def _raise_effort_error(**kwargs):
            raise Exception("unknown option: --effort")
            # Make this an async generator as query() is used with `async for`.
            return
            yield  # pragma: no cover

        with patch("tgclaude.claude_bridge.query", side_effect=_raise_effort_error):
            # Must not raise — bot stays alive.
            await bridge.run_turn(
                user_id=1,
                turn=turn,
                bot=bot,
                chat_id=100,
            )

        # Verify the CFG-06 user-facing message was sent.
        assert bot.send_message.called, "send_message must be called after effort error"
        calls_text = [str(c.kwargs.get("text", "")) for c in bot.send_message.call_args_list]
        assert any("effort" in t.lower() for t in calls_text), (
            f"Expected a message mentioning 'effort'; got: {calls_text}"
        )
