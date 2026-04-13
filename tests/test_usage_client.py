from __future__ import annotations

from datetime import datetime, timezone, timedelta
from typing import Any

import pytest

from tgclaude.usage_client import (
    BucketUsage,
    UsageClient,
    UsageData,
    _format_countdown,
    _progress_bar,
    render_usage,
    _BAR_WIDTH,
    _FILLED,
    _EMPTY,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_data(
    five_hour: BucketUsage | None = None,
    seven_day: BucketUsage | None = None,
    seven_day_sonnet: BucketUsage | None = None,
    extra_enabled: bool = False,
    extra_monthly_limit: float | None = None,
    extra_used_credits: float | None = None,
    extra_utilization: float | None = None,
) -> UsageData:
    return UsageData(
        five_hour=five_hour,
        seven_day=seven_day,
        seven_day_sonnet=seven_day_sonnet,
        extra_usage_enabled=extra_enabled,
        extra_usage_monthly_limit=extra_monthly_limit,
        extra_usage_used_credits=extra_used_credits,
        extra_usage_utilization=extra_utilization,
    )


def _bucket(utilization: float, resets_at: datetime | None = None) -> BucketUsage:
    return BucketUsage(utilization=utilization, resets_at=resets_at)


def _future_resets_at(hours: int = 5) -> datetime:
    return datetime.now(timezone.utc) + timedelta(hours=hours)


# ---------------------------------------------------------------------------
# Progress bar tests
# ---------------------------------------------------------------------------


class TestProgressBar:
    def test_zero_percent(self) -> None:
        bar = _progress_bar(0.0)
        assert bar == _EMPTY * _BAR_WIDTH
        assert len(bar) == _BAR_WIDTH

    def test_fifty_percent(self) -> None:
        bar = _progress_bar(50.0)
        assert len(bar) == _BAR_WIDTH
        filled_count = bar.count(_FILLED)
        empty_count = bar.count(_EMPTY)
        assert filled_count == 10
        assert empty_count == 10

    def test_hundred_percent(self) -> None:
        bar = _progress_bar(100.0)
        assert bar == _FILLED * _BAR_WIDTH
        assert len(bar) == _BAR_WIDTH

    def test_clamps_above_100(self) -> None:
        bar = _progress_bar(150.0)
        assert bar == _FILLED * _BAR_WIDTH

    def test_clamps_below_0(self) -> None:
        bar = _progress_bar(-10.0)
        assert bar == _EMPTY * _BAR_WIDTH

    def test_seven_percent(self) -> None:
        bar = _progress_bar(7.0)
        # 7% of 20 = 1.4 → rounds to 1
        assert bar.count(_FILLED) == 1
        assert len(bar) == _BAR_WIDTH


# ---------------------------------------------------------------------------
# Countdown format tests
# ---------------------------------------------------------------------------


class TestFormatCountdown:
    def test_days_and_hours(self) -> None:
        delta = timedelta(days=6, hours=19)
        result = _format_countdown(delta)
        assert result == "in 6d 19h"

    def test_hours_and_minutes(self) -> None:
        delta = timedelta(hours=4, minutes=23)
        result = _format_countdown(delta)
        assert result == "in 4h 23m"

    def test_minutes_only(self) -> None:
        delta = timedelta(minutes=37)
        result = _format_countdown(delta)
        assert result == "in 37m"

    def test_zero_or_negative(self) -> None:
        result = _format_countdown(timedelta(seconds=0))
        assert result == "now"
        result_neg = _format_countdown(timedelta(seconds=-100))
        assert result_neg == "now"

    def test_exactly_one_hour(self) -> None:
        delta = timedelta(hours=1)
        result = _format_countdown(delta)
        assert result == "in 1h 0m"

    def test_exactly_one_day(self) -> None:
        delta = timedelta(days=1)
        result = _format_countdown(delta)
        assert result == "in 1d 0h"


# ---------------------------------------------------------------------------
# render_usage tests
# ---------------------------------------------------------------------------


class TestRenderUsage:
    def test_null_resets_at_renders_not_yet_active(self) -> None:
        data = _make_data(
            seven_day_sonnet=_bucket(0.0, resets_at=None),
        )
        output = render_usage(data, display_tz=None, cache_age=None)
        assert "(not yet active this week)" in output

    def test_null_bucket_skipped(self) -> None:
        data = _make_data(
            five_hour=None,
            seven_day=_bucket(1.0, _future_resets_at()),
            seven_day_sonnet=None,
        )
        output = render_usage(data, display_tz=None, cache_age=None)
        assert "Current session" not in output
        assert "Current week (all)" in output
        assert "Current week (Sonnet)" not in output

    def test_extra_usage_disabled_footer(self) -> None:
        data = _make_data(extra_enabled=False)
        output = render_usage(data, display_tz=None, cache_age=None)
        assert "Extra usage: not enabled" in output

    def test_extra_usage_enabled_with_credits(self) -> None:
        data = _make_data(
            extra_enabled=True,
            extra_used_credits=4.50,
            extra_monthly_limit=20.0,
        )
        output = render_usage(data, display_tz=None, cache_age=None)
        assert "$4.50 / $20.00" in output

    def test_cache_age_hint_present(self) -> None:
        data = _make_data()
        output = render_usage(data, display_tz=None, cache_age=42.7)
        assert "(updated 42s ago)" in output

    def test_cache_age_hint_absent_when_none(self) -> None:
        data = _make_data()
        output = render_usage(data, display_tz=None, cache_age=None)
        assert "updated" not in output

    def test_html_pre_tags_present(self) -> None:
        data = _make_data(five_hour=_bucket(7.0, _future_resets_at()))
        output = render_usage(data, display_tz=None, cache_age=None)
        assert "<pre>" in output
        assert "</pre>" in output

    def test_title_present(self) -> None:
        data = _make_data()
        output = render_usage(data, display_tz=None, cache_age=None)
        assert "📊 Usage" in output

    def test_all_buckets_rendered(self) -> None:
        data = _make_data(
            five_hour=_bucket(7.0, _future_resets_at(hours=4)),
            seven_day=_bucket(1.0, _future_resets_at(hours=7 * 24 - 5)),
            seven_day_sonnet=_bucket(0.0, resets_at=None),
        )
        output = render_usage(data, display_tz=None, cache_age=None)
        assert "Current session" in output
        assert "Current week (all)" in output
        assert "Current week (Sonnet)" in output


# ---------------------------------------------------------------------------
# UsageClient._parse_response tests (against real JSON shape from §7)
# ---------------------------------------------------------------------------


class TestParseResponse:
    _REAL_RESPONSE: dict[str, Any] = {
        "five_hour": {"utilization": 7.0, "resets_at": "2026-04-12T05:00:00+00:00"},
        "seven_day": {"utilization": 1.0, "resets_at": "2026-04-19T00:00:00+00:00"},
        "seven_day_sonnet": {"utilization": 0.0, "resets_at": None},
        "seven_day_opus": None,
        "seven_day_oauth_apps": None,
        "seven_day_cowork": None,
        "iguana_necktie": None,
        "extra_usage": {
            "is_enabled": False,
            "monthly_limit": None,
            "used_credits": None,
            "utilization": None,
        },
    }

    def test_five_hour_parsed(self) -> None:
        data = UsageClient._parse_response(self._REAL_RESPONSE)
        assert data.five_hour is not None
        assert data.five_hour.utilization == 7.0
        expected_resets_at = datetime(2026, 4, 12, 5, 0, 0, tzinfo=timezone.utc)
        assert data.five_hour.resets_at == expected_resets_at

    def test_seven_day_parsed(self) -> None:
        data = UsageClient._parse_response(self._REAL_RESPONSE)
        assert data.seven_day is not None
        assert data.seven_day.utilization == 1.0
        expected_resets_at = datetime(2026, 4, 19, 0, 0, 0, tzinfo=timezone.utc)
        assert data.seven_day.resets_at == expected_resets_at

    def test_seven_day_sonnet_null_resets_at(self) -> None:
        data = UsageClient._parse_response(self._REAL_RESPONSE)
        assert data.seven_day_sonnet is not None
        assert data.seven_day_sonnet.utilization == 0.0
        assert data.seven_day_sonnet.resets_at is None

    def test_null_buckets_yield_none(self) -> None:
        data = UsageClient._parse_response(self._REAL_RESPONSE)
        # iguana_necktie and others are not stored on UsageData at all;
        # the three known buckets should be present
        assert data.five_hour is not None
        assert data.seven_day is not None
        assert data.seven_day_sonnet is not None

    def test_extra_usage_disabled(self) -> None:
        data = UsageClient._parse_response(self._REAL_RESPONSE)
        assert data.extra_usage_enabled is False
        assert data.extra_usage_monthly_limit is None
        assert data.extra_usage_used_credits is None
        assert data.extra_usage_utilization is None

    def test_extra_usage_enabled_with_values(self) -> None:
        payload = dict(self._REAL_RESPONSE)
        payload["extra_usage"] = {
            "is_enabled": True,
            "monthly_limit": 20.0,
            "used_credits": 4.5,
            "utilization": 22.5,
        }
        data = UsageClient._parse_response(payload)
        assert data.extra_usage_enabled is True
        assert data.extra_usage_monthly_limit == 20.0
        assert data.extra_usage_used_credits == 4.5
        assert data.extra_usage_utilization == 22.5

    def test_all_null_buckets(self) -> None:
        payload: dict[str, Any] = {
            "five_hour": None,
            "seven_day": None,
            "seven_day_sonnet": None,
            "extra_usage": {"is_enabled": False, "monthly_limit": None, "used_credits": None, "utilization": None},
        }
        data = UsageClient._parse_response(payload)
        assert data.five_hour is None
        assert data.seven_day is None
        assert data.seven_day_sonnet is None
