"""Incidents (migrations 001 and 015): what the watchdog opened and resolved, and TELETRAAN's
verdicts on the fixes for them.

The decisions are worker/watchdog/checks.py's and worker/watchdog/reconcile.py's; this module
reads and writes. Every pass applies its plan in one transaction, under a lock, so two passes
cannot both open the same incident.

The breaker: each failed verdict adds one to `fix_failures`, and at BREAKER_LIMIT the incident
needs a human. After that no verdict is taken (`halted`), so the crew cannot keep trying.
"""

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from sqlalchemy import Connection, Engine, text

from worker.watchdog.checks import SOURCE_KINDS, Finding
from worker.watchdog.reconcile import REOPEN_WITHIN, Known, plan

BREAKER_LIMIT = 3
# Verdicts one incident takes, whatever they say.
MAX_VERDICTS = 20
_LOCK = 726202606

_COLUMNS = (
    "id, kind, subject, severity, status, title, evidence, opened_at, last_seen, checks, "
    "clear_since, resolved_at, fix_failures, needs_human, reopened"
)


@dataclass(frozen=True)
class Incident:
    id: int
    kind: str
    subject: str
    severity: str
    status: str
    title: str
    evidence: dict[str, Any]
    opened_at: datetime
    last_seen: datetime | None
    checks: int
    clear_since: datetime | None
    resolved_at: datetime | None
    fix_failures: int
    needs_human: bool
    reopened: int


@dataclass(frozen=True)
class Verdict:
    ts: datetime
    verdict: str
    pr: int | None
    reasons: str | None


@dataclass(frozen=True)
class PassResult:
    opened: list[Incident] = field(default_factory=list)  # new or reopened this pass
    resolved: list[Incident] = field(default_factory=list)
    unresolved: list[Incident] = field(default_factory=list)  # after the pass


Outcome = Literal["recorded", "not_found", "resolved", "halted", "full"]


@dataclass(frozen=True)
class VerdictResult:
    outcome: Outcome
    incident: Incident | None = None
    tripped: bool = False  # this verdict tripped the breaker


def _incident(row: Any) -> Incident:
    return Incident(**dict(row))


def load_unresolved(conn: Connection) -> list[Incident]:
    rows = conn.execute(
        text(f"select {_COLUMNS} from incidents where status <> 'resolved' order by opened_at, id")
    ).mappings()
    return [_incident(r) for r in rows]


def load_incidents(
    conn: Connection, *, unresolved_only: bool, since: datetime, limit: int = 100
) -> list[Incident]:
    """Unresolved incidents, or with `unresolved_only` false also those resolved since `since`;
    newest first."""
    rows = conn.execute(
        text(
            f"select {_COLUMNS} from incidents where status <> 'resolved' "
            "or (not :unresolved_only and resolved_at >= :since) "
            "order by opened_at desc, id desc limit :limit"
        ),
        {"unresolved_only": unresolved_only, "since": since, "limit": limit},
    ).mappings()
    return [_incident(r) for r in rows]


def load_incident(conn: Connection, incident_id: int) -> Incident | None:
    row = (
        conn.execute(text(f"select {_COLUMNS} from incidents where id = :id"), {"id": incident_id})
        .mappings()
        .first()
    )
    return _incident(row) if row else None


def load_verdicts(conn: Connection, ids: Sequence[int]) -> dict[int, list[Verdict]]:
    """Each incident's verdicts, oldest first."""
    out: dict[int, list[Verdict]] = {i: [] for i in ids}
    if not ids:
        return out
    rows = conn.execute(
        text(
            "select incident_id, ts, verdict, pr, reasons from incident_verdicts "
            "where incident_id = any(cast(:ids as bigint[])) order by ts, id"
        ),
        {"ids": list(ids)},
    )
    for r in rows:
        out[r.incident_id].append(Verdict(r.ts, r.verdict, r.pr, r.reasons))
    return out


def _values(f: Finding, now: datetime) -> dict[str, Any]:
    return {
        "kind": f.kind,
        "subject": f.subject,
        "severity": f.severity,
        "title": f.title,
        "evidence": json.dumps(f.evidence),
        "now": now,
    }


