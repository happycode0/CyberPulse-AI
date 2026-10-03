"""Pipeline orchestrator: one lane, one pass.

For each enabled source in the lane: fetch (conditional, concurrent, bounded) -> parse ->
normalise -> resolve against stored events -> create/merge events -> record health. When every
source is done, stored events that turn out to be one story are merged (consolidation,
worker/pipeline/correlate.py), the recent events' independent reports are settled by lineage
(worker/pipeline/lineage.py), all live events are rescored and the run summary is persisted.

Failure model (PLAN.md section 11): a source that errors, hangs, returns garbage or whose
items cannot be stored is recorded as failed and the run carries on. Each source is stored in
its own transaction, so one source's failure rolls back only that source's items. Only a
database failure aborts the run, because without Postgres there is nowhere to record anything.

Fetching is concurrent; storing is sequential (one source at a time, under a shared advisory
lock). Resolution reads what earlier items wrote, so it cannot be parallel, and the lock
stops two overlapping runs from each deciding the same story is new.
"""

import asyncio
import logging
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
from sqlalchemy import Connection, Engine

from worker.collectors.feed import parse_feed
from worker.collectors.http import (
    FetchResult,
    FetchStatus,
    cache_raw,
    fetch,
    prune_cache,
)
from worker.collectors.json_api import parse_json_api
from worker.collectors.web_page import parse_web_page
from worker.db.archive import archive_faded
from worker.db.au import load_au_facts, save_au
from worker.db.events import find_candidates, load_events, next_event_id
from worker.db.groundtruth import set_event_severity
from worker.db.ingest import (
    add_relationship,
    apply_update,
    events_needing_score,
    insert_new_event,
    known_url_hashes,
    lock_ingest,
    save_scores,
    touch_last_seen,
)
from worker.db.lineage import refresh_lineage
from worker.db.merge import load_story_records, load_token_weights, merge_events
from worker.db.runs import finish_run, start_run
from worker.db.session import get_engine
from worker.db.sources import (
    FetchState,
    load_fetch_states,
    load_health_history,
    load_lifecycle_states,
    record_health,
    save_fetch_state,
    set_lifecycle_state,
    upsert_registry,
)
from worker.models import (
    Event,
    HealthStatus,
    Lane,
    LifecycleState,
    NormalisedItem,
    RawItem,
    RelationshipType,
    RunSummary,
    SourceConfig,
    SourceHealth,
)
from worker.pipeline.assemble import build_new_event, evidence_class_for, plan_update
from worker.pipeline.au import assess_au
from worker.pipeline.correlate import plan_merges
from worker.pipeline.health import assess, next_lifecycle_state
from worker.pipeline.lineage import Publishers
from worker.pipeline.normalise import normalise
from worker.pipeline.resolve import Decision, resolve
from worker.pipeline.score import DEFAULT_SCORING_PATH, ArchiveRule, ScoringConfig, score_event
from worker.sources.registry import load_publishers, load_registry, sources_for_lane

logger = logging.getLogger(__name__)

CONFIG_DIR = Path(__file__).resolve().parents[2] / "config"
DEFAULT_REGISTRY_PATH = CONFIG_DIR / "sources.yaml"

MAX_CONCURRENT_FETCHES = 10
# Hard ceiling per source across all retries; `fetch` has its own per-attempt timeout.
SOURCE_DEADLINE_SECONDS = 120.0

# An event scored below this and not touched this run is not rescored: prominence only ever
# decays with time, so it cannot have risen.
RESCORE_FLOOR = 0.001
RESCORE_BATCH = 200

# Consolidation compares the events seen this recently, weighting headline words by how rare
# they are among this longer window's headlines.
CONSOLIDATE_LOOKBACK = timedelta(days=30)
WORD_WEIGHT_LOOKBACK = timedelta(days=180)

MAX_RUN_ERRORS = 100
ERROR_MAX_CHARS = 300

_FAILED = frozenset({HealthStatus.ERROR, HealthStatus.TIMEOUT, HealthStatus.EMPTY})
_RELATED_DECISIONS = frozenset({Decision.AMBIGUOUS, Decision.RELATED_BUT_DISTINCT})


