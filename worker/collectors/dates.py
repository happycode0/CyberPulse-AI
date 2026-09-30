"""Lenient date parsing for real-world feed date formats."""

from datetime import datetime
import re
from email.utils import parsedate_to_datetime
from zoneinfo import ZoneInfo


UTC = ZoneInfo("UTC")


def parse_date(value: str | None) -> datetime | None:
    """
    Parse a date string in various formats, normalizing to UTC.
    
    Handles:
    - RFC 2822 with 2-digit years (CISA: "Sun, 27 Sep 26 12:00:00 +0000")
    - Custom formats: "Sep 28, 2026 00:00:00-0400"
    - ISO 8601: "2026-09-29T11:57:00Z", "2026-09-29 11:57"
    - Assumes UTC when no timezone is present
    - 2-digit years pivot on current century (26 → 2026, not 1926)
    
    Returns None on parse failure; never raises.
    """
    if not value or not isinstance(value, str):
        return None
    
    value = value.strip()
    if not value:
        return None
    
    # Try email.utils.parsedate_to_datetime first (handles RFC 2822)
    try:
        dt = parsedate_to_datetime(value)
        # Fix 2-digit years: if year < 1000, pivot on current century
        if dt.year < 1000:
            # Assume years 00-99 map to 2000-2099
            dt = dt.replace(year=2000 + dt.year)
        # Ensure UTC-aware
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt.astimezone(UTC)
    except (TypeError, ValueError):
        pass
    
    # Try ISO format with fromisoformat
    try:
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt.astimezone(UTC)
    except ValueError:
        pass
    
    # Try custom format: "Sep 28, 2026 00:00:00-0400"
    # Matches: "Mon, 29 Sep 2026 HH:MM:SS±offset" or "Mon 29 Sep 2026 HH:MM:SS±offset"
    match = re.match(
        r'^\w{3},?\s+(\d{1,2})\s+(\w{3})\s+(\d{4})\s+(\d{1,2}):(\d{2}):(\d{2})\s*([-+])(\d{2}):?(\d{2})$',
        value
    )
    if match:
        day, month_str, year, hour, minute, second, tz_sign, tz_hours, tz_mins = match.groups()
        months = {
            'Jan': 1, 'Feb': 2, 'Mar': 3, 'Apr': 4, 'May': 5, 'Jun': 6,
            'Jul': 7, 'Aug': 8, 'Sep': 9, 'Oct': 10, 'Nov': 11, 'Dec': 12
        }
        if month_str not in months:
            return None
        
        try:
            dt = datetime(
                year=int(year),
                month=months[month_str],
                day=int(day),
                hour=int(hour),
                minute=int(minute),
                second=int(second)
            )
            # Apply timezone offset
            offset_mins = int(tz_hours) * 60 + int(tz_mins)
            if tz_sign == '-':
                offset_mins = -offset_mins
            tz = ZoneInfo(f"UTC{tz_sign}{tz_hours}:{tz_mins}")
            dt = dt.replace(tzinfo=tz)
            return dt.astimezone(UTC)
        except (ValueError, Exception):
            pass
    
    # Try another custom format without leading day name
    match = re.match(
        r'^(\w{3})\s+(\d{1,2}),?\s+(\d{4})\s+(\d{1,2}):(\d{2}):(\d{2})\s*([-+])(\d{2}):?(\d{2})$',
        value
    )
    if match:
        month_str, day, year, hour, minute, second, tz_sign, tz_hours, tz_mins = match.groups()
        months = {
            'Jan': 1, 'Feb': 2, 'Mar': 3, 'Apr': 4, 'May': 5, 'Jun': 6,
            'Jul': 7, 'Aug': 8, 'Sep': 9, 'Oct': 10, 'Nov': 11, 'Dec': 12
        }
        if month_str not in months:
            return None
        
        try:
            dt = datetime(
                year=int(year),
                month=months[month_str],
                day=int(day),
                hour=int(hour),
                minute=int(minute),
                second=int(second)
            )
            # Apply timezone offset
            offset_mins = int(tz_hours) * 60 + int(tz_mins)
            if tz_sign == '-':
                offset_mins = -offset_mins
            tz = ZoneInfo(f"UTC{tz_sign}{tz_hours}:{tz_mins}")
            dt = dt.replace(tzinfo=tz)
            return dt.astimezone(UTC)
        except (ValueError, Exception):
            pass
    
    # Try simple formats: "2026-09-29 11:57" or "2026-09-29"
    match = re.match(r'^(\d{4})-(\d{2})-(\d{2})(?:\s+(\d{1,2}):(\d{2}))?$', value)
    if match:
        year, month, day, hour, minute = match.groups()
        try:
            dt = datetime(
                year=int(year),
                month=int(month),
                day=int(day),
                hour=int(hour) if hour else 0,
                minute=int(minute) if minute else 0,
                tzinfo=UTC
            )
            return dt
        except ValueError:
            pass
    
    return None
