"""Tests for per-credential daily request budgets."""

from datetime import UTC, datetime

import pytest

from free_claude_code.providers.daily_quota import DailyQuotaTracker


def _epoch(year: int, month: int, day: int, hour: int = 12) -> float:
    return datetime(year, month, day, hour, tzinfo=UTC).timestamp()


def test_no_limit_means_never_exhausted() -> None:
    tracker = DailyQuotaTracker(2)
    assert tracker.enabled is False
    for _ in range(1000):
        tracker.record(0)
    assert tracker.exhausted(0) is False
    assert tracker.exhausted_indices() == frozenset()


def test_credential_is_exhausted_once_the_budget_is_spent() -> None:
    tracker = DailyQuotaTracker(2, limit=3)
    now = _epoch(2026, 7, 31)
    for _ in range(3):
        tracker.record(0, now=now)
    assert tracker.exhausted(0, now=now) is True
    assert tracker.exhausted(1, now=now) is False
    assert tracker.exhausted_indices(now=now) == frozenset({0})


def test_budget_resets_at_the_day_boundary() -> None:
    """A daily cap resets on a wall clock, which cooldown timers cannot express."""
    tracker = DailyQuotaTracker(1, limit=2)
    late = _epoch(2026, 7, 31, hour=23)
    tracker.record(0, now=late)
    tracker.record(0, now=late)
    assert tracker.exhausted(0, now=late) is True

    next_day = _epoch(2026, 8, 1, hour=0)
    assert tracker.exhausted(0, now=next_day) is False
    assert tracker.snapshot(now=next_day)[0]["requests_today"] == 0


def test_reset_offset_shifts_the_day_boundary() -> None:
    """Gemini resets at Pacific midnight, not UTC midnight."""
    pacific = DailyQuotaTracker(1, limit=1, reset_offset_hours=-8.0)
    # 03:00 UTC on the 31st is still the 30th in Pacific time.
    late_utc = _epoch(2026, 7, 31, hour=3)
    pacific.record(0, now=late_utc)
    assert pacific.exhausted(0, now=late_utc) is True

    utc = DailyQuotaTracker(1, limit=1)
    utc.record(0, now=late_utc)
    # For a UTC-reset provider the same instant is already the new day...
    assert utc.exhausted(0, now=late_utc) is True
    # ...but the Pacific pool only rolls over eight hours later.
    after_pacific_midnight = _epoch(2026, 7, 31, hour=9)
    assert pacific.exhausted(0, now=after_pacific_midnight) is False


def test_snapshot_reports_remaining_and_reset() -> None:
    tracker = DailyQuotaTracker(2, limit=10)
    now = _epoch(2026, 7, 31, hour=12)
    tracker.record(0, now=now)
    tracker.record(0, now=now)
    entry = tracker.snapshot(now=now)[0]
    assert entry["requests_today"] == 2
    assert entry["daily_limit"] == 10
    assert entry["daily_remaining"] == 8
    # Twelve hours from noon UTC to the next UTC midnight.
    assert entry["seconds_until_reset"] == pytest.approx(12 * 3600, abs=1)


def test_snapshot_omits_limits_when_unset() -> None:
    entry = DailyQuotaTracker(1).snapshot()[0]
    assert entry["daily_limit"] is None
    assert entry["daily_remaining"] is None


def test_out_of_range_index_is_ignored() -> None:
    tracker = DailyQuotaTracker(1, limit=1)
    tracker.record(5)
    assert tracker.exhausted(5) is False


def test_key_count_must_be_positive() -> None:
    with pytest.raises(ValueError):
        DailyQuotaTracker(0)