@dataclass(frozen=True)
class Prepared:
    item: NormalisedItem
    payload_hash: str


@dataclass
class Collected:
    """One source after fetch + parse + normalise, before anything is stored."""

    source: SourceConfig
    fetch: FetchResult
    items: list[Prepared] = field(default_factory=list)
    raw_count: int = 0
    errors: list[str] = field(default_factory=list)


@dataclass
class Tally:
    new_events: int = 0
    updated_events: int = 0
    duplicates: int = 0
    touched: set[str] = field(default_factory=set)


@dataclass
class SourceOutcome:
    health: SourceHealth
    tally: Tally
    items_fetched: int
    errors: list[str]


def _short(message: object) -> str:
    text = " ".join(str(message).split())
    return text if len(text) <= ERROR_MAX_CHARS else text[: ERROR_MAX_CHARS - 1] + "\u2026"


def parse_body(source: SourceConfig, body: bytes) -> list[RawItem]:
    """Parse a fetched body with the collector for the source's type."""
    if source.type == "json_api":
        return parse_json_api(source, body)
    if source.type in ("rss", "atom"):
        return parse_feed(source, body)
    if source.type == "web_page":
        return parse_web_page(source, body)
    raise ValueError(f"unsupported source type {source.type!r}")


def _failed_fetch(status: FetchStatus, error: str, duration_ms: int = 0) -> FetchResult:
    return FetchResult(status=status, error=error, duration_ms=duration_ms)


async def _collect(
    client: httpx.AsyncClient,
    gate: asyncio.Semaphore,
    source: SourceConfig,
    state: FetchState,
    *,
    now: datetime,
) -> Collected:
    async with gate:
        try:
            result = await asyncio.wait_for(
                fetch(client, source, etag=state.etag, last_modified=state.last_modified),
                SOURCE_DEADLINE_SECONDS,
            )
        except TimeoutError:
            result = _failed_fetch(
                FetchStatus.TIMEOUT,
                f"no response within {SOURCE_DEADLINE_SECONDS:g}s",
                int(SOURCE_DEADLINE_SECONDS * 1000),
            )
        except Exception as exc:
            result = _failed_fetch(FetchStatus.ERROR, _short(exc))

    collected = Collected(source=source, fetch=result)
    if result.status is not FetchStatus.OK or result.body is None:
        return collected

    try:
        cache_raw(source.id, result.body)
    except OSError as exc:
        logger.warning("raw cache write failed for %s: %s", source.id, exc)

    try:
        raws = parse_body(source, result.body)
    except Exception as exc:
        collected.fetch = _failed_fetch(
            FetchStatus.ERROR, _short(f"parse failed: {exc!r}"), result.duration_ms
        )
        return collected

    collected.raw_count = len(raws)
    for raw in raws:
        try:
            collected.items.append(Prepared(normalise(raw, now=now), raw.payload_hash))
        except Exception as exc:
            collected.errors.append(_short(f"{source.id}: skipped {raw.url}: {exc!r}"))
    return collected


async def _collect_safe(
    client: httpx.AsyncClient,
    gate: asyncio.Semaphore,
    source: SourceConfig,
    state: FetchState,
    *,
    now: datetime,
) -> Collected:
    """`_collect` that cannot raise: an unexpected error becomes that source's failure."""
    try:
        return await _collect(client, gate, source, state, now=now)
    except Exception as exc:
        logger.exception("collecting %s failed", source.id)
        return Collected(source, _failed_fetch(FetchStatus.ERROR, _short(f"collect failed: {exc!r}")))


