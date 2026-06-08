"""Unit tests for _error_handler in tgclaude.main.

Uses output-based / state-based testing (Khorikov style):
assert on observable side-effects (log records), not internal state.
asyncio_mode = "auto" is set in pyproject.toml so no explicit mark needed.
"""

from __future__ import annotations

import logging
from unittest.mock import MagicMock

import pytest
import telegram.error

from tgclaude.main import _error_handler


def _make_ctx(error: BaseException) -> MagicMock:
    ctx = MagicMock()
    ctx.error = error
    return ctx


# ---------------------------------------------------------------------------
# Transient errors — must be logged at WARNING, not ERROR
# ---------------------------------------------------------------------------


async def test_network_error_logged_at_warning(caplog: pytest.LogCaptureFixture) -> None:
    """NetworkError is a transient polling issue; must not surface as ERROR."""
    ctx = _make_ctx(telegram.error.NetworkError("Bad Gateway"))
    with caplog.at_level(logging.WARNING, logger="tgclaude.main"):
        await _error_handler(update=None, context=ctx)

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert warnings, "Expected at least one WARNING record for NetworkError"
    assert not errors, "NetworkError must NOT be logged at ERROR level"
    assert "Bad Gateway" in warnings[0].getMessage()


async def test_timed_out_logged_at_warning(caplog: pytest.LogCaptureFixture) -> None:
    """TimedOut is a transient polling issue; must not surface as ERROR."""
    ctx = _make_ctx(telegram.error.TimedOut())
    with caplog.at_level(logging.WARNING, logger="tgclaude.main"):
        await _error_handler(update=None, context=ctx)

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert warnings, "Expected at least one WARNING record for TimedOut"
    assert not errors, "TimedOut must NOT be logged at ERROR level"


async def test_retry_after_logged_at_warning(caplog: pytest.LogCaptureFixture) -> None:
    """RetryAfter is a transient flood-control signal; must not surface as ERROR."""
    ctx = _make_ctx(telegram.error.RetryAfter(retry_after=5))
    with caplog.at_level(logging.WARNING, logger="tgclaude.main"):
        await _error_handler(update=None, context=ctx)

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert warnings, "Expected at least one WARNING record for RetryAfter"
    assert not errors, "RetryAfter must NOT be logged at ERROR level"


# ---------------------------------------------------------------------------
# Non-transient errors — must be logged at ERROR
# ---------------------------------------------------------------------------


async def test_generic_exception_logged_at_error(caplog: pytest.LogCaptureFixture) -> None:
    """Unexpected exceptions must be surfaced at ERROR so they are not silently swallowed."""
    ctx = _make_ctx(RuntimeError("boom"))
    with caplog.at_level(logging.WARNING, logger="tgclaude.main"):
        await _error_handler(update=None, context=ctx)

    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    warnings_for_transient = [
        r for r in caplog.records
        if r.levelno == logging.WARNING and "suppressed" in r.getMessage()
    ]
    assert errors, "RuntimeError must be logged at ERROR level"
    assert not warnings_for_transient, (
        "RuntimeError must NOT produce a transient-suppressed WARNING"
    )
