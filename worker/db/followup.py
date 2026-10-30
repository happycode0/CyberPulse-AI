"""Event status and DECKARD's follow-up queue in the database. worker/pipeline/status.py and
worker/pipeline/followup.py decide; this reads the record and writes the result.

- `settle_statuses`, every collection run: each standing event's status from its record.
- `schedule_followups`, straight after: one open `check` task per followed-up event, due on its
  cadence (config/followup.yaml), backed off while its checks find nothing, and a
  `final_summary` task for each resolved event that should have one. An open task whose event
  no longer qualifies is cancelled.
- `load_due`, `count_accepted`, `load_new_since` and `submit_report`: what `GET` and
  `POST /ops/followup` read and write.
- `close_quiet_checks` and `count_due`: what the hourly routine wake (worker/notify/routines.py)
  does before it decides whether to wake DECKARD at all.

Like every writer here it takes the caller's connection and never commits.
"""

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

from sqlalchemy import Connection, Row, text

from worker.db.ingest import lock_ingest
from worker.models import EventStatus, MaterialChange, SourceRef, TimelineEntry
from worker.pipeline.followup import (
    EventContext,
    Report,
    ReportRejected,
    check_report,
)
from worker.pipeline.status import (
    AGENT_SOURCE,
    FIX_TYPES,
    NOT_CHANGES,
    FollowupRule,
    StatusFacts,
    StatusRule,
    Transition,
    transitions,
)

# A task refused this many times is given up ('failed'); a check is opened again on its cadence.
MAX_REPORT_ATTEMPTS = 3
# The report the worker records itself for a due check with nothing new since the last one:
# the no_change DECKARD's skill sent for it, without a read. `by` keeps it out of DECKARD's
# daily cap, which bounds what the agent spends; it counts as a quiet check for the back-off.
WORKER_NO_CHANGE = {"outcome": "no_change", "recorded": [], "refused": [], "by": "worker"}
ERROR_MAX_CHARS = 300
_OPEN = "status in ('pending', 'in_progress')"
_SEVERITY_RANK = (
    "case e.severity when 'critical' then 0 when 'high' then 1 when 'medium' then 2 "
    "when 'unknown' then 3 else 4 end"
)


# --- Status ------------------------------------------------------------------------------------


def load_status_facts(
    conn: Connection, event_ids: Sequence[str] | None = None
) -> list[StatusFacts]:
    """The facts for every standing event (not archived, not merged), or for `event_ids`."""
    only = "and e.event_id = any(cast(:ids as text[])) " if event_ids is not None else ""
    rows = conn.execute(
        text(
            "select e.event_id, e.status, e.first_seen, e.last_material_update, "
            "e.last_independent_confirmation is not null as confirmed, "
            "max(t.ts) filter (where t.type = any(cast(:fix as text[]))) as fixed_at, "
            "max(t.ts) filter (where not t.type = any(cast(:fix as text[])) and ("
            "  not t.type = any(cast(:not_changes as text[])) "
            "  or (t.type = 'NEW_FACT' and t.sources[1] = :agent))) as changed_at "
            "from events e left join event_timeline t on t.event_id = e.event_id "
            f"where e.merged_into is null and e.status <> 'archived' {only}"
            "group by e.event_id order by e.event_id"
        ),
        {
            "fix": sorted(FIX_TYPES),
            "not_changes": sorted(NOT_CHANGES),
            "agent": AGENT_SOURCE,
            "ids": list(event_ids or []),
        },
    )
    return [
        StatusFacts(
            event_id=r.event_id,
            status=EventStatus(r.status),
            first_seen=r.first_seen,
            last_material_update=r.last_material_update,
            confirmed=r.confirmed,
            fixed_at=r.fixed_at,
            changed_at=r.changed_at,
        )
        for r in rows
    ]


def settle_statuses(
    conn: Connection,
    rule: StatusRule,
    *,
    now: datetime,
    event_ids: Sequence[str] | None = None,
) -> list[Transition]:
    """Give each standing event (or each of `event_ids`) the status its record says. Returns
    the events moved."""
    moved = transitions(load_status_facts(conn, event_ids), rule, now=now)
    for t in moved:
        conn.execute(
            text(
                "update events set status = :now, updated_at = now() "
                "where event_id = :id and status = :was and merged_into is null"
            ),
            {"id": t.event_id, "was": t.was.value, "now": t.now.value},
        )
    return moved


# --- The queue ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class Scheduled:
    opened: list[str] = field(default_factory=list)  # event ids
    cancelled: int = 0
    brought_forward: int = 0


