"""Build the public JSON files from the database, or write nothing.

`build_all` is fail-closed and all-or-nothing:

1. every payload is built in memory, without the events worker/publish/hold.py holds back
   (no sources, a directory listing, off the beat), which no file carries;
2. every payload is secret-scanned, then schema-validated, then serialised and the
   serialised text is secret-scanned again (that is what would reach disk);
3. only if all of that passes is anything written: files are staged inside `out_dir`, then
   moved into place with `os.replace`, and any failure while moving restores the previous
   files.

A failure in steps 1-2 leaves `out_dir` exactly as it was, including not creating it.
"""

import json
import logging
import os
import shutil
import tempfile
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from pydantic import ValidationError
from sqlalchemy import Connection

from worker.db.crew import load_crew_activity, load_job_activity, load_pipeline_ai
from worker.db.discovery import (
    Candidate,
    active_discovered,
    load_at_the_gate,
    load_gate_counts,
    load_settled,
)
from worker.db.events import load_event_dates, load_live_events
from worker.db.jobs import JobRun, load_latest_jobs
from worker.db.merge import merged_redirects
from worker.db.runs import (
    load_lane_activity,
    load_last_completed_collection,
    load_latest_finished_run,
    load_unfinished_runs,
)
from worker.db.sources import (
    DEFAULT_HISTORY_LIMIT,
    RegistryRow,
    load_corroboration,
    load_health_history,
    load_lifecycle_states,
    load_registry_rows,
)
from worker.db.trends import load_trend_inputs
from worker.discovery.gate import DiscoveryConfig, GateConfig
from worker.models import (
    Beat,
    Event,
    EventStatus,
    HealthStatus,
    LifecycleState,
    PublisherConfig,
    RunSummary,
    SourceConfig,
    SourceHealth,
)
from worker.pipeline.assemble import strip_feed_tail
from worker.pipeline.health import consecutive_failures
from worker.pipeline.importance import Voice, with_importance
from worker.pipeline.reputation import CORROBORATION_WINDOW, Reputation, assess_reputation
from worker.pipeline.trends import ACTIVITY_DAYS, compute_trends, load_trends_config
from worker.publish.claims import with_fact_claims
from worker.publish.hold import holds
from worker.publish.validate import (
    ValidationFailure,
    scan_for_secrets,
    scan_text_for_secrets,
    validate_payload,
)
from worker.schedule import LANE_JOBS, public_schedule
from worker.sources.registry import REGISTRY_PATH, load_publishers, load_registry
from worker.version import PIPELINE_VERSION, SCHEMA_VERSION, SCORING_VERSION

logger = logging.getLogger(__name__)

LIVE_MIN_PROMINENCE = 0.05
LIVE_LIMIT = 500
# Below every possible prominence (0..1), so this selects every scored event.
ALL_SCORED = -1.0
HISTORY_EVENT_LIMIT = 10_000
# The checks a source's uptime is over. The discovery gate rates the sources it may retire to
# make room with the same (worker/db/discovery.py `retire_weak`).
HEALTH_HISTORY_LIMIT = DEFAULT_HISTORY_LIMIT
# index.json's `merged` map covers events first seen this recently: a link to one of them that
# was shared before it was merged still finds the story (site/assets/hud.js, `findEvent`).
MERGED_REDIRECT_WINDOW = timedelta(days=90)
HEALTH_ERROR_MAX_CHARS = 300
# source-health.json's `pipeline`: the finds still at SERAPH's gate, furthest through first.
PIPELINE_LIMIT = 25
# source-health.json's `discovery.recent`: finds activated or retired this recently.
SETTLED_WINDOW = timedelta(days=14)
SETTLED_LIMIT = 20
# A run started this recently and not finished is still going; an older one crashed (runs.py).
RUN_IN_PROGRESS_WINDOW = timedelta(hours=2)
# An `attention` reason quotes at most this much of a check's error or a registry note.
ATTENTION_DETAIL_MAX_CHARS = 160

STAGING_PREFIX = ".publish-"
_SEVERITIES = ("critical", "high", "medium", "low", "unknown")