def _ingest(
    conn: Connection, source: SourceConfig, prepared: Sequence[Prepared], *, now: datetime
) -> Tally:
    """Resolve each item and create or merge its event. Runs inside the caller's savepoint."""
    tally = Tally()
    seen_again: set[str] = set()
    known = known_url_hashes(conn, [p.item.url_hash for p in prepared])

    for p in prepared:
        item = p.item
        if (event_id := known.get(item.url_hash)) is not None:
            seen_again.add(event_id)
            tally.duplicates += 1
            continue

        candidates = find_candidates(conn, item)
        resolution = resolve(item, candidates, evidence_class=evidence_class_for(source))

        if resolution.decision is Decision.DUPLICATE:
            assert resolution.event_id is not None
            seen_again.add(resolution.event_id)
            tally.duplicates += 1
        elif resolution.decision is Decision.UPDATE_EXISTING:
            target = next(c for c in candidates if c.event_id == resolution.event_id)
            apply_update(
                conn,
                plan_update(target, item, source, now=now),
                item,
                payload_hash=p.payload_hash,
            )
            tally.touched.add(target.event_id)
            tally.updated_events += 1
        else:
            # NEW_EVENT, plus AMBIGUOUS / RELATED_BUT_DISTINCT: ingest never merges on a
            # doubt. The similar event is linked; consolidation may join them later.
            event = build_new_event(item, source, next_event_id(conn, now.year), now=now)
            insert_new_event(conn, event, item, payload_hash=p.payload_hash)
            if resolution.decision in _RELATED_DECISIONS and resolution.event_id:
                add_relationship(
                    conn, event.event_id, resolution.event_id, RelationshipType.RELATED_EVENT
                )
            tally.touched.add(event.event_id)
            tally.new_events += 1

    touch_last_seen(conn, seen_again, now)
    return tally


def _apply_lifecycle(conn: Connection, source: SourceConfig) -> None:
    """Move the source along its lifecycle if its recorded health says it should.

    `next_lifecycle_state` only demotes an ACTIVE source after repeated failures or lets a
    DEGRADED/BROKEN one back in through TESTING; it never promotes, so this cannot activate
    anything (that gate belongs to SERAPH).
    """
    current = source.lifecycle_state or (
        LifecycleState.ACTIVE if source.enabled else LifecycleState.DISCOVERED
    )
    new = next_lifecycle_state(source, load_health_history(conn, source.id))
    if new is not current:
        set_lifecycle_state(conn, source.id, new)
        logger.warning("source %s: lifecycle %s -> %s", source.id, current.value, new.value)


def _store_source(engine: Engine, c: Collected, *, now: datetime) -> SourceOutcome:
    """Store one collected source and record its health, in a single transaction.

    Items are written inside a savepoint: if storing them fails, only the items roll back,
    and the failure is what gets recorded as the source's health.
    """
    source = c.source
    result = c.fetch
    tally = Tally()
    errors = list(c.errors)
    stored = True

    with engine.begin() as conn:
        history = load_health_history(conn, source.id)
        if c.items:
            lock_ingest(conn)
            try:
                with conn.begin_nested():
                    tally = _ingest(conn, source, c.items, now=now)
            except Exception as exc:
                logger.exception("storing items from %s failed", source.id)
                stored = False
                tally = Tally()
                result = _failed_fetch(
                    FetchStatus.ERROR, _short(f"storing items failed: {exc!r}"), result.duration_ms
                )

        items = [p.item for p in c.items] if stored else []
        health = assess(source, result, items, now=now, history=history)
        record_health(conn, health)
        if stored and result.status is FetchStatus.OK and items:
            save_fetch_state(conn, source.id, FetchState(result.etag, result.last_modified))
        _apply_lifecycle(conn, source)

    if health.status in _FAILED or health.status is HealthStatus.STALE:
        errors.append(_short(f"{source.id}: {health.status.value}: {health.error}"))
    return SourceOutcome(
        health=health,
        tally=tally,
        items_fetched=c.raw_count if stored else 0,
        errors=errors,
    )


def _assess_au(conn: Connection, events: Sequence[Event], config: ScoringConfig) -> list[Event]:
    """Set each event's AU relevance from its facts (worker/pipeline/au.py) before it is scored,
    since AU relevance is one of prominence's terms."""
    facts = load_au_facts(conn, [e.event_id for e in events])
    assessed = {e.event_id: assess_au(facts[e.event_id], config.au) for e in events}
    save_au(conn, assessed)
    return [e.model_copy(update={"au": assessed[e.event_id]}) for e in events]


