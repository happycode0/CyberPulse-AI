"""The daily intelligence report in the database (migration 020): what it reads and where it is
kept. worker/ai/report.py writes it; worker/publish/build.py publishes the latest.

The input is the day's digest (worker/db/digest.py) with each event's own facts, and the quiet
topics: categories and Australian sectors that have a record but nothing new for a week, which
MORPHEUS used to look for by hand ("nothing on the Australian health sector in 9 days").
"""

import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import Connection, text

from worker.db.digest import load_digest

# Events the call is shown: the window's most prominent and its escalation candidates.
MAX_INPUT_EVENTS = 15
SUMMARY_MAX_CHARS = 400
WHY_MAX_CHARS = 240
# A topic is quiet after this many days with nothing new, if it had at least `MIN_RECORD`
# stories in the `RECORD_DAYS` before: a gap in coverage, not a topic that never comes up.
QUIET_DAYS = 7
RECORD_DAYS = 90
MIN_RECORD = 3
MAX_GAPS = 10
# An Australian sector counts an event only at the AU desk's relevance (worker/ai/tasks.py).
AU_SECTOR_RELEVANCE = 0.5


@dataclass(frozen=True)
class ReportInput:
    events: list[dict[str, Any]]  # each with the facts below, the call's only events
    gaps: list[dict[str, Any]]  # {id, topic, kind, quiet_days}, quietest first
    window_hours: int


def _cut(value: str | None, most: int) -> str | None:
    if value is None:
        return None
    return value if len(value) <= most else value[: most - 1] + "…"


def _event_facts(conn: Connection, event_ids: Sequence[str]) -> dict[str, dict[str, Any]]:
    rows = conn.execute(
        text(
            "select e.event_id, e.title, e.summary, e.why_it_matters, e.severity, e.status, "
            "e.au_relevance, e.au_directly_reported, e.categories, e.au_sectors, e.first_seen, "
            "array(select c.cve_id from event_cves c where c.event_id = e.event_id "
            "  order by c.cve_id) as cves, "
            "exists(select 1 from event_cves c join cves k on k.cve_id = c.cve_id "
            "  where c.event_id = e.event_id and k.kev_listed) as kev "
            "from events e where e.event_id = any(cast(:ids as text[]))"
        ),
        {"ids": list(event_ids)},
    ).mappings()
    return {
        r["event_id"]: {
            "event_id": r["event_id"],
            "title": r["title"],
            "summary": _cut(r["summary"], SUMMARY_MAX_CHARS),
            "why_it_matters": _cut(r["why_it_matters"], WHY_MAX_CHARS),
            "severity": r["severity"],
            "status": r["status"],
            "au_relevance": r["au_relevance"],
            "reported_in_australia": r["au_directly_reported"],
            "categories": list(r["categories"]),
            "au_sectors": list(r["au_sectors"]),
            "cves": list(r["cves"]),
            "kev": r["kev"],
            "first_seen": r["first_seen"].date().isoformat(),
        }
        for r in rows
    }


def _quiet(conn: Connection, *, now: datetime) -> list[dict[str, Any]]:
    since = now - timedelta(days=RECORD_DAYS)
    rows = conn.execute(
        text(
            "with seen as ("
            "  select 'category' as kind, t.topic, e.first_seen from events e, "
            "  unnest(e.categories) as t(topic) "
            "  where e.merged_into is null and e.first_seen >= :since "
            "  union all "
            "  select 'au_sector', t.topic, e.first_seen from events e, "
            "  unnest(e.au_sectors) as t(topic) "
            "  where e.merged_into is null and e.first_seen >= :since "
            "  and coalesce(e.au_relevance, 0) >= :au"
            ") "
            "select kind, topic, max(first_seen) as last_seen, count(*) as stories "
            "from seen group by kind, topic"
        ),
        {"since": since, "au": AU_SECTOR_RELEVANCE},
    ).mappings()
    gaps = []
    for r in rows:
        quiet_days = (now - r["last_seen"]).total_seconds() / 86400
        if quiet_days >= QUIET_DAYS and r["stories"] >= MIN_RECORD:
            gaps.append({
                "id": f"{r['kind']}:{r['topic']}",
                "topic": r["topic"],
                "kind": r["kind"],
                "quiet_days": round(quiet_days, 1),
            })
    gaps.sort(key=lambda g: (-g["quiet_days"], g["id"]))
    return gaps[:MAX_GAPS]


def load_report_input(conn: Connection, *, now: datetime, hours: int) -> ReportInput:
    """The events of the `hours` before `now` the report may name, most prominent first with the
    escalation candidates among them, and the quiet topics it may point to."""
    digest = load_digest(conn, now=now, hours=hours, top_in_window=True)
    ids = list(dict.fromkeys(
        e["event_id"] for e in [*digest["escalation_candidates"], *digest["top"]]
    ))
    facts = _event_facts(conn, ids)
    events = [facts[i] for i in ids if i in facts][:MAX_INPUT_EVENTS]
    return ReportInput(events=events, gaps=_quiet(conn, now=now), window_hours=hours)


def report_exists(conn: Connection, report_date: date) -> bool:
    return conn.execute(
        text("select exists(select 1 from daily_reports where report_date = :d)"),
        {"d": report_date},
    ).scalar_one()


def store_report(
    conn: Connection,
    *,
    report_date: date,
    generated_at: datetime,
    window_hours: int,
    summary: str,
    items: Sequence[dict[str, Any]],
    gaps: Sequence[dict[str, Any]],
    model: str | None,
) -> bool:
    """Keep the report for `report_date`. False when that date has one already: the first
    stands, as the digest is sent once per date."""
    row = conn.execute(
        text(
            "insert into daily_reports (report_date, generated_at, window_hours, summary, items, "
            "gaps, model) values (:d, :at, :hours, :summary, cast(:items as jsonb), "
            "cast(:gaps as jsonb), :model) on conflict (report_date) do nothing returning id"
        ),
        {"d": report_date, "at": generated_at, "hours": window_hours, "summary": summary,
         "items": json.dumps(list(items)), "gaps": json.dumps(list(gaps)), "model": model},
    ).first()
    return row is not None


def load_latest_report(conn: Connection) -> dict[str, Any] | None:
    row = conn.execute(
        text(
            "select report_date, generated_at, window_hours, summary, items, gaps, model "
            "from daily_reports order by report_date desc limit 1"
        )
    ).mappings().first()
    return dict(row) if row else None
