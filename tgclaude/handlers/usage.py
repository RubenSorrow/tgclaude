from __future__ import annotations

import logging

from telegram import Update
from telegram.ext import ContextTypes

from tgclaude.usage_client import UsageAuthError, UsageFetchError, UsageSubscriptionError, UsageClient, render_usage

log = logging.getLogger(__name__)
logger = log


async def usage_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handler for /usage command.

    Fetches usage data via UsageClient stored in context.bot_data["usage_client"],
    renders with render_usage(), and sends as an HTML parse_mode message.

    On UsageAuthError: posts a human-readable auth-expired message.
    On UsageFetchError: posts a generic failure message.
    """
    if update.message is None or update.effective_user is None:
        return
    user_id = update.effective_user.id
    config = context.bot_data["config"]
    if user_id not in config.allowed_user_ids:
        logger.debug("Ignoring /usage from unlisted user %d", user_id)
        return

    usage_client: UsageClient = context.bot_data["usage_client"]

    try:
        data = await usage_client.get_usage()
        cache_age = usage_client.cache_age_seconds()
        text = render_usage(data, config.display_tz, cache_age)
        await update.message.reply_html(text)
    except UsageAuthError:
        log.warning("UsageAuthError while handling /usage for user %s", update.effective_user)
        await update.message.reply_text(
            "Auth expired \u2014 SSH in and run `claude` once to refresh.",
            parse_mode=None,
        )
    except UsageSubscriptionError:
        await update.message.reply_text(
            "/usage is only available for Claude Max subscription plans. "
            "This account does not appear to have an active subscription."
        )
    except UsageFetchError as exc:
        log.error("UsageFetchError while handling /usage: %s", exc)
        await update.message.reply_text("Failed to load usage data.")
