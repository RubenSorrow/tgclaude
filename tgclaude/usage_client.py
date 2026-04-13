from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

import httpx

from tgclaude.auth import read_access_token

log = logging.getLogger(__name__)

USAGE_ENDPOINT = "https://api.anthropic.com/api/oauth/usage"
CACHE_TTL_S = 60
BETA_HEADER = "oauth-2025-04-20"

_KNOWN_BUCKETS = {"five_hour", "seven_day", "seven_day_sonnet"}
_IGNORED_FIELDS = {
    "seven_day_opus",
    "seven_day_oauth_apps",
    "seven_day_cowork",
    "iguana_necktie",
}

_BUCKET_LABELS: dict[str, str] = {
    "five_hour": "Current session",
    "seven_day": "Current week (all)",
    "seven_day_sonnet": "Current week (Sonnet)",
}


class UsageAuthError(Exception):
    """Raised when the access token is invalid and cannot be refreshed from disk."""


class UsageFetchError(Exception):
    """Raised on non-auth HTTP errors while fetching usage data."""


@dataclass
class BucketUsage:
    utilization: float  # 0.0–100.0
    resets_at: datetime | None


@dataclass
class UsageData:
    five_hour: BucketUsage | None
    seven_day: BucketUsage | None
    seven_day_sonnet: BucketUsage | None
    extra_usage_enabled: bool
    extra_usage_monthly_limit: float | None
    extra_usage_used_credits: float | None
    extra_usage_utilization: float | None


class UsageClient:
    """Fetches and caches usage data from the Anthropic OAuth usage endpoint."""

    def __init__(self, http_client: httpx.AsyncClient, claude_home: Path) -> None:
        self._http = http_client
        self._claude_home = claude_home
        self._cached_data: UsageData | None = None
        self._cached_at: float | None = None
        self._token: str | None = None

    def _load_token(self) -> str:
        token = read_access_token(self._claude_home)
        self._token = token
        return token

    def _request_headers(self, token: str) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {token}",
            "anthropic-beta": BETA_HEADER,
            "User-Agent": "claude-cli/1.0.0",
        }

    @staticmethod
    def _parse_response(payload: dict[str, Any]) -> UsageData:
        def parse_bucket(raw: dict[str, Any] | None) -> BucketUsage | None:
            if raw is None:
                return None
            utilization: float = float(raw.get("utilization") or 0.0)
            resets_at_raw: str | None = raw.get("resets_at")
            resets_at: datetime | None = None
            if resets_at_raw is not None:
                resets_at = datetime.fromisoformat(resets_at_raw)
            return BucketUsage(utilization=utilization, resets_at=resets_at)

        five_hour = parse_bucket(payload.get("five_hour"))
        seven_day = parse_bucket(payload.get("seven_day"))
        seven_day_sonnet = parse_bucket(payload.get("seven_day_sonnet"))

        extra_raw: dict[str, Any] = payload.get("extra_usage") or {}
        extra_enabled: bool = bool(extra_raw.get("is_enabled", False))
        extra_monthly_limit: float | None = extra_raw.get("monthly_limit")
        extra_used_credits: float | None = extra_raw.get("used_credits")
        extra_utilization: float | None = extra_raw.get("utilization")

        return UsageData(
            five_hour=five_hour,
            seven_day=seven_day,
            seven_day_sonnet=seven_day_sonnet,
            extra_usage_enabled=extra_enabled,
            extra_usage_monthly_limit=extra_monthly_limit,
            extra_usage_used_credits=extra_used_credits,
            extra_usage_utilization=extra_utilization,
        )

    async def _fetch(self, token: str) -> UsageData:
        response = await self._http.get(
            USAGE_ENDPOINT,
            headers=self._request_headers(token),
        )
        if response.status_code == 401:
            raise UsageAuthError("401 Unauthorized from usage endpoint")
        if response.status_code != 200:
            raise UsageFetchError(
                f"Usage endpoint returned HTTP {response.status_code}: {response.text[:200]}"
            )
        payload: dict[str, Any] = response.json()
        return self._parse_response(payload)

    async def get_usage(self, bypass_cache: bool = False) -> UsageData:
        """Fetch usage. Cached for CACHE_TTL_S seconds unless bypass_cache=True.

        On 401: reload token from disk, retry once.
        On persistent 401: raise UsageAuthError.
        On other HTTP errors: raise UsageFetchError.
        """
        now = time.monotonic()

        if (
            not bypass_cache
            and self._cached_data is not None
            and self._cached_at is not None
            and (now - self._cached_at) < CACHE_TTL_S
        ):
            log.debug("Returning cached usage data (age %.1fs)", now - self._cached_at)
            return self._cached_data

        token = self._token or self._load_token()

        try:
            data = await self._fetch(token)
        except UsageAuthError:
            log.info("401 from usage endpoint; reloading token from disk and retrying")
            token = self._load_token()
            try:
                data = await self._fetch(token)
            except UsageAuthError as exc:
                raise UsageAuthError(
                    "Access token invalid after reload — SSH in and run `claude` once to refresh."
                ) from exc

        self._cached_data = data
        self._cached_at = now
        return data

    def cache_age_seconds(self) -> float | None:
        """Return seconds since last successful fetch, or None if never fetched."""
        if self._cached_at is None:
            return None
        return time.monotonic() - self._cached_at