def record_pass(
    engine: Engine, findings: Sequence[Finding], covered: frozenset[str], now: datetime
) -> PassResult:
    """Apply one pass's findings to the incidents."""
    with engine.begin() as conn:
        conn.execute(text("select pg_advisory_xact_lock(:k)"), {"k": _LOCK})
        rows = conn.execute(
            text(
                "select id, kind, subject, status = 'resolved' as resolved, clear_since, "
                "resolved_at from incidents where status <> 'resolved' or resolved_at >= :since"
            ),
            {"since": now - REOPEN_WITHIN},
        )
        known = [
            Known(r.id, r.kind, r.subject, r.resolved, r.clear_since, r.resolved_at) for r in rows
        ]
        p = plan(known, findings, covered, now)
        opened: list[Incident] = []
        for f in p.insert:
            row = (
                conn.execute(
                    text(
                        "insert into incidents (kind, subject, severity, status, source_id, "
                        "title, opened_at, last_seen, checks, evidence) values (:kind, :subject, "
                        ":severity, 'open', (select id from source_registry where id = :source), "
                        ":title, :now, :now, 1, cast(:evidence as jsonb)) "
                        f"returning {_COLUMNS}"
                    ),
                    {**_values(f, now), "source": f.subject if f.kind in SOURCE_KINDS else None},
                )
                .mappings()
                .one()
            )
            opened.append(_incident(row))
        for incident_id, f in p.reopen:
            row = (
                conn.execute(
                    text(
                        "update incidents set status = 'open', resolved_at = null, "
                        "clear_since = null, reopened = reopened + 1, last_seen = :now, "
                        "checks = 1, severity = :severity, title = :title, "
                        f"evidence = cast(:evidence as jsonb) where id = :id returning {_COLUMNS}"
                    ),
                    {**_values(f, now), "id": incident_id},
                )
                .mappings()
                .one()
            )
            opened.append(_incident(row))
        if p.update:
            conn.execute(
                text(
                    "update incidents set last_seen = :now, checks = checks + 1, "
                    "clear_since = null, severity = :severity, title = :title, "
                    "evidence = cast(:evidence as jsonb) where id = :id"
                ),
                [{**_values(f, now), "id": incident_id} for incident_id, f in p.update],
            )
        if p.clearing:
            conn.execute(
                text(
                    "update incidents set clear_since = :now "
                    "where id = any(cast(:ids as bigint[])) and clear_since is null"
                ),
                {"now": now, "ids": p.clearing},
            )
        resolved: list[Incident] = []
        if p.resolve:
            rows = conn.execute(
                text(
                    "update incidents set status = 'resolved', resolved_at = :now "
                    f"where id = any(cast(:ids as bigint[])) returning {_COLUMNS}"
                ),
                {"now": now, "ids": p.resolve},
            ).mappings()
            resolved = [_incident(r) for r in rows]
        return PassResult(opened, resolved, load_unresolved(conn))


def record_verdict(
    engine: Engine,
    incident_id: int,
    *,
    verdict: Literal["pass", "fail"],
    pr: int,
    reasons: str,
    now: datetime,
) -> VerdictResult:
    with engine.begin() as conn:
        row = (
            conn.execute(
                text(f"select {_COLUMNS} from incidents where id = :id for update"),
                {"id": incident_id},
            )
            .mappings()
            .first()
        )
        if row is None:
            return VerdictResult("not_found")
        incident = _incident(row)
        if incident.status == "resolved":
            return VerdictResult("resolved", incident)
        if incident.needs_human:
            return VerdictResult("halted", incident)
        taken = conn.execute(
            text("select count(*) from incident_verdicts where incident_id = :id"),
            {"id": incident_id},
        ).scalar_one()
        if taken >= MAX_VERDICTS:
            return VerdictResult("full", incident)
        conn.execute(
            text(
                "insert into incident_verdicts (incident_id, ts, verdict, pr, reasons) "
                "values (:id, :ts, :verdict, :pr, :reasons)"
            ),
            {"id": incident_id, "ts": now, "verdict": verdict, "pr": pr, "reasons": reasons},
        )
        if verdict == "fail":
            row = (
                conn.execute(
                    text(
                        "update incidents set fix_failures = fix_failures + 1, "
                        "needs_human = fix_failures + 1 >= :limit "
                        f"where id = :id returning {_COLUMNS}"
                    ),
                    {"id": incident_id, "limit": BREAKER_LIMIT},
                )
                .mappings()
                .one()
            )
            after = _incident(row)
            return VerdictResult("recorded", after, tripped=after.needs_human)
        return VerdictResult("recorded", incident)
