"""Enrichment's reads and writes (migration 006). The calls themselves are worker/ai/enrich.py's.

Each task's answer is written in its own transaction together with its `event_enrichment` row,
so a task that succeeded stays done even when a later one fails. An event leaves
`pending_enrichment` once every task that applies to it is done at the current version
(`maybe_complete`).

Model output reaches the database only through the parsers in worker/ai/tasks.py, which have
already checked it. `last_error` holds our own words about a failure, never the output.
"""

import json
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import Connection, text

from worker.ai.tasks import Brief, SeverityJudgment, SourceFacts, Subject, TaskName, Triage
from worker.db.events import load_events
from worker.models import EvidenceClass, Severity, SeveritySource

# How long a task waits after its first, second and third failed pass in a row. After the
# fourth it gives up, and the event stays `pending_enrichment` until the version changes.
BACKOFF: tuple[timedelta, ...] = (timedelta(hours=1), timedelta(hours=6), timedelta(hours=24))

_LAST_ERROR_CHARS = 300

# The severity sources a judgment may replace. Anything else is an official score (§2.5).
_REPLACEABLE = (SeveritySource.UNKNOWN.value, SeveritySource.AI_ESTIMATE.value)


@dataclass(frozen=True)
class TaskState:
    version: str
    status: str
    next_attempt_at: datetime | None


def due_event_ids(
    conn: Connection, *, version: str, now: datetime, floor: float, limit: int
) -> list[str]:
    """Pending events worth enriching, most prominent first.

    Only events the site would publish (prominence over `floor`), so no money goes on events
    nobody sees. An event with a task in backoff, or one that has given up, waits as a whole.
    """
    rows = conn.execute(
        text(
            "select e.event_id from events e "
            "where e.pending_enrichment and e.status <> 'archived' and e.prominence > :floor "
            "and not exists (select 1 from event_enrichment x where x.event_id = e.event_id "
            "and x.version = :version and x.status = 'failed' "
            "and (x.next_attempt_at is null or x.next_attempt_at > :now)) "
            "order by e.prominence desc, e.event_id limit :limit"
        ),
        {"version": version, "now": now, "floor": floor, "limit": limit},
    )
    return [r[0] for r in rows]


def task_states(conn: Connection, event_ids: Sequence[str]) -> dict[str, dict[str, TaskState]]:
    out: dict[str, dict[str, TaskState]] = defaultdict(dict)
    rows = conn.execute(
        text(
            "select event_id, task, version, status, next_attempt_at from event_enrichment "
            "where event_id = any(cast(:ids as text[]))"
        ),
        {"ids": list(event_ids)},
    )
    for r in rows:
        out[r.event_id][r.task] = TaskState(r.version, r.status, r.next_attempt_at)
    return out


def load_subjects(conn: Connection, event_ids: Sequence[str]) -> list[Subject]:
    """The events with what enrichment may show a model: the feed's own text, the other
    sources' headlines, and who reported it. In `event_ids` order."""
    if not event_ids:
        return []
    ids = {"ids": list(event_ids)}
    source_text = dict(
        conn.execute(
            text(
                "select event_id, coalesce(source_summary, summary) from events "
                "where event_id = any(cast(:ids as text[]))"
            ),
            ids,
        ).all()
    )
    titles: dict[str, list[str]] = defaultdict(list)
    sources: dict[str, list[SourceFacts]] = defaultdict(list)
    rows = conn.execute(
        text(
            "select es.event_id, es.title, r.name, r.region, r.category from event_sources es "
            "join source_registry r on r.id = es.source_id "
            "where es.event_id = any(cast(:ids as text[])) "
            "order by es.event_id, es.published nulls last, es.id"
        ),
        ids,
    )
    for r in rows:
        if r.title:
            titles[r.event_id].append(r.title)
        sources[r.event_id].append(SourceFacts(r.name, r.region, r.category))

    out = []
    for event in load_events(conn, event_ids):
        own = event.title.casefold()
        headlines = dict.fromkeys(t for t in titles[event.event_id] if t.casefold() != own)
        out.append(
            Subject(
                event=event,
                source_text=source_text[event.event_id],
                headlines=tuple(headlines),
                sources=tuple(dict.fromkeys(sources[event.event_id])),
            )
        )
    return out