_CADENCE = (
    "cadence (status, severity, hours) as (select * from unnest("
    "cast(:st as text[]), cast(:sv as text[]), cast(:hr as double precision[])))"
)
# A material change, as load_status_facts reads one off the timeline.
_MATERIAL = (
    "(not m.type = any(cast(:not_changes as text[])) "
    "or (m.type = 'NEW_FACT' and m.sources[1] = :agent))"
)
# When each followed-up event's next check is due: its cadence after the last check ended,
# doubled for each no_change report since the event's last material change, at most
# 2^max_backoff_doublings times; the first check, now. The last material change is the later of
# `last_material_update` and when the newest material entry was written: a register dates its
# entry when the change happened, which can be before checks that could not yet have seen it.
_NEXT_CHECK = (
    "next_check (event_id, due_at) as ("
    "  select e.event_id, coalesce(p.ended + c.hours * power(cast(2 as double precision), "
    "    least(p.quiet_checks, cast(:doublings as integer))) * interval '1 hour', :now) "
    "  from events e join cadence c on c.status = e.status and c.severity = e.severity "
    "  cross join lateral (select greatest(coalesce(e.last_material_update, e.first_seen), "
    "    max(coalesce(m.created_at, m.ts))) as since "
    f"    from event_timeline m where m.event_id = e.event_id and {_MATERIAL}) r "
    "  cross join lateral (select max(d.updated_at) as ended, count(*) filter ("
    "    where d.status = 'done' and d.payload ->> 'outcome' = 'no_change' "
    "    and d.updated_at > r.since) as quiet_checks "
    "    from followup_tasks d where d.event_id = e.event_id and d.kind = 'check' "
    "    and d.status in ('done', 'failed')) p "
    "  where e.merged_into is null)"
)


def _cadence_params(rule: FollowupRule) -> dict[str, list]:
    pairs = rule.cadence()
    return {
        "st": [s.value for s, _, _ in pairs],
        "sv": [v.value for _, v, _ in pairs],
        "hr": [h for _, _, h in pairs],
    }


def schedule_followups(conn: Connection, rule: FollowupRule, *, now: datetime) -> Scheduled:
    """Open the tasks the events now need and cancel the ones they no longer do. A waiting check
    that is due sooner now (a material change ended its back-off, or its event moved to a faster
    cadence) is brought forward."""
    params = {
        **_cadence_params(rule),
        "now": now,
        "final": [s.value for s in rule.final_summary],
        "doublings": rule.max_backoff_doublings,
        "not_changes": sorted(NOT_CHANGES),
        "agent": AGENT_SOURCE,
    }
    cancelled = conn.execute(
        text(
            f"with {_CADENCE} "
            "update followup_tasks t set status = 'cancelled', updated_at = :now "
            f"from events e where e.event_id = t.event_id and t.{_OPEN} and ("
            "  e.merged_into is not null "
            "  or (t.kind = 'check' and not exists (select 1 from cadence c "
            "      where c.status = e.status and c.severity = e.severity)) "
            "  or (t.kind = 'final_summary' and (e.status <> 'resolved' "
            "      or e.resolution is not null))) "
            "returning t.id"
        ),
        params,
    ).all()
    sooner = conn.execute(
        text(
            f"with {_CADENCE}, {_NEXT_CHECK} "
            "update followup_tasks t set due_at = n.due_at, updated_at = :now "
            "from next_check n where n.event_id = t.event_id and t.kind = 'check' "
            "and t.status = 'pending' and n.due_at < t.due_at "
            "returning t.id"
        ),
        params,
    ).all()
    checks = conn.execute(
        text(
            f"with {_CADENCE}, {_NEXT_CHECK} "
            "insert into followup_tasks (event_id, kind, due_at, created_at, updated_at) "
            "select n.event_id, 'check', n.due_at, :now, :now from next_check n "
            f"on conflict (event_id, kind) where {_OPEN} do nothing "
            "returning event_id"
        ),
        params,
    ).all()
    finals = conn.execute(
        text(
            "insert into followup_tasks (event_id, kind, due_at, created_at, updated_at) "
            "select e.event_id, 'final_summary', :now, :now, :now from events e "
            "where e.merged_into is null and e.status = 'resolved' and e.resolution is null "
            "and e.severity = any(cast(:final as text[])) "
            "and not exists (select 1 from followup_tasks d where d.event_id = e.event_id "
            "  and d.kind = 'final_summary' and d.status in ('done', 'failed')) "
            f"on conflict (event_id, kind) where {_OPEN} do nothing "
            "returning event_id"
        ),
        params,
    ).all()
    return Scheduled(sorted(r[0] for r in [*checks, *finals]), len(cancelled), len(sooner))


