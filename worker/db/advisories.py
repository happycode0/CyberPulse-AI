"""OSV and GitHub advisories per CVE (migration 007): what the register said, and when we asked.

Rationed like the CVSS chain (worker/db/groundtruth.py): one request per CVE, never-asked CVEs
on the newest events first, and an answer re-checked only once it has had time to change.
"""

import json
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import Connection, text

from worker.groundtruth.osv import Advisory

RECHECK_HOURS: Mapping[str, int] = {
    # Fixed versions are added to advisories for weeks after the first record.
    "found": 72,
    # OSV converts new CVEs within a day or two; GitHub reviews later still.
    "absent": 48,
    "error": 6,
}

_RECHECK_CASE = (
    "case k.outcome "
    + " ".join(f"when '{name}' then :h_{name}" for name in RECHECK_HOURS)
    + " else 0 end"
)


def cves_due_for_advisories(conn: Connection, *, now: datetime, limit: int) -> list[str]:
    sql = (
        "select c.cve_id from cves c "
        "left join cve_advisory_checks k on k.cve_id = c.cve_id "
        "left join ("
        "  select ec.cve_id, max(e.first_seen) as newest_event from event_cves ec "
        "  join events e on e.event_id = ec.event_id group by ec.cve_id"
        ") m on m.cve_id = c.cve_id "
        "where k.checked_at is null or k.checked_at < cast(:now as timestamptz) - "
        f"make_interval(hours => {_RECHECK_CASE}) "
        "order by k.checked_at nulls first, m.newest_event desc nulls last, c.cve_id limit :limit"
    )
    params: dict[str, object] = {"now": now, "limit": limit}
    params.update({f"h_{name}": hours for name, hours in RECHECK_HOURS.items()})
    return [r[0] for r in conn.execute(text(sql), params)]


def record_advisory_check(
    conn: Connection, cve_id: str, outcome: str, detail: str | None, *, now: datetime
) -> None:
    conn.execute(
        text(
            "insert into cve_advisory_checks (cve_id, checked_at, outcome, detail) "
            "values (:cve_id, :now, :outcome, :detail) "
            "on conflict (cve_id) do update set checked_at = excluded.checked_at, "
            "outcome = excluded.outcome, detail = excluded.detail"
        ),
        {"cve_id": cve_id, "now": now, "outcome": outcome, "detail": detail},
    )


def _row(advisory: Advisory) -> dict[str, Any]:
    return {
        "source": advisory.source,
        "summary": advisory.summary,
        "severity": advisory.severity,
        "reviewed": advisory.reviewed,
        "packages": json.dumps([p.as_json() for p in advisory.packages], sort_keys=True),
        "published": advisory.published,
        "modified": advisory.modified,
        "url": advisory.url,
    }


@dataclass(frozen=True)
class AdvisoryChange:
    """What `record_advisories` changed for one CVE."""

    written: tuple[str, ...] = ()  # advisory ids inserted or updated
    # Advisory ids whose packages or fixed versions moved, which is what an event's timeline
    # cares about: a fix published is a NEW_PATCH (Stage 3).
    packages_moved: tuple[str, ...] = ()
    removed: int = 0

    @property
    def changed(self) -> bool:
        return bool(self.written or self.removed)


def record_advisories(
    conn: Connection,
    cve_id: str,
    advisories: Sequence[Advisory],
    *,
    now: datetime,
    complete: bool = False,
) -> AdvisoryChange:
    """Write what changed since the last reading.

    With `complete`, `advisories` is everything the register now says about the CVE, so a row
    it no longer names (withdrawn, or its alias dropped) is removed. A partial answer only adds.
    """
    stored = {
        r.advisory_id: r
        for r in conn.execute(
            text(
                "select advisory_id, packages, severity, reviewed, summary from cve_advisories "
                "where cve_id = :c"
            ),
            {"c": cve_id},
        )
    }
    written: list[str] = []
    moved: list[str] = []
    for advisory in advisories:
        row = _row(advisory)
        old = stored.get(advisory.id)
        new_packages = json.loads(row["packages"])
        if old is not None and (
            old.packages == new_packages
            and old.severity == row["severity"]
            and old.reviewed == row["reviewed"]
            and old.summary == row["summary"]
        ):
            continue
        conn.execute(
            text(
                "insert into cve_advisories (cve_id, advisory_id, source, summary, severity, "
                "reviewed, packages, published, modified, url, observed_at) "
                "values (:cve_id, :id, :source, :summary, :severity, :reviewed, "
                "cast(:packages as jsonb), :published, :modified, :url, :now) "
                "on conflict (cve_id, advisory_id) do update set source = excluded.source, "
                "summary = excluded.summary, severity = excluded.severity, "
                "reviewed = excluded.reviewed, packages = excluded.packages, "
                "published = excluded.published, modified = excluded.modified, "
                "url = excluded.url, observed_at = excluded.observed_at"
            ),
            {"cve_id": cve_id, "id": advisory.id, "now": now, **row},
        )
        written.append(advisory.id)
        if old is None or old.packages != new_packages:
            moved.append(advisory.id)
    removed = 0
    if complete:
        removed = conn.execute(
            text(
                "delete from cve_advisories where cve_id = :c "
                "and advisory_id <> all(cast(:ids as text[]))"
            ),
            {"c": cve_id, "ids": [a.id for a in advisories]},
        ).rowcount
    return AdvisoryChange(tuple(written), tuple(moved), removed)


def advisories_for(conn: Connection, cve_ids: Iterable[str]) -> dict[str, list[dict[str, Any]]]:
    """Published shape per CVE: GitHub advisories first, then OSV's own record."""
    ids = sorted(set(cve_ids))
    out: dict[str, list[dict[str, Any]]] = defaultdict(list)
    if not ids:
        return out
    rows = conn.execute(
        text(
            "select cve_id, advisory_id, source, severity, reviewed, packages, url "
            "from cve_advisories where cve_id = any(cast(:ids as text[])) "
            "order by cve_id, (source = 'osv'), advisory_id"
        ),
        {"ids": ids},
    )
    for r in rows:
        out[r.cve_id].append(
            {
                "id": r.advisory_id,
                "source": r.source,
                "severity": r.severity,
                "reviewed": r.reviewed,
                "packages": r.packages,
                "url": r.url,
            }
        )
    return out
