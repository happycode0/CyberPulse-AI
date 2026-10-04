"""Build the public JSON files from the database, or write nothing.

`build_all` is fail-closed and all-or-nothing:

1. every payload is built in memory;
2. every payload is secret-scanned, then schema-validated, then serialised and the
   serialised text is secret-scanned again (that is what would reach disk);
3. only if all of that passes is anything written: files are staged inside `out_dir`, then
   moved into place with `os.replace`, and any failure while moving restores the previous
   files.

A failure in steps 1-2 leaves `out_dir` exactly as it was, including not creating it.
"""

import json
import os
import shutil
import tempfile
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any

from pydantic import ValidationError
from sqlalchemy import Connection

from worker.db.crew import load_crew_activity, load_pipeline_ai
from worker.db.discovery import Candidate, load_at_the_gate
from worker.db.events import load_event_dates, load_live_events
from worker.db.merge import merged_redirects
from worker.db.runs import load_last_completed_collection, load_latest_run
from worker.db.sources import (
    RegistryRow,
    load_corroboration,
    load_health_history,
    load_lifecycle_states,
    load_registry_rows,
)
from worker.db.trends import load_trend_inputs
from worker.models import (
    Beat,
    Event,
    EventStatus,
    HealthStatus,
    LifecycleState,
    PublisherConfig,
    SourceConfig,
    SourceHealth,
)
from worker.pipeline.health import consecutive_failures
from worker.pipeline.importance import Voice, with_importance
from worker.pipeline.reputation import CORROBORATION_WINDOW, Reputation, assess_reputation
from worker.pipeline.trends import ACTIVITY_DAYS, compute_trends, load_trends_config
from worker.publish.claims import with_fact_claims
from worker.publish.validate import (
    ValidationFailure,
    scan_for_secrets,
    scan_text_for_secrets,
    validate_payload,
)
from worker.schedule import public_schedule
from worker.sources.registry import REGISTRY_PATH, load_publishers, load_registry
from worker.version import PIPELINE_VERSION, SCHEMA_VERSION, SCORING_VERSION

LIVE_MIN_PROMINENCE = 0.05
LIVE_LIMIT = 500
# Below every possible prominence (0..1), so this selects every scored event.
ALL_SCORED = -1.0
HISTORY_EVENT_LIMIT = 10_000
HEALTH_HISTORY_LIMIT = 20
# index.json's `merged` map covers events first seen this recently: a link to one of them that
# was shared before it was merged still finds the story (site/assets/hud.js, `findEvent`).
MERGED_REDIRECT_WINDOW = timedelta(days=90)
HEALTH_ERROR_MAX_CHARS = 300
# source-health.json's `pipeline`: the finds still at SERAPH's gate, furthest through first.
PIPELINE_LIMIT = 25
# An `attention` reason quotes at most this much of a check's error or a registry note.
ATTENTION_DETAIL_MAX_CHARS = 160

# crew.json: how recent an agent's last piece of work must be for it to read ACTIVE: the
# ledger takes a row whenever the pipeline calls a model. SERAPH needs none, because writing
# crew.json is its publish job, so it is active whenever the file is written, at any cadence.
CREW_FRESH = {
    "ripperdoc": timedelta(hours=24),
}

STAGING_PREFIX = ".publish-"
_SEVERITIES = ("critical", "high", "medium", "low", "unknown")


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
    degraded and healthy again. `coming`: it is not collected yet, or is still a find on trial.
    A retired source needs nothing.
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
        return {"kind": "coming", "reason": f"Not collected yet. {note}".rstrip()}
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


def _gate_reason(c: Candidate) -> str:
    """Where a find stands, in the worker's own words. Never the finder's `reason`, which a
    model wrote from pages on the open web."""
    who = _FOUND_BY.get(c.found_by, "Found")
    if c.state == "discovered":
        return f"{who}; looking for its feed."
    if c.state == "testing":
        return f"{who}; {_plural(c.passes, 'healthy probe')} in a row so far."
    if c.failures:
        return f"{who}; feed found, {_plural(c.failures, 'failed probe')} in a row."
    return f"{who}; feed found, waiting for its first probe."


def _pipeline(conn: Connection) -> list[dict[str, Any]]:
    """The finds still at the gate: names and domains only, never a feed URL or evidence."""
    return [
        {
            "name": c.name,
            "host": c.host,
            "state": c.state,
            "found_at": _iso(c.created_at),
            "reason": _gate_reason(c),
        }
        for c in load_at_the_gate(conn, limit=PIPELINE_LIMIT)
    ]