@dataclass(frozen=True)
class DueTask:
    task_id: int
    kind: str
    event_id: str
    due_at: datetime
    attempts: int
    last_error: str | None
    last_check_at: datetime | None = None  # when the event's previous check ended


def load_due(conn: Connection, *, now: datetime, limit: int) -> list[DueTask]:
    """The pending tasks that are due, most important first: by severity, then Australian
    relevance, then how long they have waited."""
    rows = conn.execute(
        text(
            "select t.id, t.kind, t.event_id, t.due_at, t.attempts, t.last_error, "
            "(select max(d.updated_at) from followup_tasks d where d.event_id = t.event_id "
            "  and d.kind = 'check' and d.status in ('done', 'failed')) as last_check_at "
            "from followup_tasks t join events e on e.event_id = t.event_id "
            "where t.status = 'pending' and t.due_at <= :now and e.merged_into is null "
            f"order by {_SEVERITY_RANK}, coalesce(e.au_relevance, 0) desc, t.due_at, t.id "
            "limit :limit"
        ),
        {"now": now, "limit": limit},
    )
    return [
        DueTask(r.id, r.kind, r.event_id, r.due_at, r.attempts, r.last_error, r.last_check_at)
        for r in rows
    ]


def count_accepted(conn: Connection, *, since: datetime) -> int:
    """How many of DECKARD's reports were accepted (their tasks done) after `since`. GET
    /ops/followup hands out no more once a day's worth were. The no_change the worker records
    itself is not one of them: it spent nothing."""
    return conn.execute(
        text(
            "select count(*) from followup_tasks where status = 'done' and updated_at > :since "
            "and coalesce(payload ->> 'by', '') <> 'worker'"
        ),
        {"since": since},
    ).scalar_one()


def count_due(conn: Connection, *, now: datetime) -> int:
    """How many pending tasks are due: the ones `load_due` would hand out, uncapped."""
    return conn.execute(
        text(
            "select count(*) from followup_tasks t join events e on e.event_id = t.event_id "
            "where t.status = 'pending' and t.due_at <= :now and e.merged_into is null"
        ),
        {"now": now},
    ).scalar_one()


def close_quiet_checks(conn: Connection, *, now: datetime) -> list[str]:
    """Record no_change on every due check whose event has gained nothing since its last check:
    no source, no timeline entry and no material update (`nothing_new` in GET /ops/followup).
    A first check, or a final summary, always has something to look at, so neither is closed.
    Returns the events whose checks were closed. Under the ingest lock, so no collection adds a
    source to an event while its check is being closed as quiet."""
    lock_ingest(conn)
    rows = conn.execute(
        text(
            "with due as ("
            "  select t.id, t.event_id, (select max(d.updated_at) from followup_tasks d "
            "    where d.event_id = t.event_id and d.kind = 'check' "
            "    and d.status in ('done', 'failed')) as ended "
            "  from followup_tasks t "
            "  where t.status = 'pending' and t.kind = 'check' and t.due_at <= :now"
            "), quiet as ("
            "  select d.id from due d join events e on e.event_id = d.event_id "
            "  where d.ended is not null and e.merged_into is null "
            "  and coalesce(e.last_material_update, e.first_seen) <= d.ended "
            "  and not exists (select 1 from event_sources s "
            "    where s.event_id = d.event_id and s.created_at > d.ended) "
            "  and not exists (select 1 from event_timeline m "
            "    where m.event_id = d.event_id and coalesce(m.created_at, m.ts) > d.ended)"
            ") "
            "update followup_tasks t set status = 'done', attempts = attempts + 1, "
            "last_error = null, payload = cast(:payload as jsonb), updated_at = :now "
            "from quiet q where t.id = q.id "
            "returning t.event_id"
        ),
        {"now": now, "payload": json.dumps(WORKER_NO_CHANGE)},
    ).all()
    return sorted(r.event_id for r in rows)


@dataclass(frozen=True)
class NewSince:
    """What the worker added to an event after its previous check ended: how many sources and
    timeline entries, and the newest few of each, newest first."""

    since: datetime
    sources: int = 0
    timeline: int = 0
    newest_sources: tuple[SourceRef, ...] = ()
    newest_timeline: tuple[TimelineEntry, ...] = ()


_CHECKED = (
    "checked (event_id, ended) as (select * from unnest("
    "cast(:ids as text[]), cast(:ended as timestamptz[])))"
)


