"""Archiving: an event that has faded leaves the live set, and is never deleted (PLAN.md §5).

An event is archived once its prominence has decayed below `archive.prominence_below`, or its
last material update is more than `archive.idle_days` old (config/scoring.yaml). An archived
event keeps everything and stays on the site's day pages (worker/publish/build.py), but is no
longer rescored, enriched or merged. A material change brings it back as a developing event:
a report (worker/db/ingest.py `apply_update`) or a register (worker/db/material.py). An
unscored event is archived on age alone: an unknown prominence is not a low one.

Like every writer here it takes the caller's connection and never commits.
"""

from datetime import datetime, timedelta

from sqlalchemy import Connection, text

from worker.pipeline.score import ArchiveRule


def archive_faded(conn: Connection, rule: ArchiveRule, *, now: datetime) -> list[str]:
    """Archive every standing event that has faded. Returns their ids."""
    rows = conn.execute(
        text(
            "update events set status = 'archived', updated_at = now() "
            "where status <> 'archived' and merged_into is null and ("
            "prominence < :below "
            "or coalesce(last_material_update, first_seen) < :idle_since) "
            "returning event_id"
        ),
        {"below": rule.prominence_below, "idle_since": now - timedelta(days=rule.idle_days)},
    )
    return sorted(r[0] for r in rows)