def _source_health_payload(
    conn: Connection, generated_at: str, sources: list[_Source] | None = None
) -> dict[str, Any]:
    if sources is None:
        sources = _load_sources(conn, datetime.fromisoformat(generated_at))
    entries = [_source_entry(s) for s in sources]
    counts = Counter(e["latest"]["status"] if e["latest"] else "no_data" for e in entries)
    return {
        "generated_at": generated_at,
        "counts": dict(counts),
        "sources": entries,
        "pipeline": _pipeline(conn),
    }


def _system_status_payload(
    conn: Connection, generated_at: str, *, published: int, total: int
) -> dict[str, Any]:
    run = load_latest_run(conn)
    registry = load_registry_rows(conn)
    return {
        "generated_at": generated_at,
        "last_completed_collection": _iso_or_none(load_last_completed_collection(conn)),
        "pipeline_version": PIPELINE_VERSION,
        "schema_version": SCHEMA_VERSION,
        "scoring_version": SCORING_VERSION,
        "last_run": None
        if run is None
        else {
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
        },
        "counts": {
            "events_published": published,
            "events_total": total,
            "sources_total": len(registry),
            "sources_enabled": sum(1 for r in registry if r.enabled),
        },
        # What runs when, and whether code or a model does it (worker/schedule.py).
        "schedule": public_schedule(),
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


def _crew_payload(conn: Connection, generated_at: str, now: datetime) -> dict[str, Any]:
    """The work the worker does for the crew this month (worker/db/crew.py).

    SERAPH is doing its job by writing this file, so its last piece of work is now, and it is
    active unless the newest ground-truth pass failed. It spends no tokens. RIPPERDOC's own
    model runs are in Paperclip, which the worker never reads, so its cost is null, not 0.
    """
    month_start = now.astimezone(UTC).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    agents = []
    for callsign, activity in load_crew_activity(conn, since=month_start).items():
        last_active = _iso_or_none(activity.last_active)
        if callsign == "seraph":
            last_active = generated_at
        if activity.failing:
            status = "degraded"
        elif callsign == "seraph" or (
            activity.last_active and now - activity.last_active <= CREW_FRESH[callsign]
        ):
            status = "active"
        else:
            status = "idle"
        agents.append(
            {
                "callsign": callsign.upper(),
                "status": status,
                "tasks_completed": activity.tasks,
                "items_processed": activity.items,
                "cost_usd": 0.0 if callsign == "seraph" else None,
                "last_active_at": last_active,
            }
        )
    ai = load_pipeline_ai(conn, since=month_start)
    return {
        "generated_at": generated_at,
        "month": month_start.strftime("%Y-%m"),
        "agents": agents,
        "pipeline_ai": {"calls": ai.calls, "cost_usd": round(float(ai.cost_usd), 4)},
    }


def build_payloads(conn: Connection, *, now: datetime) -> dict[str, tuple[str, dict[str, Any]]]:
    """Every public file as `relative path -> (schema name, payload)`, in write order.

    Order matters for readers that fetch between moves: the files a page follows links
    from (`index.json`, then `live.json`) come last, after the files they point to.
    """
    generated_at = _iso(now)

    # Prominence-descending, each with the claims its ground truth supports and its importance,
    # which its sources' reputations feed. An archived event keeps its day page but is never
    # live: its score is no longer kept up to date.
    sources = _load_sources(conn, now)
    voices = _voices(sources)
    all_events = [with_importance(with_fact_claims(e), voices) for e in _load_events(conn)]
    public = [e.model_dump_public() for e in all_events]
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
    out["source-health.json"] = (
        "source-health",
        _source_health_payload(conn, generated_at, sources),
    )
    out["system-status.json"] = (
        "system-status",
        _system_status_payload(conn, generated_at, published=len(live), total=len(public)),
    )
    out["trends.json"] = ("trends", _trends_payload(conn, generated_at, now))
    out["crew.json"] = ("crew", _crew_payload(conn, generated_at, now))
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


def build_all(conn: Connection, out_dir: Path, *, now: datetime) -> list[Path]:
    """Build, gate and atomically publish every public file into `out_dir`.

    Raises `ValidationFailure` (and leaves `out_dir` untouched) if any payload fails the
    secret scan or its schema. Returns the paths written.
    """
    payloads = build_payloads(conn, now=now)
    rendered = _gate(payloads)
    return _publish(rendered, Path(out_dir))