# (host, query parameter) pairs known, by human review, to carry opaque UI state rather than
# a credential, so the secret scan trips on them for no reason (INC-22). This is a manual
# allowlist, never a shortcut like "strip it if it would scan as a secret" — that would blind
# the scan to a real credential sharing the same shape. Add an entry only after a human has
# looked at an actual flagged value and confirmed it is not a secret.
_SAFE_QUERY_PARAMS: frozenset[tuple[str, str]] = frozenset({
    # Cisco's advisory search encodes its filter state here; the page loads fine without it.
    ("sec.cloudapps.cisco.com", "vs_f"),
})


def _iso(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("naive datetime cannot be published")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _iso_or_none(value: datetime | None) -> str | None:
    return None if value is None else _iso(value)


def _event_counts(events: list[dict[str, Any]]) -> dict[str, int]:
    by_severity = Counter(e["severity"] for e in events)
    return {
        "events": len(events),
        **{s: by_severity.get(s, 0) for s in _SEVERITIES},
        "directly_reported_in_au": sum(1 for e in events if e["au"]["directly_reported_in_au"]),
        "pending_enrichment": sum(1 for e in events if e["pending_enrichment"]),
    }


def _sanitize_source_url(url: str) -> str:
    """Drop a query parameter listed in `_SAFE_QUERY_PARAMS`; otherwise return `url` as-is.

    Ground truth keeps the original URL in the database; only this published copy is
    trimmed, and only for the exact, human-reviewed (host, parameter) pairs on the
    allowlist. The secret scan itself is never consulted here: a param's value is not
    evidence of its own safety.
    """
    split = urlsplit(url)
    params = parse_qsl(split.query, keep_blank_values=True)
    if not params:
        return url
    kept = [(key, value) for key, value in params if (split.hostname, key) not in _SAFE_QUERY_PARAMS]
    if len(kept) == len(params):
        return url
    return urlunsplit(split._replace(query=urlencode(kept)))


def _load_events(conn: Connection) -> list[Event]:
    try:
        return load_live_events(conn, min_prominence=ALL_SCORED, limit=HISTORY_EVENT_LIMIT)
    except ValidationError as exc:
        # One unpublishable row blocks the whole run: half a picture is worse than a
        # stale one. Only locations and messages are reported, never the input values.
        problems = "; ".join(
            f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}"
            for e in exc.errors(include_input=False, include_url=False, include_context=False)[:5]
        )
        raise ValidationFailure(f"stored event failed model validation: {problems}") from exc


def _health_record(h: SourceHealth) -> dict[str, Any]:
    error = h.error
    if error is not None and len(error) > HEALTH_ERROR_MAX_CHARS:
        error = error[:HEALTH_ERROR_MAX_CHARS] + "..."
    return {
        "checked_at": _iso(h.checked_at),
        "status": h.status.value,
        "error": error,
        "newest_item_age_days": h.newest_item_age_days,
        "items_fetched": h.items_fetched,
        "duration_ms": h.duration_ms,
    }


@dataclass(frozen=True)
class _Source:
    """A registered source as the publisher sees it: its registry row, its entry in
    config/sources.yaml (None for a source only the database has, which the discovery gate
    added), its recent checks and its reputation."""

    row: RegistryRow
    config: SourceConfig | None
    publisher: PublisherConfig | None
    history: list[SourceHealth]
    state: LifecycleState
    reputation: Reputation

    @property
    def label(self) -> str:
        """Who a reader knows it as: the organisation behind it, or its own name."""
        return self.publisher.name if self.publisher else self.row.name


def _load_sources(conn: Connection, now: datetime) -> list[_Source]:
    """Every registered source, with the description and standing config/sources.yaml gives
    it. The file is read at publish time, so a change there shows at the next publish."""
    configs = {s.id: s for s in load_registry(REGISTRY_PATH)}
    publishers = load_publishers(REGISTRY_PATH)
    states = load_lifecycle_states(conn)
    corroboration = load_corroboration(conn, since=now - CORROBORATION_WINDOW)
    sources = []
    for row in load_registry_rows(conn):
        history = load_health_history(conn, row.id, HEALTH_HISTORY_LIMIT)
        config = configs.get(row.id)
        sources.append(
            _Source(
                row=row,
                config=config,
                publisher=publishers.get(config.publisher or "") if config else None,
                history=history,
                state=states.get(row.id)
                or (LifecycleState.ACTIVE if row.enabled else LifecycleState.DISCOVERED),
                reputation=assess_reputation(
                    config.standing if config else None, history, corroboration.get(row.id)
                ),
            )
        )
    return sources