# ---------------------------------------------------------------------------
# Rendering helpers
# ---------------------------------------------------------------------------

_BAR_WIDTH = 20
_FILLED = "\u2588"  # █
_EMPTY = "\u2591"   # ░


def _progress_bar(utilization: float) -> str:
    """Build a 20-character Unicode block progress bar."""
    filled = round(max(0.0, min(100.0, utilization)) * _BAR_WIDTH / 100)
    return _FILLED * filled + _EMPTY * (_BAR_WIDTH - filled)


def _format_countdown(delta: timedelta) -> str:
    """Format a timedelta as 'in Xd Yh', 'in Xh Ym', or 'in Xm'."""
    total_seconds = int(delta.total_seconds())
    if total_seconds <= 0:
        return "now"

    days, remainder = divmod(total_seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes = remainder // 60

    if days > 0:
        return f"in {days}d {hours}h"
    if hours > 0:
        return f"in {hours}h {minutes}m"
    return f"in {minutes}m"


def _format_reset_time(resets_at: datetime, display_tz: str | None) -> str:
    """Format the reset time in the display timezone, e.g. '10pm (America/Phoenix)'."""
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    tz = None
    tz_label = ""
    if display_tz:
        try:
            tz = ZoneInfo(display_tz)
            tz_label = f" ({display_tz})"
        except (ZoneInfoNotFoundError, Exception):
            log.warning("Unknown display timezone %r; falling back to local time", display_tz)

    if tz is None:
        local_dt = resets_at.astimezone()
        # Use system timezone name if available
        tz_name = local_dt.strftime("%Z")
        tz_label = f" ({tz_name})" if tz_name else ""
    else:
        local_dt = resets_at.astimezone(tz)

    # Format: "10pm" or "Apr 18, 5pm"
    now_local = datetime.now(local_dt.tzinfo)
    if local_dt.date() == now_local.date():
        time_str = local_dt.strftime("%-I%p").lower()  # e.g. "10pm"
    else:
        time_str = local_dt.strftime("%b %-d, %-I%p").lower()  # e.g. "apr 18, 5pm"
        # Capitalise month abbreviation
        time_str = time_str[:3].capitalize() + time_str[3:]

    return f"{time_str}{tz_label}"


def render_usage(data: UsageData, display_tz: str | None, cache_age: float | None) -> str:
    """Render UsageData as Telegram HTML.

    Format:
        📊 Usage (updated Ns ago)

        Current session          ██░░░░░░░░░░░░░░░░░░  7%
          Resets 10pm (America/Phoenix) · in 4h 23m

        ...

        Extra usage: not enabled
    """
    title = "📊 Usage"
    if cache_age is not None:
        title += f"  <i>(updated {int(cache_age)}s ago)</i>"

    buckets: list[tuple[str, BucketUsage]] = []
    for key in ("five_hour", "seven_day", "seven_day_sonnet"):
        bucket: BucketUsage | None = getattr(data, key)
        if bucket is not None:
            buckets.append((_BUCKET_LABELS[key], bucket))

    lines: list[str] = []
    now_utc = datetime.now(timezone.utc)

    for label, bucket in buckets:
        bar = _progress_bar(bucket.utilization)
        pct = round(bucket.utilization)
        # Pad label to 24 chars for alignment within the <pre> block
        padded_label = label.ljust(24)
        lines.append(f"{padded_label} {bar}  {pct}%")

        if bucket.resets_at is None:
            lines.append("  (not yet active this week)")
        else:
            reset_str = _format_reset_time(bucket.resets_at, display_tz)
            delta = bucket.resets_at - now_utc
            countdown = _format_countdown(delta)
            lines.append(f"  Resets {reset_str} \u00b7 {countdown}")

        lines.append("")  # blank line between buckets

    # Remove trailing blank line if present
    while lines and lines[-1] == "":
        lines.pop()

    pre_content = "\n".join(lines)

    # Extra usage footer
    if data.extra_usage_enabled:
        used = data.extra_usage_used_credits
        limit = data.extra_usage_monthly_limit
        if used is not None and limit is not None:
            extra_line = f"Extra usage: ${used:.2f} / ${limit:.2f}"
        else:
            extra_line = "Extra usage: enabled"
    else:
        extra_line = "Extra usage: not enabled"

    return f"{title}\n\n<pre>{pre_content}</pre>\n\n{extra_line}"