# ─── Writing answers ──────────────────────────────────────────────────────────────────────────────


def apply_triage(conn: Connection, subject: Subject, t: Triage) -> None:
    """Categories put the model's first (the radar reads `categories[0]`) and keep each source's
    registry category behind them, which the site's sections also read."""
    categories = list(dict.fromkeys([*t.categories, *(s.category for s in subject.sources)]))
    conn.execute(
        text(
            "update events set domains = cast(:domains as text[]), "
            "categories = cast(:categories as text[]), ai_subdomain = :ai_subdomain, "
            "entity_actors = cast(:actors as text[]), "
            "entity_organisations = cast(:organisations as text[]), "
            "entity_products = cast(:products as text[]), "
            "entity_countries = cast(:countries as text[]), "
            "entity_industries = cast(:industries as text[]), tags = cast(:tags as text[]), "
            "updated_at = now() where event_id = :event_id"
        ),
        {
            "event_id": subject.event.event_id,
            "domains": list(t.domains),
            "categories": categories,
            "ai_subdomain": t.ai_subdomain.value if t.ai_subdomain else None,
            "actors": list(t.actors),
            "organisations": list(t.organisations),
            "products": list(t.products),
            "countries": list(t.countries),
            "industries": list(t.industries),
            "tags": list(t.tags),
        },
    )


def apply_brief(conn: Connection, subject: Subject, b: Brief) -> None:
    """The summaries are published as written. The AU reading is kept as the model's
    (migration 008): the AU engine (worker/pipeline/au.py) publishes AU relevance at the next
    rescore, from the record's facts, with this reading able to lift it but never lower it."""
    conn.execute(
        text(
            "update events set summary = :summary, why_it_matters = :why, "
            "au_model_relevance = :relevance, au_model_reasons = cast(:reasons as text[]), "
            "au_model_sectors = cast(:sectors as text[]), updated_at = now() "
            "where event_id = :event_id"
        ),
        {
            "event_id": subject.event.event_id,
            "summary": b.summary,
            "why": b.why_it_matters,
            "relevance": b.au_relevance,
            "reasons": list(b.au_reasons),
            "sectors": list(b.au_sectors),
        },
    )


def apply_severity(
    conn: Connection, subject: Subject, j: SeverityJudgment, *, model: str | None, version: str
) -> bool:
    """Publish the judgment as an `ai_estimate` if it is firm enough. Returns whether it was.

    The update re-checks the stored source, so an official score that arrived while the model
    was thinking is never overwritten. A judgment too weak to publish takes back an earlier
    estimate rather than leaving it standing. Either way the judgment is kept as evidence.
    """
    event_id = subject.event.event_id
    ai = SeveritySource.AI_ESTIMATE.value
    if j.usable:
        result = conn.execute(
            text(
                "update events set severity = :severity, severity_source = :ai, "
                "updated_at = now() where event_id = :event_id "
                "and severity_source = any(cast(:replaceable as text[]))"
            ),
            {
                "event_id": event_id,
                "severity": j.severity.value,
                "ai": ai,
                "replaceable": list(_REPLACEABLE),
            },
        )
        applied = result.rowcount == 1
    else:
        conn.execute(
            text(
                "update events set severity = :unknown, severity_source = :unknown, "
                "updated_at = now() where event_id = :event_id and severity_source = :ai"
            ),
            {"event_id": event_id, "unknown": Severity.UNKNOWN.value, "ai": ai},
        )
        applied = False
    conn.execute(
        text(
            "insert into evidence (event_id, kind, evidence_class, detail) "
            "values (:event_id, 'ai_severity', :cls, cast(:detail as jsonb))"
        ),
        {
            "event_id": event_id,
            "cls": EvidenceClass.AI_INFERENCE.value,
            "detail": json.dumps(
                {
                    "severity": j.severity.value,
                    "confidence": j.confidence,
                    "rationale": j.rationale,
                    "model": model,
                    "enrichment_version": version,
                    "applied": applied,
                }
            ),
        },
    )
    return applied