def _voices(sources: list[_Source]) -> dict[str, Voice]:
    return {s.row.id: Voice(s.label, s.reputation.standing, s.reputation.score) for s in sources}


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def _detail(text: str | None) -> str:
    """`text` on one line and at most `ATTENTION_DETAIL_MAX_CHARS` long."""
    text = " ".join((text or "").split())
    if len(text) > ATTENTION_DETAIL_MAX_CHARS:
        text = text[: ATTENTION_DETAIL_MAX_CHARS - 3].rstrip() + "..."
    return text


_FAILED = {
    HealthStatus.ERROR: "failed",
    HealthStatus.TIMEOUT: "timed out",
    HealthStatus.EMPTY: "came back empty",
}
_UNWELL = (LifecycleState.DEGRADED, LifecycleState.BROKEN)
_FREQUENCY = {"real_time": "around the clock"}


def _quiet_for(check: SourceHealth, row: RegistryRow) -> str:
    age = check.newest_item_age_days
    said = f"nothing new in {age:.1f} days" if age is not None else "nothing new lately"
    if row.expected_frequency:
        frequency = _FREQUENCY.get(row.expected_frequency, row.expected_frequency)
        said += f" (it usually publishes {frequency})"
    return said


def _attention(source: _Source) -> dict[str, str] | None:
    """What the Sources page should flag about a source, if anything.

    `fix`: its last check failed, or it is degraded and still failing. `watch`: it is stale, or
    degraded and healthy again. `blocked`: config/sources.yaml has it switched off, and its note
    says why; nothing moves it on until someone edits the registry. `coming`: still a find on
    trial. A retired source needs nothing.
    """
    row, state = source.row, source.state
    latest = source.history[-1] if source.history else None
    if state is LifecycleState.RETIRED:
        return None
    if not row.enabled:
        # The registry's note on a disabled source says why (config/sources.yaml).
        note = _detail(source.config.notes if source.config else None)
        if note and not note.endswith("."):
            note += "."
        return {"kind": "blocked", "reason": f"Not collected. {note}".rstrip()}
    if state in (LifecycleState.DISCOVERED, LifecycleState.CANDIDATE):
        return {"kind": "coming", "reason": "A new find, on trial before it is collected."}
    if latest is not None and latest.status in _FAILED:
        error = _detail(latest.error)
        said = f"The last check {_FAILED[latest.status]}"
        return {"kind": "fix", "reason": f"{said}: {error}" if error else f"{said}."}
    failing = consecutive_failures(source.history)
    if state in _UNWELL and failing:
        said = f"Degraded: {_plural(failing, 'unhealthy check')} in a row"
        if latest is not None and latest.status is HealthStatus.STALE:
            said += f", {_quiet_for(latest, row)}"
        return {"kind": "fix", "reason": said + "."}
    if latest is not None and latest.status is HealthStatus.STALE:
        return {"kind": "watch", "reason": f"Stale: {_quiet_for(latest, row)}."}
    if state in _UNWELL:
        return {"kind": "watch", "reason": "Recovering: healthy again after failed checks."}
    return None


def _source_entry(source: _Source) -> dict[str, Any]:
    row, config, history = source.row, source.config, source.history
    return {
        "source_id": row.id,
        "name": row.name,
        "description": config.description if config else None,
        "publisher": source.publisher.name if source.publisher else None,
        "url": row.url,
        "region": row.region,
        "category": row.category,
        # The registry leaves a cyber source's beat unset, and a source the gate added is cyber.
        "beat": (config.beat if config and config.beat else Beat.CYBER).value,
        "lane": row.lane,
        "priority": row.priority,
        "expected_frequency": row.expected_frequency,
        "enabled": row.enabled,
        "standing": source.reputation.standing.value,
        "reputation": source.reputation.public(),
        "lifecycle_state": source.state.value,
        "attention": _attention(source),
        "latest": _health_record(history[-1]) if history else None,
        "history": [_health_record(h) for h in history],
    }


_FOUND_BY = {"search": "Found by the nightly search", "tachikoma": "Proposed by TACHIKOMA"}


