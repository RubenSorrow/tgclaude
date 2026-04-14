"""Application entry point.

Wires all components together and starts the Telegram long-polling loop.
"""

from __future__ import annotations

import asyncio
import logging
import re
import sys
from pathlib import Path

import httpx
from telegram import BotCommand
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    filters,
)

from tgclaude.auth import read_access_token, AuthError
from tgclaude.claude_bridge import ClaudeBridge
from tgclaude.config import load_config
from tgclaude.db import init_db
from tgclaude.handlers.alerts import alerts_command, alerts_poller
from tgclaude.handlers.commands import (
    delete_command,
    delete_confirm_callback,
    delete_picker_callback,
    help_command,
    list_command,
    new_command,
    permission_callback,
    picker_callback,
    start_command,
    whoami_command,
)
from tgclaude.handlers.messages import message_handler, unsupported_message_handler
from tgclaude.handlers.usage import usage_command
from tgclaude.permissions import PermissionManager
from tgclaude.usage_client import UsageClient

logger = logging.getLogger(__name__)

_ALERTS_POLL_INTERVAL_S = 300


# ---------------------------------------------------------------------------
# Startup / shutdown hooks
# ---------------------------------------------------------------------------


async def _startup(app: Application) -> None:
    """Initialise DB, usage client, and other shared resources."""
    config = app.bot_data["config"]
    access_token = app.bot_data["_access_token"]

    db = await init_db(config.database_path)
    app.bot_data["db"] = db

    http_client = httpx.AsyncClient(timeout=30.0)
    app.bot_data["_http_client"] = http_client

    redaction_filter = app.bot_data.get("_redaction_filter")
    usage_client = UsageClient(http_client, config.claude_home, redaction_filter=redaction_filter)
    app.bot_data["usage_client"] = usage_client

    permission_manager = PermissionManager(db)
    app.bot_data["permission_manager"] = permission_manager

    bridge = ClaudeBridge(config, db, permission_manager)
    app.bot_data["claude_bridge"] = bridge

    logger.info("tgclaude started (permission_mode=%s)", config.permission_mode)


async def _shutdown(app: Application) -> None:
    """Clean up DB and HTTP connections."""
    db = app.bot_data.get("db")
    if db:
        await db.close()
        logger.info("Database connection closed")

    http_client = app.bot_data.get("_http_client")
    if http_client:
        await http_client.aclose()


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------


