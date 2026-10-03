"""What the watchdog reads to build its snapshot (worker/watchdog/checks.py): every part on its
own read-only connection, so one that cannot be read leaves only its own kinds uncovered.
"""

import logging
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta
from decimal import Decimal
from typing import TypeVar
from urllib.parse import urlsplit

from sqlalchemy import Connection, Engine, text
from sqlalchemy.exc import SQLAlchemyError

from worker.db.digest import month_bounds
from worker.db.jobs import load_latest_jobs
from worker.watchdog.checks import (
    SOURCE_HISTORY,
    Cost,
    Jobs,
    LaneRun,
    Snapshot,
    SourceState,
    Volume,
)

logger = logging.getLogger(__name__)

STATEMENT_TIMEOUT = "15s"
# The earlier days a usual day is the median of.
USUAL_DAYS = 7
# The lanes with a schedule (worker/scheduler.py), and the most runs a check looks back on.
LANES = ("fast", "normal")
LANE_RUNS = 3

T = TypeVar("T")


@contextmanager
def _read(engine: Engine) -> Iterator[Connection]:
    with engine.connect() as conn:
        transaction = conn.begin()
        try:
            conn.execute(text("set transaction read only"))
            conn.execute(text(f"set local statement_timeout = '{STATEMENT_TIMEOUT}'"))
            yield conn
        finally:
            transaction.rollback()


def ping(engine: Engine) -> bool:
    try:
        with engine.connect() as conn:
            conn.execute(text("select 1"))
    except SQLAlchemyError:
        return False
    return True


def _part(engine: Engine, name: str, read: Callable[[Connection], T]) -> T | None:
    try:
        with _read(engine) as conn:
            return read(conn)
    except SQLAlchemyError as exc:
        logger.warning("watchdog could not read its %s: %s", name, type(exc).__name__)
        return None


def lanes(conn: Connection) -> dict[str, list[LaneRun]]:
    rows = conn.execute(
        text(
            "select lane, finished_at, items_fetched from (select lane, finished_at, "
            "items_fetched, row_number() over (partition by lane "
            "order by finished_at desc, run_id desc) as n from runs "
            "where finished_at is not null and lane = any(cast(:lanes as text[]))) r "
            "where n <= :n order by lane, finished_at desc"
        ),
        {"lanes": list(LANES), "n": LANE_RUNS},
    )
    out: dict[str, list[LaneRun]] = {lane: [] for lane in LANES}
    for r in rows:
        out[r.lane].append(LaneRun(r.finished_at, r.items_fetched))
    return out


def sources(conn: Connection) -> list[SourceState]:
    rows = conn.execute(
        text(
            "select r.id, r.name, r.lane, r.priority, r.url, r.lifecycle_state, h.statuses, "
            "h.last_error, h.last_checked, exists (select 1 from source_health s "
            "where s.source_id = r.id and s.items_fetched > 0) as had_items "
            "from source_registry r left join lateral (select "
            "array_agg(x.status order by x.checked_at, x.id) as statuses, "
            "(array_agg(x.error order by x.checked_at desc, x.id desc))[1] as last_error, "
            "max(x.checked_at) as last_checked from (select status, error, checked_at, id "
            "from source_health where source_id = r.id and status <> 'disabled' "
            "order by checked_at desc, id desc limit :n) x) h on true "
            "where r.enabled order by r.id"
        ),
        {"n": SOURCE_HISTORY},
    )
    return [
        SourceState(
            source_id=r.id,
            name=r.name,
            lane=r.lane,
            priority=r.priority,
            host=urlsplit(r.url).hostname or "",
            lifecycle=r.lifecycle_state,
            statuses=tuple(r.statuses or ()),
            had_items=r.had_items,
            last_error=r.last_error,
            last_checked=r.last_checked,
        )
        for r in rows
    ]


def _usual_days(conn: Connection, now: datetime, first: datetime | None, sql: str) -> list:
    """The figure `sql` gives for each of the USUAL_DAYS 24 hours before the last, newest
    first, leaving out any day that starts before a full day of history."""
    if first is None:
        return []
    rows = conn.execute(
        text(
            "select k, (" + sql + ") as value from generate_series(1, :days) as k "
            "where cast(:now as timestamptz) - make_interval(days => k + 1) >= :earliest "
            "order by k"
        ),
        {"now": now, "days": USUAL_DAYS, "earliest": first + timedelta(days=1)},
    )
    return [r.value for r in rows]


# An event is fresh when it is less than a week older than when we first saw it, so a new
# source's back catalogue is not read as a flood.
_FRESH = "e.first_seen >= e.created_at - interval '7 days'"
_DAY_START = "cast(:now as timestamptz) - make_interval(days => k + 1)"
_DAY_END = "cast(:now as timestamptz) - make_interval(days => k)"


def volume(conn: Connection, now: datetime) -> Volume:
    fresh_24h, fresh_6h = conn.execute(
        text(
            "select count(*), count(*) filter (where e.created_at >= :six) from events e "
            f"where e.created_at >= :day and {_FRESH}"
        ),
        {"day": now - timedelta(days=1), "six": now - timedelta(hours=6)},
    ).one()
    first = conn.execute(text("select min(created_at) from events")).scalar()
    usual = _usual_days(
        conn,
        now,
        first,
        "select count(*) from events e where e.created_at >= "
        f"{_DAY_START} and e.created_at < {_DAY_END} and {_FRESH}",
    )
    return Volume(fresh_24h, fresh_6h, tuple(int(v) for v in usual))


def cost(conn: Connection, now: datetime, budget: Decimal) -> Cost:
    start, end = month_bounds(now)
    last_24h, month = conn.execute(
        text(
            "select coalesce(sum(cost_usd) filter (where ts >= :day), 0), "
            "coalesce(sum(cost_usd) filter (where ts >= :start and ts < :end), 0) "
            "from cost_ledger where ts >= least(:day, :start)"
        ),
        {"day": now - timedelta(days=1), "start": start, "end": end},
    ).one()
    first = conn.execute(text("select min(ts) from cost_ledger")).scalar()
    usual = _usual_days(
        conn,
        now,
        first,
        "select coalesce(sum(c.cost_usd), 0) from cost_ledger c "
        f"where c.ts >= {_DAY_START} and c.ts < {_DAY_END}",
    )
    return Cost(Decimal(last_24h), tuple(Decimal(v) for v in usual), Decimal(month), budget)


def jobs(conn: Connection) -> Jobs:
    return Jobs(load_latest_jobs(conn), load_latest_jobs(conn, completed_only=True))


def gather(engine: Engine, now: datetime, *, budget: Decimal) -> Snapshot:
    """The parts of the snapshot that come from the database. The probes are the caller's."""
    return Snapshot(
        now=now,
        lanes=_part(engine, "lanes", lanes),
        sources=_part(engine, "sources", sources),
        volume=_part(engine, "volume", lambda c: volume(c, now)),
        jobs=_part(engine, "jobs", jobs),
        cost=_part(engine, "cost", lambda c: cost(c, now, budget)),
    )
