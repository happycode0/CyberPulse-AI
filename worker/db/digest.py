"""What changed in a window: event counts, the most prominent events, escalation candidates,
source-health changes, collection runs and the month's AI cost.

Two readers share it. MORPHEUS reads it from the ops API (`GET /ops/digest`), and the daily
Telegram digest (worker/notify/jobs.py) is written from it, so both describe the same day the
same way.
"""

from collections.abc import Mapping
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import Connection, text

from worker.publish.build import LIVE_MIN_PROMINENCE

TOP_EVENTS = 10
ESCALATION_LIMIT = 25
# ZION's rule (docs/wiki/stage-4b-the-crew.md): FAST-lane events this relevant to Australia.
ESCALATE_AU_RELEVANCE = 0.7
ERROR_MAX_CHARS = 300

EVENT_COLUMNS = (
    "e.event_id, e.title, e.severity, e.status, e.prominence, e.au_relevance, "
    "e.au_directly_reported, e.first_seen, e.last_material_update"
)


def usd(value: Decimal | float | None) -> float | None:
    return None if value is None else round(float(value), 6)


def truncate(message: str) -> str:
    return message if len(message) <= ERROR_MAX_CHARS else message[:ERROR_MAX_CHARS] + "..."


def month_bounds(start: datetime) -> tuple[datetime, datetime]:
    """The calendar month `start` falls in, as [first instant, first instant of the next)."""
    start = start.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    end = start.replace(year=start.year + 1, month=1) if start.month == 12 else start.replace(
        month=start.month + 1
    )
    return start, end


def event_row(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "event_id": row["event_id"],
        "title": row["title"],
        "severity": row["severity"],
        "status": row["status"],
        "prominence": row["prominence"],
        "au_relevance": row["au_relevance"],
        "au_directly_reported": row["au_directly_reported"],
        "first_seen": row["first_seen"],
        "last_material_update": row["last_material_update"],
    }


def ledger_totals(conn: Connection, start: datetime, end: datetime) -> dict[str, Any]:
    calls, cost, unknown = conn.execute(
        text(
            "select count(*), coalesce(sum(cost_usd), 0), "
            "count(*) filter (where cost_usd is null) "
            "from cost_ledger where ts >= :start and ts < :end"
        ),
        {"start": start, "end": end},
    ).one()
    return {"calls": calls, "cost_usd": usd(cost), "unknown_cost_calls": unknown}


def load_digest(conn: Connection, *, now: datetime, hours: int) -> dict[str, Any]:
    """The digest of the `hours` before `now`. Datetimes and Decimals are left as they are."""
    since = now - timedelta(hours=hours)
    params = {"since": since}
    start, end = month_bounds(now)
    counts = (
        conn.execute(
            text(
                "select "
                "count(*) filter (where first_seen >= :since and merged_into is null) "
                "as new, "
                "count(*) filter (where first_seen >= :since and merged_into is null "
                "and status = 'archived') as new_archived_on_arrival, "
                "count(*) filter (where first_seen < :since and merged_into is null "
                "and last_material_update >= :since) as updated, "
                "count(*) filter (where merged_into is not null and updated_at >= :since) "
                "as merged, "
                "count(*) filter (where merged_into is null and status <> 'archived') "
                "as standing "
                "from events"
            ),
            params,
        )
        .mappings()
        .one()
    )
    top = conn.execute(
        text(
            f"select {EVENT_COLUMNS} from events e "
            "where e.status <> 'archived' and e.merged_into is null "
            "and e.prominence > :min "
            "order by e.prominence desc, e.last_material_update desc nulls last, "
            "e.event_id limit :limit"
        ),
        {"min": LIVE_MIN_PROMINENCE, "limit": TOP_EVENTS},
    ).mappings()
    top_events = [event_row(r) for r in top]
    candidates = conn.execute(
        text(
            f"select {EVENT_COLUMNS}, array(select distinct r.lane "
            "from event_sources s join source_registry r on r.id = s.source_id "
            "where s.event_id = e.event_id order by r.lane) as lanes "
            "from events e "
            "where e.status <> 'archived' and e.merged_into is null "
            "and (e.first_seen >= :since or e.last_material_update >= :since) "
            "and (e.severity = 'critical' or e.au_relevance >= :au) "
            "order by e.prominence desc nulls last, e.event_id limit :limit"
        ),
        {**params, "au": ESCALATE_AU_RELEVANCE, "limit": ESCALATION_LIMIT},
    ).mappings()
    escalations = [
        {**event_row(r), "lanes": list(r["lanes"]), "fast_lane": "fast" in r["lanes"]}
        for r in candidates
    ]
    changes = conn.execute(
        text(
            "with latest as ("
            "  select distinct on (source_id) source_id, status, error, checked_at "
            "  from source_health order by source_id, checked_at desc, id desc"
            "), earlier as ("
            "  select distinct on (source_id) source_id, status from source_health "
            "  where checked_at < :since order by source_id, checked_at desc, id desc"
            ") "
            "select l.source_id, b.status as was, l.status as now, l.checked_at, l.error "
            "from latest l left join earlier b using (source_id) "
            "where b.status is distinct from l.status order by l.source_id"
        ),
        params,
    ).mappings()
    health_changes = [
        {**dict(r), "error": truncate(r["error"]) if r["error"] else None} for r in changes
    ]
    runs = conn.execute(
        text(
            "select lane, count(*) as runs, "
            "count(*) filter (where finished_at is null) as unfinished, "
            "coalesce(sum(new_events), 0) as new_events, "
            "coalesce(sum(updated_events), 0) as updated_events, "
            "coalesce(sum(archived_events), 0) as archived_events, "
            "coalesce(sum(cardinality(errors)), 0) as errors "
            "from runs where started_at >= :since group by lane order by lane"
        ),
        params,
    ).mappings()
    by_lane = {r["lane"]: {k: int(v) for k, v in r.items() if k != "lane"} for r in runs}
    return {
        "window": {"hours": hours, "from": since, "to": now},
        "events": dict(counts),
        "top": top_events,
        "escalation_candidates": escalations,
        "source_health_changes": health_changes,
        "runs": by_lane,
        "cost_this_month": {"month": f"{start:%Y-%m}", **ledger_totals(conn, start, end)},
    }