def _gate_reason(c: Candidate, gate: GateConfig) -> str:
    """Where a find stands, in the worker's own words. Never the finder's `reason`, which a
    model wrote from pages on the open web."""
    who = _FOUND_BY.get(c.found_by, "Found")
    if c.state == "discovered" and c.feed_url:
        return f"{who}; feed found, queued until a testing slot frees."
    if c.state == "discovered":
        return f"{who}; looking for its feed."
    if c.state == "testing" and c.passes >= gate.probes_to_activate:
        return (f"{who}; passed the gate, waiting for one of the {gate.max_active} "
                "discovered-source slots.")
    if c.state == "testing":
        return f"{who}; {_plural(c.passes, 'healthy probe')} in a row so far."
    if c.failures:
        return f"{who}; feed found, {_plural(c.failures, 'failed probe')} in a row."
    return f"{who}; feed found, waiting for its first probe."


def _pipeline(conn: Connection, gate: GateConfig) -> list[dict[str, Any]]:
    """The finds still at the gate: names and domains only, never a feed URL or evidence."""
    return [
        {
            "name": c.name,
            "host": c.host,
            "state": c.state,
            "found_at": _iso(c.created_at),
            "reason": _gate_reason(c, gate),
        }
        for c in load_at_the_gate(conn, limit=PIPELINE_LIMIT)
    ]


# What a gate or search pass's `detail` (migration 018) may put on the site: counts and the ids of
# sources it activated or retired, which source-health.json lists anyway.
_GATE_FIELDS = ("probes", "healthy", "rejected", "activated", "retired", "pruned", "waiting",
                "promoted")
_SEARCH_FIELDS = ("searches", "new_hosts", "skipped")


def _last_pass(run: JobRun | None, fields: tuple[str, ...]) -> dict[str, Any] | None:
    """The newest pass of a discovery job: when it finished and whether it completed, then what
    it found when it did. A pass that raised says so and no more; never its note."""
    if run is None:
        return None
    out: dict[str, Any] = {"finished_at": _iso(run.finished_at), "completed": run.completed}
    if run.completed and run.detail:
        out.update({k: run.detail[k] for k in fields if k in run.detail})
    return out


def _discovery_summary(
    conn: Connection, gate: GateConfig, jobs: dict[str, JobRun], *, shown: int, now: datetime
) -> dict[str, Any]:
    """Where discovery stands, for the Sources page: the slots taken, what waits at the gate
    and why, and what the last gate pass and search did."""
    counts = load_gate_counts(conn, gate=gate)
    return {
        "active": active_discovered(conn),
        "max_active": gate.max_active,
        "ready_waiting": counts.ready_waiting,
        "testing": counts.testing,
        "queued": counts.queued,
        "homes_waiting": counts.homes_waiting,
        "at_the_gate": counts.at_the_gate,
        # How many of `at_the_gate` the `pipeline` list shows (PIPELINE_LIMIT).
        "shown": shown,
        "rejected_total": counts.rejected,
        "retired_total": counts.retired,
        "probes_to_activate": gate.probes_to_activate,
        "max_open": gate.max_open,
        "last_gate": _last_pass(jobs.get("source-gate"), _GATE_FIELDS),
        "last_search": _last_pass(jobs.get("discovery"), _SEARCH_FIELDS),
        "recent": [
            {
                "name": c.name,
                "host": c.host,
                "state": c.state,
                "at": _iso(c.updated_at),
                "found_by": c.found_by,
            }
            for c in load_settled(conn, since=now - SETTLED_WINDOW, limit=SETTLED_LIMIT)
        ],
    }


def _source_health_payload(
    conn: Connection,
    generated_at: str,
    sources: list[_Source] | None = None,
    jobs: dict[str, JobRun] | None = None,
) -> dict[str, Any]:
    now = datetime.fromisoformat(generated_at.replace("Z", "+00:00"))
    if sources is None:
        sources = _load_sources(conn, now)
    if jobs is None:
        jobs = load_latest_jobs(conn)
    gate = DiscoveryConfig.load().gate
    entries = [_source_entry(s) for s in sources]
    counts = Counter(e["latest"]["status"] if e["latest"] else "no_data" for e in entries)
    pipeline = _pipeline(conn, gate)
    return {
        "generated_at": generated_at,
        "counts": dict(counts),
        "sources": entries,
        "pipeline": pipeline,
        "discovery": _discovery_summary(conn, gate, jobs, shown=len(pipeline), now=now),
    }