def setup_logging(config) -> None:
    """Configure root logging with token redaction."""
    level = getattr(logging, config.log_level, logging.INFO)
    logging.basicConfig(
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        level=level,
    )

    # Silence noisy third-party loggers at WARNING
    for noisy in ("httpx", "telegram", "apscheduler"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def build_redaction_filter(secrets: list[str]) -> logging.Filter:
    """Return a Filter that replaces known secret literals with <REDACTED>.
    Also applies a regex fallback for token-shaped strings.
    """
    _TOKEN_RE = re.compile(
        r"sk-ant-[A-Za-z0-9_-]+"                                               # Anthropic API keys
        r"|eyJ[A-Za-z0-9_=-]+\.[A-Za-z0-9_=-]+(?:\.[A-Za-z0-9_=-]+)?"        # JWTs
        r"|[A-Za-z0-9+/]{60,}={0,2}"                                           # Long base64 OAuth tokens (60+ chars avoids UUIDs)
    )
    _mutable_secrets = list(secrets)  # mutable so new tokens can be added

    class _RedactionFilter(logging.Filter):
        def add_secret(self, secret: str) -> None:
            """Register a new secret literal for redaction (e.g. after token refresh)."""
            if secret and secret not in _mutable_secrets:
                _mutable_secrets.append(secret)

        def filter(self, record: logging.LogRecord) -> bool:
            msg = str(record.getMessage())
            # Pass 1: literal replacements (primary defense)
            for secret in _mutable_secrets:
                if secret and secret in msg:
                    msg = msg.replace(secret, "<REDACTED>")
            # Pass 2: regex fallback for token-shaped strings not already redacted
            msg = _TOKEN_RE.sub("<REDACTED>", msg)
            record.msg = msg
            record.args = ()
            return True

    return _RedactionFilter()


def _install_redaction_filter(config, access_token: str) -> logging.Filter:
    """Attach the redaction filter to the root logger and return it."""
    secrets = [config.bot_token, access_token]
    flt = build_redaction_filter(secrets)
    root = logging.getLogger()
    for handler in root.handlers:
        handler.addFilter(flt)
    return flt


# ---------------------------------------------------------------------------
# Bot commands (shown in Telegram's suggestion menu)
# ---------------------------------------------------------------------------

_BOT_COMMANDS = [
    BotCommand("start", "Session picker"),
    BotCommand("list", "Re-show the session picker"),
    BotCommand("new", "Start a fresh session"),
    BotCommand("usage", "Show Max-plan usage"),
    BotCommand("alerts", "Manage usage alerts"),
    BotCommand("whoami", "Show user ID and active session"),
    BotCommand("delete", "Permanently delete a session"),
    BotCommand("help", "Show available commands"),
]


async def _post_init(application: Application) -> None:
    """Initialise shared resources and register bot commands."""
    await _startup(application)
    await application.bot.set_my_commands(_BOT_COMMANDS)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """Entry point for the tgclaude bot.

    Pre-flight checks (fail-closed per §10):
    1. Load and validate config.
    2. Read OAuth access token from $CLAUDE_HOME/.credentials.json.
    3. Set up logging with redaction.
    4. Build Application, register handlers, configure job queue.
    5. Run long-polling (drop_pending_updates=True).
    """
    # 1. Config (validates all env vars; exits on failure)
    config = load_config()

    # 2. Auth (fail-closed)
    try:
        access_token = read_access_token(config.claude_home)
    except AuthError as exc:
        sys.exit(f"[tgclaude] Auth error: {exc}")

    # 3. Logging
    setup_logging(config)
    redaction_filter = _install_redaction_filter(config, access_token)

    logger.info(
        "tgclaude initialising | allowed_users=%d | permission_mode=%s",
        len(config.allowed_user_ids),
        config.permission_mode,
    )

    # 4. Build application
    app = (
        Application.builder()
        .token(config.bot_token)
        .concurrent_updates(True)
        .post_init(_post_init)
        .post_shutdown(_shutdown)
        .build()
    )

    # Stash config and token for startup hook
    app.bot_data["config"] = config
    app.bot_data["_access_token"] = access_token
    app.bot_data["_redaction_filter"] = redaction_filter

    # Register command handlers
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("list", list_command))
    app.add_handler(CommandHandler("new", new_command))
    app.add_handler(CommandHandler("whoami", whoami_command))
    app.add_handler(CommandHandler("usage", usage_command))
    app.add_handler(CommandHandler("alerts", alerts_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("delete", delete_command))

    # Free-text message handler
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler)
    )
    app.add_handler(
        MessageHandler(~filters.COMMAND & ~filters.TEXT, unsupported_message_handler)
    )

    # Callback query handlers
    app.add_handler(CallbackQueryHandler(picker_callback, pattern=r"^pick:"))
    app.add_handler(CallbackQueryHandler(permission_callback, pattern=r"^perm:"))
    app.add_handler(CallbackQueryHandler(delete_picker_callback, pattern=r"^del:"))
    app.add_handler(CallbackQueryHandler(delete_confirm_callback, pattern=r"^delconfirm:"))

    # Background job: usage alerts poller every 300 s
    if app.job_queue:
        app.job_queue.run_repeating(
            alerts_poller,
            interval=_ALERTS_POLL_INTERVAL_S,
            first=10,
            name="alerts_poller",
        )
    else:
        logger.warning(
            "JobQueue not available; usage alerts will not fire. "
            "Install python-telegram-bot[job-queue] to enable."
        )

    # 5. Run long-polling
    logger.info("Starting long-polling loop")
    app.run_polling(
        drop_pending_updates=True,
        allowed_updates=["message", "callback_query"],
    )


if __name__ == "__main__":
    main()
