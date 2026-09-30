"""Tests for lenient date parsing."""

from datetime import datetime

import pytest
from zoneinfo import ZoneInfo

from worker.collectors.dates import parse_date

UTC = ZoneInfo("UTC")


@pytest.mark.parametrize("raw,expected", [
    ("Sun, 27 Sep 26 12:00:00 +0000", datetime(2026, 9, 27, 12, 0, tzinfo=UTC)),   # CISA 2-digit year
    ("Sep 28, 2026 00:00:00-0400", datetime(2026, 9, 28, 4, 0, tzinfo=UTC)),      # CrowdStrike
    ("Tue, 29 Sep 2026 21:30:00 +1300", datetime(2026, 9, 29, 8, 30, tzinfo=UTC)), # SecurityBrief NZ
    ("2026-09-29T11:57:00Z", datetime(2026, 9, 29, 11, 57, tzinfo=UTC)),
    ("2026-09-29 11:57", datetime(2026, 9, 29, 11, 57, tzinfo=UTC)),                # assume UTC
])
def test_parse_date_handles_real_world_formats(raw, expected):
    assert parse_date(raw) == expected


@pytest.mark.parametrize("raw,expected_year", [
    ("Sun, 27 Sep 26 12:00:00 +0000", 2026),  # 2-digit year in 00-68 range → 20xx
    ("Wed, 15 Aug 68 09:30:00 +0000", 2068),  # boundary case: 68 → 2068
    ("Thu, 15 Aug 69 09:30:00 +0000", 1969),  # boundary case: 69 → 1969 (email.utils pivot)
])
def test_two_digit_year_pivot_boundary(raw, expected_year):
    """Verify 2-digit years pivot correctly: 00-68 → 2000-2068, 69-99 → 1969-1999."""
    result = parse_date(raw)
    assert result is not None
    assert result.year == expected_year


@pytest.mark.parametrize("bad", [None, "", "not a date", "Thu, 32 Xxx 2026"])
def test_parse_date_returns_none_rather_than_raising(bad):
    assert parse_date(bad) is None


def test_parsed_dates_are_always_timezone_aware():
    result = parse_date("2026-09-29 11:57")
    assert result.tzinfo is not None