# Each scheduled job that leaves a job_runs row, by the name the row carries (worker/db/jobs.py).
# The lanes leave `runs` rows instead; the Telegram jobs leave notifications, not passes.
_JOB_ROWS = {
    "groundtruth-sync": "groundtruth",
    "ai-enrichment": "enrichment",
    "source-discovery": "discovery",
    "source-gate": "source-gate",
    "model-scan": "model-scan",
    "model-gauntlet": "model-gauntlet",
    "watchdog": "watchdog",
}
# Not scheduled: they follow each run, and the site is only as fresh as they are.
_AFTER_EACH_RUN = (("publish", "Publish"), ("push", "Push"))


def _job_health(
    job_id: str,
    label: str,
    newest: JobRun | None,
    completed: JobRun | None,
    *,
    recorded: bool,
) -> dict[str, Any]:
    return {
        "job": job_id,
        "label": label,
        "recorded": recorded,
        "running": False,
        "last_run_at": _iso_or_none(newest.finished_at if newest else None),
        "last_success_at": _iso_or_none(completed.finished_at if completed else None),
        "ok": None if newest is None else newest.completed,
    }


def _lane_health(
    job_id: str,
    label: str,
    started: datetime | None,
    finished: datetime | None,
    *,
    now: datetime,
) -> dict[str, Any]:
    """A lane's newest run either finished, is still going, or crashed (it never finished)."""
    unfinished = started is not None and (finished is None or started > finished)
    running = unfinished and started >= now - RUN_IN_PROGRESS_WINDOW
    if started is None:
        ok = None
    elif running:
        ok = finished is not None
    else:
        ok = not unfinished
    return {
        "job": job_id,
        "label": label,
        "recorded": True,
        "running": running,
        "last_run_at": _iso_or_none(started),
        "last_success_at": _iso_or_none(finished),
        "ok": ok,
    }


def _jobs_payload(
    conn: Connection, jobs: dict[str, JobRun], *, now: datetime
) -> list[dict[str, Any]]:
    """How each job last went: one row per scheduled job, then the publish and the push."""
    completed = load_latest_jobs(conn, completed_only=True)
    lanes = load_lane_activity(conn)
    lane_of = {job.id: lane.value for lane, job in LANE_JOBS.items()}
    rows = []
    for entry in public_schedule():
        job_id, label = entry["job"], entry["label"]
        if job_id in lane_of:
            started, finished = lanes.get(lane_of[job_id], (None, None))
            rows.append(_lane_health(job_id, label, started, finished, now=now))
            continue
        name = _JOB_ROWS.get(job_id)
        rows.append(
            _job_health(job_id, label, jobs.get(name) if name else None,
                        completed.get(name) if name else None, recorded=name is not None)
        )
    for name, label in _AFTER_EACH_RUN:
        rows.append(_job_health(name, label, jobs.get(name), completed.get(name), recorded=True))
    return rows


def _ai_payload(
    conn: Connection,
    jobs: dict[str, JobRun],
    live: list[dict[str, Any]],
    *,
    budget_usd: Decimal | None,
    now: datetime,
) -> dict[str, Any]:
    """Where the worker's AI layer stands: whose models it calls, under what monthly budget and
    in which mode, when it last called one, and how many published stories still wait for it.
    The mode is what the newest enrichment pass recorded, when it recorded one."""
    month_start = now.astimezone(UTC).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    ai = load_pipeline_ai(conn, since=month_start)
    enrichment = jobs.get("enrichment")
    mode = (enrichment.detail or {}).get("mode") if enrichment else None
    return {
        "provider": ai.provider,
        "monthly_budget_usd": None if budget_usd is None else float(budget_usd),
        "mode": mode if isinstance(mode, str) else None,
        "calls_this_month": ai.calls,
        "cost_this_month_usd": round(float(ai.cost_usd), 4),
        "last_call_at": _iso_or_none(ai.last_call_at),
        "last_pass_at": _iso_or_none(enrichment.finished_at if enrichment else None),
        "events": len(live),
        "awaiting": sum(1 for e in live if e["pending_enrichment"]),
    }


