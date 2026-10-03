"""How often the FAST lane collects, in one place.

The scheduler builds the FAST lane's cron and the alerts' minutes from it (worker/scheduler.py).
The watchdog's thresholds that follow the cadence are derived from it too
(worker/watchdog/checks.py), and so is how long PROWL and SERAPH read as active
(worker/publish/build.py). To change the cadence, change FAST_INTERVAL_MINUTES and nothing else.

It is a module of its own because the watchdog cannot import the scheduler: the scheduler
imports the watchdog. Hourly since 2026-10-04, for stability (PLAN.md §2.3).
"""

from datetime import timedelta

FAST_INTERVAL_MINUTES = 60

if not 0 < FAST_INTERVAL_MINUTES <= 60 or 60 % FAST_INTERVAL_MINUTES:
    # A cron minute list repeats every hour, so the interval has to divide it.
    raise ValueError(f"FAST_INTERVAL_MINUTES must divide 60, not {FAST_INTERVAL_MINUTES}")

FAST_INTERVAL = timedelta(minutes=FAST_INTERVAL_MINUTES)


def fast_minutes(every: int = FAST_INTERVAL_MINUTES) -> tuple[int, ...]:
    """The minutes past each hour the FAST lane runs at: (0,) hourly, (0, 30) half-hourly."""
    return tuple(range(0, 60, every))
