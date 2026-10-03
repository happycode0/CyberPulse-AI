"""Lineage's reads and writes (worker/pipeline/lineage.py decides; this records it).

Like every writer here it takes the caller's connection and never commits.
"""

from collections import defaultdict
from datetime import datetime
from typing import Any

from sqlalchemy import Connection, text

from worker.pipeline.lineage import Publishers, Report, assign, last_independent_confirmation


def _rows(conn: Connection, since: datetime) -> dict[str, list[Any]]:
    rows: dict[str, list[Any]] = defaultdict(list)
    for r in conn.execute(
        text(
            "select s.id, s.event_id, s.source_id, s.title, "
            "coalesce(s.published, s.fetched_at, s.created_at) at, s.lineage_id, s.independent, "
            "e.last_independent_confirmation lic "
            "from event_sources s join events e on e.event_id = s.event_id "
            "where e.merged_into is null and e.last_seen >= :since order by s.event_id, s.id"
        ),
        {"since": since},
    ).mappings():
        rows[r["event_id"]].append(r)
    return rows


def _report(r: Any) -> Report:
    return Report(r["id"], r["source_id"], r["title"], r["at"])


def load_reports(conn: Connection, *, since: datetime) -> dict[str, list[Report]]:
    """Every report on the events still standing on their own seen since `since`, by event."""
    return {e: [_report(r) for r in rs] for e, rs in _rows(conn, since).items()}


def refresh_lineage(conn: Connection, publishers: Publishers, *, since: datetime) -> set[str]:
    """Settle the lineage and independence of every report on the events still standing on
    their own seen since `since`, and each event's last independent confirmation. Returns the
    events where anything changed (their scores are stale)."""
    sources: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []
    changed: set[str] = set()
    for event_id, rows in _rows(conn, since).items():
        reports = [_report(r) for r in rows]
        assigned = assign(reports, publishers)
        for r in rows:
            a = assigned[r["id"]]
            if (r["lineage_id"], r["independent"]) != (a.lineage, a.independent):
                sources.append({"id": r["id"], "l": a.lineage, "ind": a.independent})
                changed.add(event_id)
        lic = last_independent_confirmation(reports, assigned)
        if lic != rows[0]["lic"]:
            events.append({"e": event_id, "lic": lic})
            changed.add(event_id)

    if sources:
        conn.execute(
            text(
                "insert into source_lineage (lineage_id, description) values (:l, :d) "
                "on conflict (lineage_id) do nothing"
            ),
            [{"l": line, "d": publishers.labels.get(line)} for line in {s["l"] for s in sources}],
        )
        conn.execute(
            text("update event_sources set lineage_id = :l, independent = :ind where id = :id"),
            sources,
        )
    if events:
        conn.execute(
            text(
                "update events set last_independent_confirmation = :lic, updated_at = now() "
                "where event_id = :e"
            ),
            events,
        )
    return changed
