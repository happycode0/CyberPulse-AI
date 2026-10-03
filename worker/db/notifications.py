"""notifications rows: one per message, claimed before it is sent (migration 011)."""

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from sqlalchemy import Connection, Engine, text

Kind = Literal[
    "critical_au_alert", "daily_digest", "developing_update", "incident", "source_discovery",
    "system_failure", "weekly_trends",
]
Outcome = Literal["sent", "failed", "withheld"]

# A failed send is tried again by later passes, up to this many attempts in all.
MAX_ATTEMPTS = 3
ERROR_MAX_CHARS = 200
# The alert rule (docs/wiki/stage-5-full-crew.md): as relevant to Australia as ZION's escalations.
ALERT_AU_RELEVANCE = 0.7
ALERT_CANDIDATES = 50


@dataclass(frozen=True)
class Claim:
    id: int
    attempt: int


def claim(
    engine: Engine,
    *,
    kind: Kind,
    key: str,
    now: datetime,
    event_id: str | None = None,
    channel: str = "telegram",
) -> Claim | None:
    """The right to send the message `key`, or None if it was sent, is being sent, was withheld
    or has failed `MAX_ATTEMPTS` times. Claiming and checking are one statement, so two passes
    can never both win."""
    with engine.begin() as conn:
        row = conn.execute(
            text(
                "insert into notifications "
                "(kind, dedupe_key, channel, event_id, status, attempts, claimed_at) "
                "values (:kind, :key, :channel, :event_id, 'sending', 1, :now) "
                "on conflict (dedupe_key) do update "
                "set status = 'sending', attempts = notifications.attempts + 1, "
                "claimed_at = excluded.claimed_at, error = null "
                "where notifications.status = 'failed' and notifications.attempts < :max "
                "returning id, attempts"
            ),
            {
                "kind": kind, "key": key, "channel": channel, "event_id": event_id, "now": now,
                "max": MAX_ATTEMPTS,
            },
        ).first()
    return Claim(row.id, row.attempts) if row else None


def settle(
    engine: Engine, claimed: Claim, outcome: Outcome, *, now: datetime, error: str | None = None
) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                "update notifications set status = :status, error = :error, "
                "sent_at = :sent_at where id = :id"
            ),
            {
                "id": claimed.id, "status": outcome,
                "sent_at": now if outcome == "sent" else None,
                "error": error[:ERROR_MAX_CHARS] if error else None,
            },
        )



@dataclass(frozen=True)
class AlertEvent:
    event_id: str
    title: str
    severity: str
    status: str
    au_relevance: float | None
    au_directly_reported: bool
    first_seen: datetime
    kev_cves: tuple[str, ...]


def load_alert_events(conn: Connection, *, since: datetime) -> list[AlertEvent]:
    """Events first seen since `since` that are critical or carry a KEV-listed CVE, and that
    matter to Australia: relevance of `ALERT_AU_RELEVANCE` or more, or reported there. Oldest
    first, so a backlog goes out in the order it arrived."""
    rows = conn.execute(
        text(
            "select e.event_id, e.title, e.severity, e.status, e.au_relevance, "
            "e.au_directly_reported, e.first_seen, "
            "array(select c.cve_id from event_cves ec join cves c on c.cve_id = ec.cve_id "
            "      where ec.event_id = e.event_id and c.kev_listed order by c.cve_id) "
            "as kev_cves "
            "from events e "
            "where e.merged_into is null and e.status <> 'archived' and e.first_seen >= :since "
            "and (e.au_relevance >= :au or e.au_directly_reported) "
            "and (e.severity = 'critical' or exists ("
            "  select 1 from event_cves ec join cves c on c.cve_id = ec.cve_id "
            "  where ec.event_id = e.event_id and c.kev_listed)) "
            "order by e.first_seen, e.event_id limit :limit"
        ),
        {"since": since, "au": ALERT_AU_RELEVANCE, "limit": ALERT_CANDIDATES},
    ).mappings()
    return [AlertEvent(**{**r, "kev_cves": tuple(r["kev_cves"])}) for r in rows]
