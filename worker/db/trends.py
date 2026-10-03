"""Reads for the trend engine (worker/pipeline/trends.py): reports, the events they are on, KEV
additions and when collection began. Read-only.
"""

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy import Connection, text

from worker.pipeline.trends import Report, Story


@dataclass(frozen=True)
class TrendInputs:
    reports: list[Report]
    stories: list[Story]
    kev_added: dict[date, int]
    collecting_since: datetime | None


def load_trend_inputs(conn: Connection, *, since: datetime) -> TrendInputs:
    """Everything dated from `since`: independent reports on standing or archived events (never
    on a merged one: its reports moved to the event it joined), the events first seen or
    reported on since then, and the CVEs CISA added to KEV each day."""
    reports = [
        Report(event_id=r[0], headline=r[1] or "", at=r[2])
        for r in conn.execute(
            text(
                "select es.event_id, coalesce(es.title, e.title), "
                "coalesce(es.published, es.fetched_at) as at "
                "from event_sources es join events e on e.event_id = es.event_id "
                "where e.merged_into is null and es.independent "
                "and coalesce(es.published, es.fetched_at) >= :since order by at, es.id"
            ),
            {"since": since},
        )
    ]
    reported = sorted({r.event_id for r in reports})

    cves: dict[str, list[str]] = defaultdict(list)
    kev: dict[str, set[str]] = defaultdict(set)
    stories_rows = conn.execute(
        text(
            "select event_id, first_seen, prominence, severity in ('critical', 'high'), "
            "au_directly_reported or coalesce(au_relevance, 0) >= 0.5 from events "
            "where merged_into is null "
            "and (first_seen >= :since or event_id = any(cast(:reported as text[]))) "
            "order by event_id"
        ),
        {"since": since, "reported": reported},
    ).all()
    for event_id, cve_id, listed in conn.execute(
        text(
            "select ec.event_id, ec.cve_id, c.kev_listed from event_cves ec "
            "join cves c on c.cve_id = ec.cve_id "
            "where ec.event_id = any(cast(:ids as text[])) order by ec.event_id, ec.cve_id"
        ),
        {"ids": [r[0] for r in stories_rows]},
    ):
        cves[event_id].append(cve_id)
        if listed:
            kev[event_id].add(cve_id)
    stories = [
        Story(
            event_id=r[0],
            first_seen=r[1],
            prominence=r[2],
            critical_or_high=bool(r[3]),
            au=bool(r[4]),
            cves=tuple(cves.get(r[0], ())),
            kev_cves=frozenset(kev.get(r[0], ())),
        )
        for r in stories_rows
    ]

    kev_added = {
        r[0]: r[1]
        for r in conn.execute(
            text(
                "select kev_date_added, count(*) from cves "
                "where kev_listed and kev_date_added >= cast(:since as date) group by 1"
            ),
            {"since": since},
        )
    }
    collecting_since = conn.execute(text("select min(started_at) from runs")).scalar_one()
    return TrendInputs(reports, stories, kev_added, collecting_since)
