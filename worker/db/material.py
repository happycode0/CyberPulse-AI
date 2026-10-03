"""Writes the material changes the registers report (worker/pipeline/material.py decides).

Like every writer here it takes the caller's connection and never commits.
"""

import logging
from collections.abc import Iterable

from sqlalchemy import Connection, text

from worker.pipeline.material import RegisterChange

logger = logging.getLogger(__name__)


def record_register_changes(conn: Connection, changes: Iterable[RegisterChange]) -> set[str]:
    """Put each change on the timeline of every standing event naming its CVE that was first
    seen before it and has no change of that kind yet, and bring the event's
    `last_material_update` up to it. Returns the events changed (their scores are stale).

    One of a kind, as for a report (`known`): CISA's "Adds ... to Catalog" notice and the
    listing it announces are one confirmation, and a second advisory's fix is not a second
    patch."""
    touched: set[str] = set()
    for c in changes:
        rows = conn.execute(
            text(
                "insert into event_timeline (event_id, ts, type, summary) "
                "select e.event_id, :at, :type, :summary from events e "
                "join event_cves ec on ec.event_id = e.event_id and ec.cve_id = :cve "
                "where e.merged_into is null and e.status <> 'archived' and e.first_seen < :at "
                "and not exists (select 1 from event_timeline t where t.event_id = e.event_id "
                "  and t.type = :type) "
                "returning event_id"
            ),
            {"cve": c.cve_id, "at": c.at, "type": c.type.value, "summary": c.summary},
        )
        ids = [r[0] for r in rows]
        if not ids:
            continue
        conn.execute(
            text(
                "update events set last_material_update = greatest(last_material_update, :at), "
                "status = case when status = 'new' then 'developing' else status end, "
                "updated_at = now() where event_id = any(cast(:ids as text[]))"
            ),
            {"at": c.at, "ids": ids},
        )
        logger.info("%s on %s: %d events", c.type.value, c.cve_id, len(ids))
        touched.update(ids)
    return touched