def rescore(
    engine: Engine, config: ScoringConfig, touched: Iterable[str], *, now: datetime
) -> list[str]:
    """Rescore every event whose stored score may be stale, AU relevance first. Returns error
    messages.

    Public because the ground-truth sync needs it too: KEV listings and CVSS bands are two of the
    three inputs to `urgency`, so a sync that changed them has left stored scores wrong in exactly
    the way this repairs. `touched` matters there — `events_needing_score` skips events already
    scored below `RESCORE_FLOOR`, so an event whose severity just went from unknown to critical
    only gets looked at again if it is named.
    """
    errors: list[str] = []
    with engine.begin() as conn:
        ids = events_needing_score(
            conn, scoring_version=config.version, touched=touched, floor=RESCORE_FLOOR
        )
        for i in range(0, len(ids), RESCORE_BATCH):
            chunk = ids[i : i + RESCORE_BATCH]
            try:
                with conn.begin_nested():
                    events = _assess_au(conn, load_events(conn, chunk), config)
                    save_scores(conn, (score_event(e, config, now=now) for e in events))
            except Exception as exc:
                logger.exception("scoring batch failed")
                errors.append(_short(f"scoring: {len(chunk)} events skipped: {exc!r}"))
    return errors


@dataclass
class Consolidation:
    touched: set[str] = field(default_factory=set)
    archived: int = 0
    errors: list[str] = field(default_factory=list)


def consolidate(engine: Engine, *, now: datetime, publishers: Publishers) -> Consolidation:
    """Merge the recent events that are one story (worker/pipeline/correlate.py), then settle
    which of their reports are independent (worker/pipeline/lineage.py).

    One transaction under the ingest lock, so no source is being stored meanwhile. A failure
    rolls the whole pass back and is reported, never raised: the run's own work is already
    stored and must still be scored.
    """
    since = now - CONSOLIDATE_LOOKBACK
    try:
        with engine.begin() as conn:
            lock_ingest(conn)
            records = load_story_records(conn, since=since)
            weights = load_token_weights(conn, since=now - WORD_WEIGHT_LOOKBACK)
            groups = plan_merges(records, weights)
            for g in groups:
                merge_events(conn, g)
                logger.info(
                    "correlation: merged %s into %s (%s)",
                    ", ".join(g.losers),
                    g.winner,
                    ", ".join(g.methods),
                )
            done = Consolidation(
                touched={g.winner for g in groups}, archived=sum(len(g.losers) for g in groups)
            )
            if groups:
                # A winner with a loser's CVEs may now band higher.
                done.touched |= set(set_event_severity(conn))
            # Every recent event, not only the winners: this run's ingest guessed per outlet.
            done.touched |= refresh_lineage(conn, publishers, since=since)
    except Exception as exc:
        logger.exception("correlation failed")
        return Consolidation(errors=[_short(f"correlation: {exc!r}")])
    return done


def archive(engine: Engine, rule: ArchiveRule, *, now: datetime) -> tuple[list[str], list[str]]:
    """Archive the events that have faded since they were scored (worker/db/archive.py).
    Returns their ids and any error: like consolidation, a failure is reported, never raised."""
    try:
        with engine.begin() as conn:
            lock_ingest(conn)
            ids = archive_faded(conn, rule, now=now)
    except Exception as exc:
        logger.exception("archiving failed")
        return [], [_short(f"archiving: {exc!r}")]
    if ids:
        logger.info("archived %d faded events", len(ids))
    return ids, []


def _prepare(
    engine: Engine, sources: Sequence[SourceConfig], run_id: str, lane: Lane, started: datetime
) -> tuple[dict[str, LifecycleState], dict[str, FetchState]]:
    with engine.begin() as conn:
        upsert_registry(conn, sources)
        start_run(conn, run_id, lane, started)
        return load_lifecycle_states(conn), load_fetch_states(conn)