def load_new_since(
    conn: Connection, tasks: Sequence[DueTask], *, most: int
) -> dict[int, NewSince]:
    """For each check task whose event has been checked before, what was added to the event
    since, with at most `most` of each kind; keyed by task id. A source counts from when it was
    attached, and a timeline entry from when it was written (its date, for one from before
    migration 012), so a register's entry dated last week but found today is new."""
    checks = [t for t in tasks if t.kind == "check" and t.last_check_at is not None]
    if not checks:
        return {}
    params = {
        "ids": [t.event_id for t in checks],
        "ended": [t.last_check_at for t in checks],
        "most": most,
    }
    sources = conn.execute(
        text(
            f"with {_CHECKED} select * from ("
            "  select s.event_id, s.source_id, s.url, s.published, s.evidence_class, "
            "  count(*) over (partition by s.event_id) as added, row_number() over ("
            "    partition by s.event_id order by s.created_at desc, s.id desc) as n "
            "  from event_sources s join checked c on c.event_id = s.event_id "
            "  where s.created_at > c.ended) a "
            "where n <= :most order by event_id, n"
        ),
        params,
    ).all()
    timeline = conn.execute(
        text(
            f"with {_CHECKED} select * from ("
            "  select t.event_id, t.ts, t.type, t.summary, t.sources, "
            "  count(*) over (partition by t.event_id) as added, row_number() over ("
            "    partition by t.event_id order by coalesce(t.created_at, t.ts) desc, t.id desc"
            "  ) as n "
            "  from event_timeline t join checked c on c.event_id = t.event_id "
            "  where coalesce(t.created_at, t.ts) > c.ended) a "
            "where n <= :most order by event_id, n"
        ),
        params,
    ).all()
    out = {}
    for task in checks:
        mine = [r for r in sources if r.event_id == task.event_id]
        entries = [r for r in timeline if r.event_id == task.event_id]
        out[task.task_id] = NewSince(
            since=task.last_check_at,
            sources=mine[0].added if mine else 0,
            timeline=entries[0].added if entries else 0,
            newest_sources=tuple(
                SourceRef(source_id=r.source_id, url=r.url, published=r.published,
                          evidence_class=r.evidence_class)
                for r in mine
            ),
            newest_timeline=tuple(
                TimelineEntry(timestamp=r.ts, type=r.type, summary=r.summary,
                              sources=list(r.sources or []))
                for r in entries
            ),
        )
    return out


def load_queue_counts(conn: Connection, *, now: datetime, overdue_after: float) -> dict:
    """How many tasks are due, waiting and overdue (due more than `overdue_after` hours ago),
    and how many standing events are in each status."""
    queue = conn.execute(
        text(
            "select count(*) filter (where due_at <= :now) as due, "
            "count(*) filter (where due_at > :now) as waiting, "
            "count(*) filter (where due_at <= :now - :hours * interval '1 hour') as overdue "
            "from followup_tasks where status = 'pending'"
        ),
        {"now": now, "hours": overdue_after},
    ).one()
    statuses = conn.execute(
        text(
            "select status, count(*) from events where merged_into is null "
            "and status <> 'archived' group by status"
        )
    ).all()
    return {
        "due": queue.due,
        "waiting": queue.waiting,
        "overdue": queue.overdue,
        "statuses": {s.value: 0 for s in EventStatus if s is not EventStatus.ARCHIVED}
        | {status: n for status, n in statuses},
    }


# --- Reports -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class Submitted:
    """What became of a report. `result` says which, and so which HTTP status the API sends."""

    result: Literal["recorded", "rejected", "not_found", "closed"]
    message: str = ""
    task_id: int | None = None
    event_id: str | None = None
    report: Report | None = None
    attempts: int = 0
    task_status: str | None = None
    transition: Transition | None = None


def _context(conn: Connection, event_id: str, first_seen: datetime) -> EventContext:
    timeline = conn.execute(
        text("select type, summary, sources from event_timeline where event_id = :e"),
        {"e": event_id},
    ).all()
    cves = conn.execute(
        text("select cve_id from event_cves where event_id = :e"), {"e": event_id}
    ).all()
    return EventContext(
        cves=frozenset(r[0].upper() for r in cves),
        types=frozenset(MaterialChange(r.type) for r in timeline),
        summaries=frozenset(r.summary.casefold() for r in timeline),
        agent_entries=sum(1 for r in timeline if r.sources and r.sources[0] == AGENT_SOURCE),
        first_seen=first_seen.date(),
    )


