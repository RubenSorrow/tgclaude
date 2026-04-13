from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta

from telegram import Update
from telegram.error import TelegramError
from telegram.ext import ContextTypes

from tgclaude.db import Database
from tgclaude.usage_client import UsageClient, UsageAuthError, UsageFetchError, BucketUsage
from tgclaude.usage_client import _format_countdown

log = logging.getLogger(__name__)

_SETTING_ALERTS_ENABLED = "alerts_enabled"
_SETTING_ALERT_THRESHOLDS = "alert_thresholds"

_BUCKET_DISPLAY: dict[str, str] = {
    "five_hour": "Current session",
    "seven_day": "Current week (all)",
    "seven_day_sonnet": "Current week (Sonnet)",
}


# ---------------------------------------------------------------------------
# /alerts command handler
# ---------------------------------------------------------------------------


async def alerts_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handler for /alerts command.

    Subcommands:
      /alerts on             → enable alerts
      /alerts off            → disable alerts
      /alerts thresholds X   → set thresholds (comma-separated ints 0-100)
      /alerts reset          → clear runtime DB overrides
      /alerts (no args)      → show current status
    """
    if update.message is None or update.effective_user is None:
        return
    user_id = update.effective_user.id
    config = context.bot_data["config"]
    if user_id not in config.allowed_user_ids:
        log.debug("Ignoring /alerts from unlisted user %d", user_id)
        return

    db: Database = context.bot_data["db"]

    raw_text: str = update.message.text or ""
    # Strip the command prefix (/alerts[@botname]) and leading whitespace
    parts = raw_text.strip().split(maxsplit=1)
    subcommand = parts[1].strip() if len(parts) > 1 else ""

    if subcommand == "on":
        await db.set_setting(_SETTING_ALERTS_ENABLED, "true")
        await update.message.reply_text("Alerts enabled.")

    elif subcommand == "off":
        await db.set_setting(_SETTING_ALERTS_ENABLED, "false")
        await update.message.reply_text("Alerts disabled.")

    elif subcommand.startswith("thresholds"):
        raw_values = subcommand[len("thresholds"):].strip()
        try:
            thresholds = _parse_thresholds(raw_values)
        except ValueError as exc:
            await update.message.reply_text(f"Invalid thresholds: {exc}")
            return
        csv = ",".join(str(t) for t in sorted(thresholds))
        await db.set_setting(_SETTING_ALERT_THRESHOLDS, csv)
        await update.message.reply_text(f"Alert thresholds set to: {csv}")

    elif subcommand == "reset":
        await db.delete_setting(_SETTING_ALERTS_ENABLED)
        await db.delete_setting(_SETTING_ALERT_THRESHOLDS)
        await update.message.reply_text(
            "Runtime alert overrides cleared. Using env-var defaults with alerts enabled."
        )

    else:
        # Show current status
        enabled_raw = await db.get_setting(_SETTING_ALERTS_ENABLED)
        enabled = enabled_raw != "false" if enabled_raw is not None else True

        thresholds_raw = await db.get_setting(_SETTING_ALERT_THRESHOLDS)
        if thresholds_raw:
            thresholds_display = thresholds_raw
            source = "runtime override"
        else:
            thresholds_display = ",".join(str(t) for t in sorted(config.alert_thresholds))
            source = "env default"

        status = "enabled" if enabled else "disabled"
        await update.message.reply_text(
            f"Alerts: {status}\n"
            f"Thresholds: {thresholds_display} ({source})"
        )


# ---------------------------------------------------------------------------
# Background polling job
# ---------------------------------------------------------------------------


async def alerts_poller(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Background job that runs every 5 minutes (registered via JobQueue).

    Fetches usage (bypassing cache), checks thresholds, and broadcasts alerts
    to all ALLOWED_USER_IDS when a bucket crosses a threshold for the first
    time in the current window.
    """
    db: Database = context.bot_data["db"]
    usage_client: UsageClient = context.bot_data["usage_client"]
    config = context.bot_data["config"]

    # 1. Check alerts_enabled
    enabled_raw = await db.get_setting(_SETTING_ALERTS_ENABLED)
    alerts_enabled = enabled_raw != "false" if enabled_raw is not None else True
    if not alerts_enabled:
        log.debug("alerts_poller: alerts disabled, skipping")
        return

    # 2. Fetch usage bypassing cache
    try:
        data = await usage_client.get_usage(bypass_cache=True)
    except UsageAuthError:
        log.warning("alerts_poller: UsageAuthError fetching usage; skipping this cycle")
        return
    except UsageFetchError as exc:
        log.warning("alerts_poller: UsageFetchError fetching usage: %s; skipping", exc)
        return

    # 3. Load effective thresholds
    thresholds_raw = await db.get_setting(_SETTING_ALERT_THRESHOLDS)
    if thresholds_raw:
        try:
            thresholds = _parse_thresholds(thresholds_raw)
        except ValueError:
            log.warning(
                "alerts_poller: invalid thresholds in DB %r; falling back to config", thresholds_raw
            )
            thresholds = list(config.alert_thresholds)
    else:
        thresholds = list(config.alert_thresholds)

    # 4. Check each bucket × threshold
    bucket_items: list[tuple[str, BucketUsage]] = [
        (key, getattr(data, key))
        for key in ("five_hour", "seven_day", "seven_day_sonnet")
        if getattr(data, key) is not None
    ]

    messages_to_send: list[str] = []

    for bucket_key, bucket in bucket_items:
        current_resets_at = (
            bucket.resets_at.isoformat() if bucket.resets_at is not None else "null"
        )

        # Bucket reset detection: clear stale state before the threshold loop.
        # Query any existing row for the bucket; if its stored resets_at differs
        # from the current one the window has rolled over — wipe the whole bucket
        # once, up front, so the threshold loop below only writes fresh rows.
        # Using any row (rather than min(thresholds)) means removed thresholds
        # don't leave stale rows that are never consulted as sentinels.
        any_state = await db.get_any_alert_state_for_bucket(bucket_key)
        if any_state is not None and any_state != current_resets_at:
            await db.clear_alert_state_for_bucket(bucket_key)
            log.debug(
                "alerts_poller: cleared stale alert state for bucket=%s (window reset)",
                bucket_key,
            )

        for threshold in sorted(thresholds):
            state = await db.get_alert_state(bucket_key, threshold)

            if bucket.utilization >= threshold:
                # Fire only if we haven't already fired for this resets_at window
                if state != current_resets_at:
                    label = _BUCKET_DISPLAY.get(bucket_key, bucket_key)
                    pct = round(bucket.utilization)
                    if bucket.resets_at is not None:
                        now_utc = datetime.now(timezone.utc)
                        delta = bucket.resets_at - now_utc
                        countdown = _format_countdown(delta)
                        msg = f"\u26a0 {label} at {pct}% \u00b7 resets {countdown}"
                    else:
                        msg = f"\u26a0 {label} at {pct}%"
                    messages_to_send.append(msg)
                    await db.set_alert_state(bucket_key, threshold, current_resets_at)
                    log.info(
                        "alerts_poller: firing alert bucket=%s threshold=%d utilization=%.1f",
                        bucket_key,
                        threshold,
                        bucket.utilization,
                    )

    if not messages_to_send:
        return

    messages_to_send[0] += "\n\n<i>Tip: /alerts off to silence · /alerts thresholds 80,95 to tune</i>"

    # 5. Broadcast to all allowed users
    for user_id in config.allowed_user_ids:
        for msg in messages_to_send:
            try:
                await context.bot.send_message(chat_id=user_id, text=msg, parse_mode="HTML")
            except TelegramError as exc:
                log.debug(
                    "alerts_poller: could not send to user_id=%d (likely never started bot): %s",
                    user_id,
                    exc,
                )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _parse_thresholds(raw: str) -> list[int]:
    """Parse a comma-separated string of threshold integers in [0, 100].

    Raises ValueError with a descriptive message on invalid input.
    """
    if not raw.strip():
        raise ValueError("threshold list is empty")

    values: list[int] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            val = int(part)
        except ValueError:
            raise ValueError(f"{part!r} is not an integer")
        if not (0 <= val <= 100):
            raise ValueError(f"{val} is out of range [0, 100]")
        values.append(val)

    if not values:
        raise ValueError("no valid thresholds found")

    return values
