"""Source health assessment and lifecycle transitions.

A feed can answer HTTP 200 with valid XML and still be dead: the Google Security Blog moved
and its old feed kept serving fresh-looking responses. Freshness is therefore judged by the
age of the newest item, never by the fetch outcome alone.
"""

from collections.abc import Iterable, Sequence
from datetime import datetime, timedelta

from worker.collectors.http import FetchResult, FetchStatus
from worker.models import (
    HealthStatus,
    LifecycleState,
    NormalisedItem,
    SourceConfig,
    SourceHealth,
)

__all__ = [
    "DEGRADE_AFTER",
    "HealthStatus",
    "LifecycleState",
    "WARN_AFTER",
    "assess",
    "consecutive_failures",
    "next_lifecycle_state",
    "staleness_threshold",
]

# Days without a new item before a source of that frequency counts as stale.
STALE_AFTER_DAYS: dict[str, int] = {
    "real_time": 2,
    "hourly": 2,
    "daily": 7,
    "weekly": 30,
    "monthly": 90,
    # ASD's own news page: months between posts (231 days in 2025).
    "quarterly": 270,
}
DEFAULT_STALE_AFTER_DAYS = STALE_AFTER_DAYS["daily"]

# A newest-item date further ahead than this is treated as bogus, not as freshness.
FUTURE_TOLERANCE = timedelta(days=1)

# PLAN.md section 4.2 (SERAPH): 3 failures warn, 5 consecutive degrade.
WARN_AFTER = 3
DEGRADE_AFTER = 5

_SECONDS_PER_DAY = 86_400
_FAILURES = frozenset(
    {HealthStatus.ERROR, HealthStatus.TIMEOUT, HealthStatus.EMPTY, HealthStatus.STALE}
)


def staleness_threshold(source: SourceConfig) -> timedelta:
    days = STALE_AFTER_DAYS.get(source.expected_frequency, DEFAULT_STALE_AFTER_DAYS)
    return timedelta(days=days)


def _days(delta: timedelta) -> float:
    return delta.total_seconds() / _SECONDS_PER_DAY


def _newest_age(items: Iterable[NormalisedItem], now: datetime) -> timedelta | None:
    """Age of the newest item whose date is real.

    Estimated dates (normalise() backfills a missing date with fetched_at) and dates far in
    the future say nothing about when the feed last published, so they cannot vouch for it.
    """
    dates = [
        i.published
        for i in items
        if i.published is not None
        and not i.published_is_estimated
        and i.published <= now + FUTURE_TOLERANCE
    ]
    if not dates:
        return None
    return max(timedelta(0), now - max(dates))


def _stale_reason(age: timedelta, threshold: timedelta) -> str:
    return f"newest item is {_days(age):g} days old; expected one within {_days(threshold):g} days"


def _carry_forward(
    source: SourceConfig, fetch: FetchResult, now: datetime, history: Sequence[SourceHealth]
) -> SourceHealth:
    """A 304 confirms nothing changed *and* that the server answered, so the last content
    verdict (OK or STALE) stands, except that the newest item keeps ageing and can cross
    the staleness threshold while the feed says 304.

    A previous ERROR / TIMEOUT / EMPTY is not carried forward: any valid HTTP response,
    even Not Modified, proves that failure has cleared. Carrying it would keep a healthy,
    reachable source failing (and eventually degraded) for as long as it answers 304. The
    verdict is instead rebuilt from the last OK / STALE record, so a feed that was already
    stale before the blip is not laundered into OK.
    """
    verdicts = sorted(
        (h for h in history if h.status in (HealthStatus.OK, HealthStatus.STALE)),
        key=lambda h: h.checked_at,
        reverse=True,
    )
    previous = verdicts[0] if verdicts else None
    base = {"source_id": source.id, "checked_at": now, "items_fetched": 0,
            "duration_ms": fetch.duration_ms}
    if previous is None:
        return SourceHealth(status=HealthStatus.OK, **base)

    age_days = previous.newest_item_age_days
    if age_days is not None:
        age_days += _days(max(timedelta(0), now - previous.checked_at))
    status, error = previous.status, previous.error
    threshold = staleness_threshold(source)
    if (
        status in (HealthStatus.OK, HealthStatus.STALE)
        and age_days is not None
        and timedelta(days=age_days) > threshold
    ):
        status, error = HealthStatus.STALE, _stale_reason(timedelta(days=age_days), threshold)
    return SourceHealth(status=status, error=error, newest_item_age_days=age_days, **base)


def assess(
    source: SourceConfig,
    fetch: FetchResult,
    items: Sequence[NormalisedItem],
    *,
    now: datetime,
    history: Sequence[SourceHealth],
) -> SourceHealth:
    """Judge one fetch of `source`. `history` is that source's earlier health records."""
    base = {"source_id": source.id, "checked_at": now, "duration_ms": fetch.duration_ms}

    if not source.enabled:
        return SourceHealth(status=HealthStatus.DISABLED, **base)
    if fetch.status is FetchStatus.ERROR:
        return SourceHealth(status=HealthStatus.ERROR, error=fetch.error, **base)
    if fetch.status is FetchStatus.TIMEOUT:
        return SourceHealth(status=HealthStatus.TIMEOUT, error=fetch.error, **base)
    if fetch.status in (FetchStatus.NOT_MODIFIED, FetchStatus.SKIPPED):
        return _carry_forward(source, fetch, now, history)

    if not items:
        return SourceHealth(status=HealthStatus.EMPTY, error="fetch succeeded but returned no items", **base)

    age = _newest_age(items, now)
    if age is None:
        return SourceHealth(status=HealthStatus.OK, items_fetched=len(items), **base)
    threshold = staleness_threshold(source)
    if age > threshold:
        return SourceHealth(
            status=HealthStatus.STALE,
            error=_stale_reason(age, threshold),
            newest_item_age_days=_days(age),
            items_fetched=len(items),
            **base,
        )
    return SourceHealth(
        status=HealthStatus.OK,
        newest_item_age_days=_days(age),
        items_fetched=len(items),
        **base,
    )


def _meaningful(history: Iterable[SourceHealth]) -> list[SourceHealth]:
    """Chronological history without DISABLED entries, which say nothing about the feed."""
    return sorted(
        (h for h in history if h.status is not HealthStatus.DISABLED),
        key=lambda h: h.checked_at,
    )


def consecutive_failures(history: Iterable[SourceHealth]) -> int:
    """Length of the run of failed checks at the end of `history`."""
    count = 0
    for h in reversed(_meaningful(history)):
        if h.status not in _FAILURES:
            break
        count += 1
    return count


def next_lifecycle_state(source: SourceConfig, history: Sequence[SourceHealth]) -> LifecycleState:
    """The lifecycle state `source` should be in given its health history.

    Health alone only ever demotes an ACTIVE source (5 consecutive failures) or lets a
    DEGRADED/BROKEN one back in through TESTING. It never promotes: reaching ACTIVE stays
    SERAPH's gate, and the states before it are untouched here.
    """
    current = source.lifecycle_state or (
        LifecycleState.ACTIVE if source.enabled else LifecycleState.DISCOVERED
    )
    recent = _meaningful(history)

    if current is LifecycleState.ACTIVE:
        if consecutive_failures(recent) >= DEGRADE_AFTER:
            return LifecycleState.DEGRADED
        return current
    if current in (LifecycleState.DEGRADED, LifecycleState.BROKEN):
        if recent and recent[-1].status is HealthStatus.OK:
            return LifecycleState.TESTING
        return current
    return current