def _run_record(run: RunSummary) -> dict[str, Any]:
    return {
        "run_id": run.run_id,
        "lane": run.lane.value,
        "started_at": _iso(run.started_at),
        "finished_at": _iso_or_none(run.finished_at),
        "sources_ok": run.sources_ok,
        "sources_failed": run.sources_failed,
        "sources_stale": run.sources_stale,
        "items_fetched": run.items_fetched,
        "new_events": run.new_events,
        "updated_events": run.updated_events,
        "duplicates": run.duplicates,
        "archived_events": run.archived_events,
        "error_count": len(run.errors),
    }


def _system_status_payload(
    conn: Connection,
    generated_at: str,
    *,
    published: int,
    total: int,
    held: int = 0,
    live: list[dict[str, Any]] | None = None,
    jobs: dict[str, JobRun] | None = None,
    ai_budget_usd: Decimal | None = None,
) -> dict[str, Any]:
    now = datetime.fromisoformat(generated_at.replace("Z", "+00:00"))
    if jobs is None:
        jobs = load_latest_jobs(conn)
    # The run that finished last, not the newest started: the FAST and NORMAL lanes start
    # together, and the one still going has nothing to show (28 Oct 2026, "0 sources checked").
    run = load_latest_finished_run(conn)
    registry = load_registry_rows(conn)
    return {
        "generated_at": generated_at,
        "last_completed_collection": _iso_or_none(load_last_completed_collection(conn)),
        "pipeline_version": PIPELINE_VERSION,
        "schema_version": SCHEMA_VERSION,
        "scoring_version": SCORING_VERSION,
        "last_run": None if run is None else _run_record(run),
        "runs_in_progress": [
            {"run_id": r.run_id, "lane": r.lane.value, "started_at": _iso(r.started_at)}
            for r in load_unfinished_runs(conn, since=now - RUN_IN_PROGRESS_WINDOW)
        ],
        "counts": {
            "events_published": published,
            "events_total": total,
            # Left out of every public file (worker/publish/hold.py), not counted in the total.
            "events_held": held,
            "sources_total": len(registry),
            "sources_enabled": sum(1 for r in registry if r.enabled),
        },
        # What runs when, and whether code or a model does it (worker/schedule.py).
        "schedule": public_schedule(),
        "jobs": _jobs_payload(conn, jobs, now=now),
        "ai": _ai_payload(conn, jobs, live or [], budget_usd=ai_budget_usd, now=now),
    }


def _trends_payload(conn: Connection, generated_at: str, now: datetime) -> dict[str, Any]:
    """Counted from the stored reports (worker/pipeline/trends.py), over the activity chart's
    days, which take in both velocity windows."""
    first_day = now.astimezone(UTC).date() - timedelta(days=ACTIVITY_DAYS - 1)
    inputs = load_trend_inputs(conn, since=datetime.combine(first_day, time(), tzinfo=UTC))
    trends = compute_trends(
        inputs.reports,
        inputs.stories,
        inputs.kev_added,
        load_trends_config(),
        now=now,
        collecting_since=inputs.collecting_since,
    )
    return {"generated_at": generated_at, "pipeline_version": PIPELINE_VERSION, **trends}


# The crew's AI jobs, which run in the worker since Paperclip was retired on 2026-10-31: the
# callsign each keeps from its agent, and the cost-ledger stage its calls are recorded under.
CREW_AI_JOBS: tuple[tuple[str, str], ...] = (
    ("morpheus", "report.daily"),
    ("deckard", "followup.report"),
    ("teletraan", "incident.diagnosis"),
)


