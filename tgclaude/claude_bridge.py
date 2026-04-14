"""Claude SDK bridge.

Wraps the claude-agent-sdk in one cohesive async class that:
- Maps a Telegram user + text message to a single SDK turn.
- Streams each SDK content block to Telegram as it arrives.
- Implements the interactive tool-permission loop (§6).
- Runs the session-persist step after every completed turn (§4).
"""

from __future__ import annotations

import asyncio
import collections
import html
import json
import logging
from pathlib import Path
from typing import Any, AsyncIterable

import aiosqlite

from claude_agent_sdk import query, ClaudeAgentOptions
from claude_agent_sdk.types import (
    TextBlock,
    ThinkingBlock,
    ToolUseBlock,
    ToolResultBlock,
    ResultMessage,
    AssistantMessage,
    UserMessage,
    SystemMessage,
    ToolPermissionContext,
    PermissionResultAllow,
    PermissionResultDeny,
)
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, Message

from tgclaude.config import Config, READONLY_TOOLS
from tgclaude.db import Database
from tgclaude.formatter import format_text, chunk_message
from tgclaude.media import dispatch_file
from tgclaude.permissions import PermissionManager

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Process-global coordination state
# ---------------------------------------------------------------------------

# (telegram_user_id, tool_use_id) → asyncio.Future[dict]
# Future resolves to {"allow": bool, "message": str | None}
pending_permissions: dict[tuple[int, str], asyncio.Future] = {}

# user_id → tool_use_id they're composing a denial reason for
waiting_for_reason: dict[int, str] = {}

# session_uuid → user_id of the user currently running a turn against that session
_active_sessions: dict[str, int] = {}

# ---------------------------------------------------------------------------
# Truncation constants (§5 ToolUseBlock display)
# ---------------------------------------------------------------------------

_TOOL_INPUT_MAX_LINES = 20
_TOOL_INPUT_MAX_CHARS = 500


def _truncate_tool_input(raw: Any) -> tuple[str, bool, int]:
    """Serialise tool input to a short string for display.

    Returns (truncated_text, was_truncated, total_chars).
    """
    try:
        full = json.dumps(raw, indent=2, ensure_ascii=False)
    except (TypeError, ValueError):
        full = repr(raw)

    total = len(full)

    lines = full.splitlines()
    if len(lines) > _TOOL_INPUT_MAX_LINES:
        by_lines = "\n".join(lines[:_TOOL_INPUT_MAX_LINES])
        lines_truncated = True
    else:
        by_lines = full
        lines_truncated = False

    if len(full) > _TOOL_INPUT_MAX_CHARS:
        by_chars = full[:_TOOL_INPUT_MAX_CHARS]
        chars_truncated = True
    else:
        by_chars = full
        chars_truncated = False

    if not lines_truncated and not chars_truncated:
        return full, False, total

    truncated = by_lines if len(by_lines) <= len(by_chars) else by_chars
    return truncated, True, total


def _build_tool_announcement(
    tool_name: str,
    tool_input: Any,
    permission_keyboard: InlineKeyboardMarkup | None = None,
) -> tuple[str, InlineKeyboardMarkup | None]:
    """Build the Telegram message text and optional keyboard for a ToolUseBlock.

    Format per §5:
        🔧 <tool_name>
        <pre>truncated input JSON</pre>
        (truncated, N chars total)   ← only if truncated
    """
    snippet, was_truncated, total_chars = _truncate_tool_input(tool_input)
    escaped = html.escape(snippet)
    text = f"🔧 <b>{html.escape(tool_name)}</b>\n<pre>{escaped}</pre>"
    if was_truncated:
        text += f"\n<i>(truncated, {total_chars} chars total)</i>"
    return text, permission_keyboard


async def _as_user_stream(text: str, done: asyncio.Event):
    """Wrap a plain string as the AsyncIterable[dict] the SDK expects when can_use_tool is set."""
    yield {"type": "user", "message": {"role": "user", "content": text}}
    # Keep the stream open until the turn is complete.
    await done.wait()


def _build_permission_keyboard(tool_use_id: str) -> InlineKeyboardMarkup:
    """Return the 4-button inline keyboard for interactive permission prompts."""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("\u2705 Yes", callback_data=f"perm:yes:{tool_use_id}"),
            InlineKeyboardButton("\u2b50 Always allow", callback_data=f"perm:always:{tool_use_id}"),
        ],
        [
            InlineKeyboardButton("\u274c No", callback_data=f"perm:no:{tool_use_id}"),
            InlineKeyboardButton("\u270f\ufe0f No, and say why", callback_data=f"perm:why:{tool_use_id}"),
        ],
    ])


