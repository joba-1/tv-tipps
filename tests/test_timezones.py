"""Tests for app/timezones.py — range invariants and formatting."""
from datetime import datetime, timezone, timedelta
import pytest
from app.timezones import (
    utcnow, from_timestamp, to_local_str,
    prime_range, tonight_range, today_remaining_range, hours_range,
)
from config import settings


class TestUtcNow:
    def test_returns_naive_datetime(self):
        now = utcnow()
        assert now.tzinfo is None

    def test_close_to_real_now(self):
        expected = datetime.now(timezone.utc).replace(tzinfo=None)
        diff = abs((utcnow() - expected).total_seconds())
        assert diff < 2


class TestFromTimestamp:
    def test_known_epoch(self):
        # Unix 0 = 1970-01-01 00:00:00 UTC
        dt = from_timestamp(0)
        assert dt == datetime(1970, 1, 1, 0, 0, 0)
        assert dt.tzinfo is None

    def test_round_trip(self):
        ts = 1700000000
        dt = from_timestamp(ts)
        recovered = int(dt.replace(tzinfo=timezone.utc).timestamp())
        assert recovered == ts


class TestToLocalStr:
    def test_format(self):
        # Any naive UTC datetime should produce "YYYY-MM-DD HH:MM"
        dt = datetime(2024, 6, 15, 18, 0, 0)
        result = to_local_str(dt)
        assert len(result) == 16
        assert result[4] == "-" and result[7] == "-" and result[10] == " " and result[13] == ":"

    def test_shifts_to_local(self):
        # UTC 18:00 in Europe/Berlin (UTC+2 in summer) should be 20:00
        dt = datetime(2024, 7, 1, 18, 0, 0)
        result = to_local_str(dt)
        assert result.endswith("20:00")


class TestPrimeRange:
    def test_returns_two_naive_datetimes(self):
        start, end = prime_range()
        assert start.tzinfo is None
        assert end.tzinfo is None

    def test_duration_matches_settings(self):
        start, end = prime_range()
        expected_hours = settings.prime_end_hour - settings.prime_start_hour
        actual_hours = (end - start).total_seconds() / 3600
        assert actual_hours == pytest.approx(expected_hours, abs=0.1)

    def test_end_after_start(self):
        start, end = prime_range()
        assert end > start

    def test_window_is_future_or_tonight(self):
        start, end = prime_range()
        now = utcnow()
        # prime_range always returns a window that ends in the future
        assert end > now


class TestTonightRange:
    def test_returns_two_naive_datetimes(self):
        start, end = tonight_range()
        assert start.tzinfo is None
        assert end.tzinfo is None

    def test_end_after_start(self):
        start, end = tonight_range()
        assert end > start

    def test_window_at_most_8_hours(self):
        start, end = tonight_range()
        hours = (end - start).total_seconds() / 3600
        assert hours <= 8 + 0.01  # allow tiny float error

    def test_start_not_in_past(self):
        start, _ = tonight_range()
        now = utcnow()
        # Start should be >= now (clamped to current time if past 18:00)
        assert start >= now - timedelta(seconds=2)


class TestTodayRemainingRange:
    def test_start_close_to_now(self):
        start, _ = today_remaining_range()
        now = utcnow()
        assert abs((start - now).total_seconds()) < 2

    def test_end_is_future_midnight(self):
        _, end = today_remaining_range()
        now = utcnow()
        assert end > now
        # End should be within the next 24 hours
        assert (end - now).total_seconds() <= 86400 + 60

    def test_end_after_start(self):
        start, end = today_remaining_range()
        assert end > start


class TestHoursRange:
    def test_start_close_to_now(self):
        start, _ = hours_range(2)
        now = utcnow()
        assert abs((start - now).total_seconds()) < 2

    def test_end_is_start_plus_hours(self):
        start, end = hours_range(4)
        assert abs((end - start).total_seconds() - 4 * 3600) < 2