def _crew_payload(
    conn: Connection, generated_at: str, now: datetime, jobs: dict[str, JobRun] | None = None
) -> dict[str, Any]:
    """The crew's jobs this month (worker/db/crew.py): SERAPH's in code, and the three AI jobs
    from the cost ledger, in that order.

    SERAPH is doing its job by writing this file, so its last run is now, and it is active
    unless the newest ground-truth pass or the newest push failed: the site keeps the last data
    it was sent. It spends no tokens. An AI job that has no call in the ledger is `not_run`:
    nothing is claimed for it. The enrichment calls are `pipeline_ai`.
    """
    if jobs is None:
        jobs = load_latest_jobs(conn)
    push = jobs.get("push")
    push_failing = push is not None and not push.completed
    month_start = now.astimezone(UTC).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    seraph = load_crew_activity(conn, since=month_start)["seraph"]
    rows = [
        {
            "callsign": "SERAPH",
            "kind": "code",
            "stage": None,
            "status": "degraded" if seraph.failing or push_failing else "active",
            "tasks_this_month": seraph.tasks,
            "items_this_month": seraph.items,
            "calls_this_month": None,
            "cost_usd": 0.0,
            "last_run_at": generated_at,
            "model": None,
            "provider": None,
        }
    ]
    activity = load_job_activity(conn, since=month_start, stages=dict(CREW_AI_JOBS))
    for callsign, stage in CREW_AI_JOBS:
        job = activity[callsign]
        rows.append(
            {
                "callsign": callsign.upper(),
                "kind": "ai",
                "stage": stage,
                "status": "active" if job.last_at is not None else "not_run",
                "tasks_this_month": None,
                "items_this_month": None,
                "calls_this_month": job.calls,
                "cost_usd": round(float(job.cost_usd), 4),
                "last_run_at": _iso_or_none(job.last_at),
                "model": job.model,
                "provider": job.provider,
            }
        )
    ai = load_pipeline_ai(conn, since=month_start)
    return {
        "generated_at": generated_at,
        "month": month_start.strftime("%Y-%m"),
        "jobs": rows,
        "pipeline_ai": {
            "calls": ai.calls,
            "cost_usd": round(float(ai.cost_usd), 4),
            "last_call_at": _iso_or_none(ai.last_call_at),
            "provider": ai.provider,
        },
    }


def build_payloads(
    conn: Connection, *, now: datetime, ai_budget_usd: Decimal | None = None
) -> dict[str, tuple[str, dict[str, Any]]]:
    """Every public file as `relative path -> (schema name, payload)`, in write order.

    Order matters for readers that fetch between moves: the files a page follows links
    from (`index.json`, then `live.json`) come last, after the files they point to.
    """
    generated_at = _iso(now)

    # Prominence-descending, each with the claims its ground truth supports and its importance,
    # which its sources' reputations feed. An archived event keeps its day page but is never
    # live: its score is no longer kept up to date. A held event (worker/publish/hold.py) is in
    # no file at all.
    sources = _load_sources(conn, now)
    voices = _voices(sources)
    gate = DiscoveryConfig.load().gate
    stored = _load_events(conn)
    held = holds(stored, on_beat=gate.beat_re(), min_share=gate.min_on_beat_share)
    if held:
        logger.info("held back %d events: %s", len(held), dict(Counter(held.values())))
    all_events = [
        with_importance(with_fact_claims(e), voices) for e in stored if e.event_id not in held
    ]
    public = [e.model_dump_public() for e in all_events]
    for p in public:
        # Stored before assembly dropped a feed's boilerplate (clean_summary), so drop it here.
        p["summary"] = strip_feed_tail(p["summary"]) or p["title"]
        for source in p.get("sources", []):
            source["url"] = _sanitize_source_url(source["url"])
    live = [
        p
        for e, p in zip(all_events, public, strict=True)
        if e.risk.prominence is not None
        and e.risk.prominence > LIVE_MIN_PROMINENCE
        and e.status is not EventStatus.ARCHIVED
    ][:LIVE_LIMIT]

    by_day: dict[date, list[dict[str, Any]]] = defaultdict(list)
    for e, p in zip(all_events, public, strict=True):
        by_day[e.first_seen.astimezone(UTC).date()].append(p)
    days = sorted(set(load_event_dates(conn)) | set(by_day), reverse=True)

    out: dict[str, tuple[str, dict[str, Any]]] = {}
    for day in days:
        events = by_day.get(day, [])
        out[f"history/{day.isoformat()}.json"] = (
            "history",
            {
                "generated_at": generated_at,
                "date": day.isoformat(),
                "pipeline_version": PIPELINE_VERSION,
                "counts": _event_counts(events),
                "events": events,
            },
        )
    jobs = load_latest_jobs(conn)
    out["source-health.json"] = (
        "source-health",
        _source_health_payload(conn, generated_at, sources, jobs),
    )
    out["system-status.json"] = (
        "system-status",
        _system_status_payload(
            conn, generated_at, published=len(live), total=len(public), held=len(held), live=live,
            jobs=jobs, ai_budget_usd=ai_budget_usd,
        ),
    )
    out["trends.json"] = ("trends", _trends_payload(conn, generated_at, now))
    out["crew.json"] = ("crew", _crew_payload(conn, generated_at, now, jobs))
    out["index.json"] = (
        "index",
        {
            "generated_at": generated_at,
            "pipeline_version": PIPELINE_VERSION,
            "latest": days[0].isoformat() if days else None,
            "days": [
                {
                    "date": d.isoformat(),
                    "path": f"history/{d.isoformat()}.json",
                    "event_count": len(by_day.get(d, [])),
                }
                for d in days
            ],
            "merged": merged_redirects(conn, since=now - MERGED_REDIRECT_WINDOW),
        },
    )
    out["live.json"] = (
        "live",
        {
            "generated_at": generated_at,
            "last_completed_collection": _iso_or_none(load_last_completed_collection(conn)),
            "pipeline_version": PIPELINE_VERSION,
            "counts": _event_counts(live),
            "events": live,
        },
    )
    return out