async def run_lane(
    lane: Lane,
    *,
    once: bool = False,
    engine: Engine | None = None,
    registry_path: Path | None = None,
    scoring_path: Path | None = None,
    now: datetime | None = None,
) -> RunSummary:
    """Run one pass over every enabled source in `lane` and persist the summary.

    A run is always a single pass; repeating it is the scheduler's job. `once` exists so the
    CLI's `--once` maps straight onto it. `engine`, the two config paths and `now` default to
    the process database, `config/` and the wall clock; they exist so tests can substitute them.
    """
    clock = (lambda: now) if now is not None else (lambda: datetime.now(UTC))
    started = clock()
    engine = engine or get_engine()
    registry = load_registry(registry_path or DEFAULT_REGISTRY_PATH)
    publishers = Publishers.from_registry(
        registry, load_publishers(registry_path or DEFAULT_REGISTRY_PATH)
    )
    scoring = ScoringConfig.load(scoring_path or DEFAULT_SCORING_PATH)
    sources = sources_for_lane(registry, lane)
    run_id = f"run-{lane.value}-{started:%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:6]}"
    logger.info("run %s: %d %s sources (once=%s)", run_id, len(sources), lane.value, once)

    lifecycle, fetch_states = await asyncio.to_thread(
        _prepare, engine, registry, run_id, lane, started
    )
    sources = [s.model_copy(update={"lifecycle_state": lifecycle.get(s.id)}) for s in sources]

    gate = asyncio.Semaphore(MAX_CONCURRENT_FETCHES)
    ok = failed = stale = items_fetched = 0
    new = updated = duplicates = 0
    errors: list[str] = []
    touched: set[str] = set()

    async with httpx.AsyncClient(follow_redirects=True) as client:
        no_state = FetchState(None, None)
        pending = [
            asyncio.ensure_future(
                _collect_safe(client, gate, s, fetch_states.get(s.id, no_state), now=started)
            )
            for s in sources
        ]
        # Store each source as soon as its fetch lands, so a slow or hanging source delays
        # only itself. Storing is sequential (see the module docstring); fetching carries on
        # concurrently in the background meanwhile.
        for finished in asyncio.as_completed(pending):
            c = await finished
            try:
                outcome = await asyncio.to_thread(_store_source, engine, c, now=started)
            except Exception as exc:
                logger.exception("source %s failed", c.source.id)
                failed += 1
                errors.append(_short(f"{c.source.id}: {exc!r}"))
                continue

            status = outcome.health.status
            ok += status is HealthStatus.OK
            stale += status is HealthStatus.STALE
            failed += status in _FAILED
            items_fetched += outcome.items_fetched
            new += outcome.tally.new_events
            updated += outcome.tally.updated_events
            duplicates += outcome.tally.duplicates
            touched |= outcome.tally.touched
            errors.extend(outcome.errors)

    merged = await asyncio.to_thread(consolidate, engine, now=started, publishers=publishers)
    errors.extend(merged.errors)
    touched |= merged.touched
    errors.extend(await asyncio.to_thread(rescore, engine, scoring, touched, now=started))
    faded, archive_errors = await asyncio.to_thread(
        archive, engine, scoring.archive, now=started
    )
    errors.extend(archive_errors)

    try:
        prune_cache()
    except OSError as exc:
        logger.warning("raw cache prune failed: %s", exc)

    summary = RunSummary(
        run_id=run_id,
        lane=lane,
        started_at=started,
        finished_at=clock(),
        sources_ok=ok,
        sources_failed=failed,
        sources_stale=stale,
        items_fetched=items_fetched,
        new_events=new,
        updated_events=updated,
        duplicates=duplicates,
        archived_events=merged.archived + len(faded),
        errors=errors[:MAX_RUN_ERRORS],
    )
    with engine.begin() as conn:
        finish_run(conn, summary)
    logger.info(
        "run %s done: ok=%d failed=%d stale=%d items=%d new=%d updated=%d dup=%d merged=%d "
        "archived=%d",
        run_id, ok, failed, stale, items_fetched, new, updated, duplicates, merged.archived,
        len(faded),
    )
    return summary
