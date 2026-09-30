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


def test_two_digit_year_is_not_interpreted_as_1926():
    result = parse_date("Sun, 27 Sep 26 12:00:00 +0000")
    assert result.year == 2026


@pytest.mark.parametrize("bad", [None, "", "not a date", "Thu, 32 Xxx 2026"])
def test_parse_date_returns_none_rather_than_raising(bad):
    assert parse_date(bad) is None


def test_parsed_dates_are_always_timezone_aware():
    result = parse_date("2026-09-29 11:57")
    assert result.tzinfo is not None