def _gone(kind: str, event: Row) -> str | None:
    """Why a task of `kind` no longer applies to `event`, or None if it still does."""
    if event.merged_into:
        return f"the event was merged into {event.merged_into}"
    if event.status == EventStatus.ARCHIVED:
        return "the event is archived"
    if kind == "final_summary" and event.status != EventStatus.RESOLVED:
        return "the event is no longer resolved"
    if kind == "final_summary" and event.resolution is not None:
        return "the event has its final summary"
    return None


def _close(conn: Connection, task_id: int, now: datetime) -> None:
    conn.execute(
        text("update followup_tasks set status = 'cancelled', updated_at = :now where id = :id"),
        {"id": task_id, "now": now},
    )


def _refuse(conn: Connection, task_id: int, reason: str, now: datetime) -> tuple[int, str]:
    row = conn.execute(
        text(
            "update followup_tasks set attempts = attempts + 1, last_error = :reason, "
            "status = case when attempts + 1 >= :most then 'failed' else status end, "
            "updated_at = :now where id = :id returning attempts, status"
        ),
        {"id": task_id, "reason": reason[:ERROR_MAX_CHARS], "most": MAX_REPORT_ATTEMPTS,
         "now": now},
    ).one()
    return row.attempts, row.status


def _payload(report: Report) -> str:
    return json.dumps({
        "outcome": report.outcome,
        "recorded": [
            {"type": c.type.value, "url": c.url,
             "date": c.happened_on.isoformat() if c.happened_on else None}
            for c in report.changes
        ],
        "refused": [{"index": r.index, "reason": r.reason} for r in report.refused],
    })


def submit_report(
    conn: Connection, task_id: int, body: object, *, rule: StatusRule, now: datetime
) -> Submitted:
    """Check DECKARD's report on task `task_id` and store what passes, in the caller's
    transaction. Under the ingest lock, so no collection writes the event meanwhile."""
    lock_ingest(conn)
    task = conn.execute(
        text(
            "select id, event_id, kind, status, attempts from followup_tasks "
            "where id = :id for update"
        ),
        {"id": task_id},
    ).one_or_none()
    if task is None:
        return Submitted("not_found", f"no follow-up task {task_id}")
    base = {"task_id": task.id, "event_id": task.event_id, "attempts": task.attempts}
    if task.status != "pending":
        return Submitted("closed", f"task {task.id} is {task.status}", task_status=task.status,
                         **base)

    event = conn.execute(
        text(
            "select status, merged_into, resolution, first_seen from events "
            "where event_id = :e for update"
        ),
        {"e": task.event_id},
    ).one()
    if gone := _gone(task.kind, event):
        _close(conn, task.id, now)
        return Submitted("closed", f"{gone}; task {task.id} is cancelled",
                         task_status="cancelled", **base)

    try:
        report = check_report(
            body, kind=task.kind, event=_context(conn, task.event_id, event.first_seen),
            today=now.date(),
        )
        if report.outcome == "changed" and not report.changes:
            raise ReportRejected(
                "no change passed the checks: "
                + "; ".join(f"change {r.index}: {r.reason}" for r in report.refused)
            )
    except ReportRejected as exc:
        attempts, status = _refuse(conn, task.id, str(exc), now)
        return Submitted("rejected", str(exc), task.id, task.event_id, attempts=attempts,
                         task_status=status)

    for c in report.changes:
        conn.execute(
            text(
                "insert into event_timeline (event_id, ts, type, summary, sources, created_at) "
                "values (:e, :now, :type, :summary, cast(:sources as text[]), :now)"
            ),
            {"e": task.event_id, "now": now, "type": c.type.value, "summary": c.summary,
             "sources": [AGENT_SOURCE, c.url]},
        )
    if report.changes:
        conn.execute(
            text(
                "update events set last_material_update = "
                "greatest(coalesce(last_material_update, first_seen), :now), updated_at = now() "
                "where event_id = :e"
            ),
            {"e": task.event_id, "now": now},
        )
    if report.summary is not None:
        conn.execute(
            text("update events set resolution = :s, updated_at = now() where event_id = :e"),
            {"e": task.event_id, "s": report.summary},
        )
    done = conn.execute(
        text(
            "update followup_tasks set status = 'done', attempts = attempts + 1, "
            "last_error = null, payload = cast(:payload as jsonb), updated_at = :now "
            "where id = :id returning attempts"
        ),
        {"id": task.id, "now": now, "payload": _payload(report)},
    ).one()
    moved = settle_statuses(conn, rule, now=now, event_ids=[task.event_id])
    return Submitted(
        "recorded", "", task.id, task.event_id, report, done.attempts, "done",
        moved[0] if moved else None,
    )