async def _edit_permission_message(
    msg: Message,
    original_html: str,
    resolution_note: str,
) -> None:
    """Remove the keyboard from a permission prompt and append a resolution note."""
    try:
        updated = f"{original_html}\n\n{resolution_note}"
        await msg.edit_text(updated, parse_mode="HTML", reply_markup=None)
    except Exception as exc:  # pragma: no cover
        logger.debug("Could not edit permission message: %s", exc)


class ClaudeBridge:
    """Executes a single Claude turn and streams the result to Telegram."""

    def __init__(
        self,
        config: Config,
        db: Database,
        permission_manager: PermissionManager,
    ) -> None:
        self._config = config
        self._db = db
        self._permission_manager = permission_manager
        # user_id → {tool_use_id → file_path_str}; populated in _send_tool_use_block,
        # consumed in _send_tool_result (Fix 2: dispatch after Write completes).
        self._pending_writes: dict[int, dict[str, str]] = {}
        # user_id → FIFO of block.id values for ToolUseBlocks seen during the current
        # turn.  Populated in _handle_content_item; consumed by the can_use_tool
        # closure in _build_options so each permission prompt uses the correct ID.
        self._tool_use_id_queues: dict[int, collections.deque[str]] = {}

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    async def run_turn(
        self,
        user_id: int,
        text: str,
        bot: Bot,
        chat_id: int,
    ) -> None:
        """Execute one Claude turn and stream results to Telegram.

        Per §4 and §5:
        1. Look up active session UUID from DB.
        2. Build ClaudeAgentOptions with cwd=config.claude_project_cwd.
        3. Send typing action before starting.
        4. Iterate SDK response blocks and dispatch each.
        5. Run session-persist step.
        6. On SDK auth error: post recovery message.
        """
        # Clear any stale state from a previous turn.
        self._pending_writes[user_id] = {}
        self._tool_use_id_queues[user_id] = collections.deque()

        session_uuid = await self._db.get_active_session(user_id)

        # Collision guard: reject if another user is already running a turn on this session.
        if session_uuid and session_uuid in _active_sessions and _active_sessions[session_uuid] != user_id:
            logger.warning(
                "Session %s already active for user %d; rejecting turn for user %d",
                session_uuid, _active_sessions[session_uuid], user_id,
            )
            await bot.send_message(
                chat_id=chat_id,
                text="This session is currently in use. Please wait a moment and retry.",
            )
            return

        # Register this session as in-use for the duration of this turn.
        if session_uuid:
            _active_sessions[session_uuid] = user_id

        options = self._build_options(user_id, session_uuid, bot, chat_id)

        await bot.send_chat_action(chat_id=chat_id, action="typing")

        new_session_uuid: str | None = session_uuid
        sdk_succeeded = False
        turn_done = asyncio.Event()
        try:
            prompt: str | AsyncIterable = text
            if self._config.permission_mode != "bypass":
                prompt = _as_user_stream(text, turn_done)
            async with asyncio.timeout(self._config.turn_timeout_s):
                async for block in query(
                    prompt=prompt,
                    options=options,
                ):
                    await bot.send_chat_action(chat_id=chat_id, action="typing")
                    new_session_uuid = await self._dispatch_block(
                        block=block,
                        user_id=user_id,
                        session_uuid=session_uuid,
                        bot=bot,
                        chat_id=chat_id,
                        current_new_uuid=new_session_uuid,
                        turn_done=turn_done,
                    )
            sdk_succeeded = True
        except TimeoutError:
            logger.warning(
                "Turn timed out for user %d after %ds",
                user_id,
                self._config.turn_timeout_s,
            )
            await bot.send_message(
                chat_id=chat_id,
                text="Turn timed out \u2014 Claude took too long to respond. Please try again.",
            )
        except Exception as exc:
            if _is_auth_error(exc):
                logger.warning("SDK auth error for user %d: %s", user_id, exc)
                await bot.send_message(
                    chat_id=chat_id,
                    text=(
                        "Auth expired \u2014 SSH in and run <code>claude</code> once "
                        "to refresh credentials, then retry."
                    ),
                    parse_mode="HTML",
                )
            else:
                logger.exception("SDK error during turn for user %d", user_id)
                await bot.send_message(
                    chat_id=chat_id,
                    text="An unexpected error occurred. Please try again.",
                )
        finally:
            turn_done.set()  # ensure generator exits if it wasn't set by ResultMessage
            # Release the session slot first so the next turn can start immediately.
            if session_uuid:
                _active_sessions.pop(session_uuid, None)

            if sdk_succeeded:
                await self._persist_session(user_id, new_session_uuid)
            else:
                # SDK failed: discard deferred flags without acting on them.
                # Command handlers (/new, picker) already wrote correct DB state.
                from tgclaude.handlers.messages import detach_after_turn, reattach_after_turn
                detach_after_turn.pop(user_id, None)
                reattach_after_turn.pop(user_id, None)

    # ------------------------------------------------------------------
    # SDK options builder
    # ------------------------------------------------------------------

    def _build_options(
        self,
        user_id: int,
        session_uuid: str | None,
        bot: Bot,
        chat_id: int,
    ) -> ClaudeAgentOptions:
        """Construct ClaudeAgentOptions for this turn."""
        kwargs: dict[str, Any] = {
            "cwd": str(self._config.claude_project_cwd),
        }
        if self._config.claude_binary:
            kwargs["cli_path"] = str(self._config.claude_binary)
        if session_uuid:
            kwargs["resume"] = session_uuid

        mode = self._config.permission_mode
        if mode == "bypass":
            kwargs["permission_mode"] = "bypassPermissions"
        elif mode in ("interactive", "readonly"):
            async def can_use_tool(
                tool_name: str,
                tool_input: Any,
                context: ToolPermissionContext,
            ):
                queue = self._tool_use_id_queues.get(user_id, collections.deque())
                tool_use_id = queue.popleft() if queue else ""
                return await self._can_use_tool(
                    tool_name=tool_name,
                    tool_input=tool_input,
                    tool_use_id=tool_use_id,
                    user_id=user_id,
                    session_uuid=session_uuid,
                    bot=bot,
                    chat_id=chat_id,
                )
            kwargs["can_use_tool"] = can_use_tool
            kwargs["permission_mode"] = "default"

        return ClaudeAgentOptions(**kwargs)

    # ------------------------------------------------------------------
    # Block dispatcher
    # ------------------------------------------------------------------

    async def _dispatch_block(
        self,
        block: Any,
        user_id: int,
        session_uuid: str | None,
        bot: Bot,
        chat_id: int,
        current_new_uuid: str | None,
        turn_done: asyncio.Event | None = None,
    ) -> str | None:
        """Route a single SDK content block to the correct handler.

        Returns the (potentially updated) session UUID.
        """
        if isinstance(block, AssistantMessage):
            for content in block.content:
                await self._handle_content_item(
                    content, user_id, session_uuid, bot, chat_id
                )
        elif isinstance(block, UserMessage):
            # ToolResultBlock appears inside a UserMessage in some SDK versions
            for content in block.content:
                if isinstance(content, ToolResultBlock):
                    await self._send_tool_result(content, user_id, bot, chat_id)
        elif isinstance(block, ResultMessage):
            if getattr(block, "is_error", False):
                logger.warning("SDK reported error in ResultMessage for turn")
            if turn_done is not None:
                turn_done.set()
            extracted = _extract_session_uuid(block)
            if extracted:
                return extracted
        elif isinstance(block, SystemMessage):
            pass  # System messages are not displayed to the user
        return current_new_uuid

    async def _handle_content_item(
        self,
        item: Any,
        user_id: int,
        session_uuid: str | None,
        bot: Bot,
        chat_id: int,
    ) -> None:
        """Handle a single content item within an AssistantMessage."""
        if isinstance(item, TextBlock):
            await self._send_text_block(item, bot, chat_id)
        elif isinstance(item, ThinkingBlock):
            pass  # Hidden in v1
        elif isinstance(item, ToolUseBlock):
            self._tool_use_id_queues.setdefault(user_id, collections.deque()).append(item.id)
            await self._send_tool_use_block(
                item, user_id, session_uuid, bot, chat_id
            )

    # ------------------------------------------------------------------
    # Block senders
    # ------------------------------------------------------------------

    async def _send_text_block(self, block: TextBlock, bot: Bot, chat_id: int) -> None:
        """Format a TextBlock through the markdown→HTML pipeline and send."""
        formatted = format_text(block.text)
        chunks = chunk_message(formatted)
        total = len(chunks)
        for idx, chunk in enumerate(chunks, start=1):
            text = chunk
            if total > 1:
                text = f"{chunk}\n<i>({idx}/{total})</i>"
            await bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML")

    async def _send_tool_use_block(
        self,
        block: ToolUseBlock,
        user_id: int,
        session_uuid: str | None,
        bot: Bot,
        chat_id: int,
    ) -> None:
        """Announce a tool-use block and track pending Write outputs.

        In bypass mode there is no can_use_tool callback, so this is the only
        place to send the announcement.  In interactive/readonly modes the
        can_use_tool callback (_can_use_tool) handles the announcement.
        """
        mode = self._config.permission_mode
        if mode == "bypass":
            text, _ = _build_tool_announcement(block.name, block.input)
            await bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML")

        # Track Write tool for post-result file dispatch (Fix 2).
        # Store path here; dispatch_file is called in _send_tool_result once the
        # file actually exists.
        if block.name == "Write" and isinstance(block.input, dict):
            file_path_str = block.input.get("file_path") or block.input.get("path")
            if file_path_str:
                self._pending_writes.setdefault(user_id, {})[block.id] = file_path_str
        # For interactive/readonly, the can_use_tool callback handles announcement.

    async def _send_tool_result(
        self, block: ToolResultBlock, user_id: int, bot: Bot, chat_id: int
    ) -> None:
        """Send raw tool result in a <pre> block, chunked if needed."""
        content = _extract_tool_result_content(block)
        if not content:
            return

        escaped = html.escape(content)
        full_text = f"<pre>{escaped}</pre>"

        # Chunk without running through format_text (raw terminal output)
        chunks = chunk_message(full_text)
        total = len(chunks)
        for idx, chunk in enumerate(chunks, start=1):
            text = chunk
            if total > 1:
                text = f"{chunk}\n<i>({idx}/{total})</i>"
            await bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML")

        # Dispatch any file written by a preceding Write tool now that it exists.
        tool_use_id = block.tool_use_id
        if tool_use_id:
            # Skip dispatch if the tool reported an error.
            tool_errored = block.is_error is True
            user_writes = self._pending_writes.get(user_id, {})
            file_path_str = user_writes.pop(tool_use_id, None)
            if file_path_str and not tool_errored:
                from pathlib import Path
                try:
                    await dispatch_file(Path(file_path_str), bot, chat_id)
                except Exception as exc:
                    logger.debug("dispatch_file failed for %s: %s", file_path_str, exc)

    # ------------------------------------------------------------------
    # Tool permission callback
    # ------------------------------------------------------------------

    async def _can_use_tool(
        self,
        tool_name: str,
        tool_input: Any,
        tool_use_id: str,
        user_id: int,
        session_uuid: str | None,
        bot: Bot,
        chat_id: int,
    ) -> PermissionResultAllow | PermissionResultDeny:
        """The can_use_tool callback passed to the SDK.

        Responsible for announcing the tool use in interactive/readonly modes
        (bypass mode announces in _send_tool_use_block instead).

        Internally uses dict protocol for futures; converts to SDK types before returning.
        """
        mode = self._config.permission_mode
        text_html, _ = _build_tool_announcement(tool_name, tool_input)

        if mode == "readonly":
            # Always announce so the user sees what Claude attempted.
            await bot.send_message(chat_id=chat_id, text=text_html, parse_mode="HTML")
            if tool_name in READONLY_TOOLS:
                logger.debug("readonly mode: auto-allowing %s", tool_name)
                return PermissionResultAllow()
            logger.info("readonly mode: denying %s for user %d", tool_name, user_id)
            return PermissionResultDeny(
                message="this bot is in readonly mode \u2014 tool rejected.",
            )

        # interactive mode: check existing grant first
        if session_uuid and await self._permission_manager.has_grant(
            user_id, session_uuid, tool_name
        ):
            logger.debug(
                "Auto-allowing %s (always-allow grant) for user %d", tool_name, user_id
            )
            # Announce without a keyboard (parity with manually-approved tools).
            await bot.send_message(chat_id=chat_id, text=text_html, parse_mode="HTML")
            return PermissionResultAllow()

        # interactive mode: ask via Telegram; result is internal dict protocol
        result = await self._ask_user_for_permission(
            tool_use_id=tool_use_id,
            user_id=user_id,
            bot=bot,
            chat_id=chat_id,
            text_html=text_html,
        )

        # Persist "always allow" grant if the user chose it
        if result.get("always") and session_uuid:
            await self._permission_manager.add_grant(user_id, session_uuid, tool_name)

        if result.get("allow"):
            return PermissionResultAllow()
        return PermissionResultDeny(message=result.get("message", ""))

    async def _ask_user_for_permission(
        self,
        tool_use_id: str,
        user_id: int,
        bot: Bot,
        chat_id: int,
        text_html: str,
    ) -> dict[str, Any]:
        """Send permission prompt, store Future, await resolution with timeout.

        *text_html* is the pre-built announcement HTML (from _can_use_tool) so we
        don't rebuild it here and can reuse it verbatim when editing the message.
        """
        keyboard = _build_permission_keyboard(tool_use_id)

        prompt_msg = await bot.send_message(
            chat_id=chat_id,
            text=text_html,
            parse_mode="HTML",
            reply_markup=keyboard,
        )

        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        pending_permissions[(user_id, tool_use_id)] = future

        try:
            result = await asyncio.wait_for(
                asyncio.shield(future),
                timeout=self._config.permission_timeout_s,
            )
        except asyncio.TimeoutError:
            result = {
                "allow": False,
                "message": "permission prompt timed out \u2014 no response from Telegram.",
            }
            if not future.done():
                future.set_result({"allow": False, "message": "Timed out waiting for permission."})
            # Clean up waiting_for_reason if user was in that state
            waiting_for_reason.pop(user_id, None)
            await _edit_permission_message(prompt_msg, text_html, "\u23f1 Timed out")
        else:
            note = "\u2705 Allowed" if result.get("allow") else "\u274c Denied"
            await _edit_permission_message(prompt_msg, text_html, note)
        finally:
            pending_permissions.pop((user_id, tool_use_id), None)

        return result

    # ------------------------------------------------------------------
    # Session persist step (§4)
    # ------------------------------------------------------------------

    async def _persist_session(
        self,
        user_id: int,
        new_session_uuid: str | None,
    ) -> None:
        """Run the session-persist step: check deferred flags and write UUID."""
        # Import here to avoid circular import; these dicts live in messages.py
        from tgclaude.handlers.messages import detach_after_turn, reattach_after_turn

        try:
            detach = detach_after_turn.pop(user_id, False)
            reattach_uuid = reattach_after_turn.pop(user_id, None)

            if detach:
                await self._db.clear_active_session(user_id)
                logger.debug("Detached user %d after turn (deferred flag)", user_id)
            elif reattach_uuid:
                await self._db.set_active_session(user_id, reattach_uuid)
                logger.debug(
                    "Reattached user %d to %s after turn (deferred flag)",
                    user_id,
                    reattach_uuid,
                )
            elif new_session_uuid:
                await self._db.set_active_session(user_id, new_session_uuid)
                logger.debug(
                    "Persisted session %s for user %d", new_session_uuid, user_id
                )
        except aiosqlite.IntegrityError:
            logger.warning(
                "Session UUID %s is already attached to another user; "
                "skipping persist for user %d (race condition)",
                new_session_uuid,
                user_id,
            )
        except Exception as exc:
            logger.exception(
                "Failed to persist session for user %d: %s", user_id, exc
            )


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def _is_auth_error(exc: Exception) -> bool:
    """Return True if the exception looks like an SDK authentication failure."""
    name = type(exc).__name__.lower()
    msg = str(exc).lower()
    return "auth" in name or "401" in msg or "unauthorized" in msg or "credential" in msg


def _extract_session_uuid(result: ResultMessage) -> str | None:
    """Extract the session UUID from a ResultMessage."""
    session_id = getattr(result, "session_id", None)
    if session_id and isinstance(session_id, str):
        return session_id
    return None


def _extract_tool_result_content(block: ToolResultBlock) -> str:
    """Extract the plain text content from a ToolResultBlock."""
    content = getattr(block, "content", None)
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                parts.append(item.get("text", ""))
            else:
                text = getattr(item, "text", None)
                if text:
                    parts.append(str(text))
        return "\n".join(parts)
    return str(content)
