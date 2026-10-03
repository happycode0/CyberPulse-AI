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
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any

from pydantic import ValidationError
from sqlalchemy import Connection

from worker.db.crew import load_crew_activity, load_pipeline_ai
from worker.db.events import load_event_dates, load_live_events
from worker.db.merge import merged_redirects
from worker.db.runs import load_last_completed_collection, load_latest_run
from worker.db.sources import (
    load_health_history,
    load_lifecycle_states,
    load_registry_rows,
)
from worker.db.trends import load_trend_inputs
from worker.models import Event, EventStatus, LifecycleState, SourceHealth
from worker.pipeline.trends import ACTIVITY_DAYS, compute_trends, load_trends_config
from worker.publish.claims import with_fact_claims
from worker.publish.validate import (
    ValidationFailure,
    scan_for_secrets,
    scan_text_for_secrets,
    validate_payload,
)
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

# crew.json: how recent an agent's last piece of work must be for it to read ACTIVE. The jobs'
# own cadences, with slack: collection and source checks every 15 minutes, ground truth every
# 6 hours, and the ledger whenever the pipeline calls a model.
CREW_FRESH = {
    "librarian": timedelta(hours=7),
    "prowl": timedelta(minutes=30),
    "seraph": timedelta(minutes=30),
    "rogue": timedelta(hours=24),
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


def _source_health_payload(conn: Connection, generated_at: str) -> dict[str, Any]:
    states = load_lifecycle_states(conn)
    sources = []
    for row in load_registry_rows(conn):
        history = load_health_history(conn, row.id, HEALTH_HISTORY_LIMIT)
        state = states.get(row.id) or (
            LifecycleState.ACTIVE if row.enabled else LifecycleState.DISCOVERED
        )
        sources.append(
            {
                "source_id": row.id,
                "name": row.name,
                "region": row.region,
                "category": row.category,
                "lane": row.lane,
                "enabled": row.enabled,
                "lifecycle_state": state.value,
                "latest": _health_record(history[-1]) if history else None,
                "history": [_health_record(h) for h in history],
            }
        )
    counts = Counter(s["latest"]["status"] if s["latest"] else "no_data" for s in sources)
    return {"generated_at": generated_at, "counts": dict(counts), "sources": sources}


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
    """The deterministic agents' work this month (worker/db/crew.py). LINK is doing its job by
    writing this file, so it is active and its last piece of work is now."""
    month_start = now.astimezone(UTC).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    agents = []
    for callsign, activity in load_crew_activity(conn, since=month_start).items():
        if activity.failing:
            status = "degraded"
        elif activity.last_active and now - activity.last_active <= CREW_FRESH[callsign]:
            status = "active"
        else:
            status = "idle"
        agents.append(
            {
                "callsign": callsign.upper(),
                "status": status,
                "tasks_completed": activity.tasks,
                "items_processed": activity.items,
                "cost_usd": 0.0,
                "last_active_at": _iso_or_none(activity.last_active),
            }
        )
    agents.append(
        {
            "callsign": "LINK",
            "status": "active",
            "tasks_completed": None,
            "items_processed": None,
            "cost_usd": 0.0,
            "last_active_at": generated_at,
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

    # Prominence-descending, each with the claims its ground truth supports. An archived event
    # keeps its day page but is never live: its score is no longer kept up to date.
    all_events = [with_fact_claims(e) for e in _load_events(conn)]
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
    out["source-health.json"] = ("source-health", _source_health_payload(conn, generated_at))
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