def _render(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, allow_nan=False, indent=2) + "\n"


def _gate(payloads: dict[str, tuple[str, dict[str, Any]]]) -> dict[str, str]:
    """Run every gate over every payload; return the exact text to write, or raise."""
    findings = [
        f"{rel}: {finding}"
        for rel, (_, payload) in payloads.items()
        for finding in scan_for_secrets(payload)
    ]
    if findings:
        raise ValidationFailure(
            f"secret scan found {len(findings)} suspected secret(s); nothing was published: "
            + "; ".join(findings[:10])
        )

    for rel, (schema, payload) in payloads.items():
        try:
            validate_payload(payload, schema)
        except ValidationFailure as exc:
            raise ValidationFailure(f"{rel}: {exc}") from exc

    rendered: dict[str, str] = {}
    for rel, (_, payload) in payloads.items():
        try:
            rendered[rel] = _render(payload)
        except (TypeError, ValueError) as exc:
            raise ValidationFailure(f"{rel}: not serialisable as strict JSON: {exc}") from exc

    # The text is what reaches disk, so scan it too (independent of the structural walk).
    findings = [
        f"{rel}: {finding}"
        for rel, text in rendered.items()
        for finding in scan_text_for_secrets(text)
    ]
    if findings:
        raise ValidationFailure(
            f"secret scan found {len(findings)} suspected secret(s) in serialised output; "
            "nothing was published: " + "; ".join(findings[:10])
        )
    return rendered


def _write_synced(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())


def _publish(rendered: dict[str, str], out_dir: Path) -> list[Path]:
    """Move already-validated text into `out_dir`; restore the old files on any failure."""
    out_dir.mkdir(parents=True, exist_ok=True)
    for stale in out_dir.glob(f"{STAGING_PREFIX}*"):  # left behind by a crashed run
        shutil.rmtree(stale, ignore_errors=True)

    # Staged inside out_dir so os.replace never crosses a filesystem boundary.
    staging = Path(tempfile.mkdtemp(prefix=STAGING_PREFIX, dir=out_dir))
    replaced: list[tuple[Path, Path | None]] = []
    try:
        for rel, text in rendered.items():
            _write_synced(staging / "new" / rel, text)

        targets: list[Path] = []
        try:
            for rel in rendered:
                target = out_dir / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                backup: Path | None = None
                if target.exists():
                    backup = staging / "old" / rel
                    backup.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(target, backup)
                os.replace(staging / "new" / rel, target)
                replaced.append((target, backup))
                targets.append(target)
        except BaseException:
            for target, backup in reversed(replaced):
                if backup is not None:
                    os.replace(backup, target)
                else:
                    target.unlink(missing_ok=True)
            raise
        return targets
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def build_all(
    conn: Connection, out_dir: Path, *, now: datetime, ai_budget_usd: Decimal | None = None
) -> list[Path]:
    """Build, gate and atomically publish every public file into `out_dir`.

    Raises `ValidationFailure` (and leaves `out_dir` untouched) if any payload fails the
    secret scan or its schema. Returns the paths written. `ai_budget_usd` is the AI layer's
    monthly budget, which system-status.json states; None leaves it unsaid.
    """
    payloads = build_payloads(conn, now=now, ai_budget_usd=ai_budget_usd)
    rendered = _gate(payloads)
    return _publish(rendered, Path(out_dir))