# ─── Bookkeeping ──────────────────────────────────────────────────────────────────────────────────


def mark_done(
    conn: Connection,
    event_id: str,
    task: TaskName,
    *,
    version: str,
    model: str | None,
    now: datetime,
) -> None:
    conn.execute(
        text(
            "insert into event_enrichment (event_id, task, version, status, model, failures, "
            "next_attempt_at, last_error, done_at, updated_at) "
            "values (:event_id, :task, :version, 'done', :model, 0, null, null, :now, :now) "
            "on conflict (event_id, task) do update set version = excluded.version, "
            "status = 'done', model = excluded.model, failures = 0, next_attempt_at = null, "
            "last_error = null, done_at = excluded.done_at, updated_at = excluded.updated_at"
        ),
        {"event_id": event_id, "task": task.value, "version": version, "model": model, "now": now},
    )


def mark_failed(
    conn: Connection,
    event_id: str,
    task: TaskName,
    *,
    version: str,
    model: str | None,
    reason: str,
    now: datetime,
) -> datetime | None:
    """Record a failed pass. Returns when the task may run again, or None if it has given up."""
    row = conn.execute(
        text(
            "select version, status, failures from event_enrichment "
            "where event_id = :event_id and task = :task for update"
        ),
        {"event_id": event_id, "task": task.value},
    ).first()
    in_a_row = row is not None and row.version == version and row.status == "failed"
    failures = row.failures + 1 if in_a_row else 1
    retry_at = now + BACKOFF[failures - 1] if failures <= len(BACKOFF) else None
    conn.execute(
        text(
            "insert into event_enrichment (event_id, task, version, status, model, failures, "
            "next_attempt_at, last_error, done_at, updated_at) "
            "values (:event_id, :task, :version, 'failed', :model, :failures, :retry_at, "
            ":error, null, :now) "
            "on conflict (event_id, task) do update set version = excluded.version, "
            "status = 'failed', model = excluded.model, failures = excluded.failures, "
            "next_attempt_at = excluded.next_attempt_at, last_error = excluded.last_error, "
            "done_at = null, updated_at = excluded.updated_at"
        ),
        {
            "event_id": event_id,
            "task": task.value,
            "version": version,
            "model": model,
            "failures": failures,
            "retry_at": retry_at,
            "error": reason[:_LAST_ERROR_CHARS],
            "now": now,
        },
    )
    return retry_at


def maybe_complete(conn: Connection, event_id: str, *, version: str) -> bool:
    """Clear `pending_enrichment` if every task that applies is done at `version`.

    Severity applies only while no official score exists, which is re-read here rather than
    trusted from when the pass began.
    """
    done = (
        "exists (select 1 from event_enrichment x where x.event_id = e.event_id "
        "and x.task = '{task}' and x.status = 'done' and x.version = :version)"
    )
    result = conn.execute(
        text(
            "update events e set pending_enrichment = false, enrichment_version = :version, "
            "updated_at = now() where e.event_id = :event_id and e.pending_enrichment "
            f"and {done.format(task=TaskName.TRIAGE)} and {done.format(task=TaskName.BRIEF)} "
            "and (e.severity_source <> all(cast(:replaceable as text[])) "
            f"or {done.format(task=TaskName.SEVERITY)})"
        ),
        {"event_id": event_id, "version": version, "replaceable": list(_REPLACEABLE)},
    )
    return result.rowcount == 1
