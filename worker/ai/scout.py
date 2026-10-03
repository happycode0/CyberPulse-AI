"""RIPPERDOC's work in the worker (PLAN.md §7.6): the daily model scan, the weekly gauntlet, and
the golden set the gauntlet judges on. RIPPERDOC itself reads the results (GET /ops/models)
and raises what they show.

- **The scan**, daily: OpenRouter's model list and what changed in it (worker/ai/catalogue.py),
  then the ladder through the price guard again. A model that drifted over the ceiling or lost
  its last capable route leaves its chain for as long as it fails (worker/ai/ladder.py), and
  the scheduler hands the result to the AI layer, so enrichment routes on it straight away.
  Going down a chain that was signed off needs nobody's approval (§7.7).
- **The gauntlet**, on Sundays: each enrichment tier's default and up to two challengers on
  the golden set (worker/ai/gauntlet.py), within `MONTHLY_CAP_USD`. A challenger that is
  better by §7.6's rules becomes a proposal in `agent_proposals`; promoting it is MORPHEUS's
  decision, on the worker's cost figures, and a reviewed edit to config/models.yaml.
- **The golden set** is pinned the first time the gauntlet finds none, or by
  `--pin-golden-set` (worker/ai/golden.py).

Neither pass is fatal to the schedule, and neither sends a key anywhere but OpenRouter's
completions and key endpoints.
"""

import asyncio
import dataclasses
import functools
import logging
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, TypeVar

import httpx
import yaml
from pydantic import ValidationError
from sqlalchemy import Connection, Engine

from worker.ai.budget import BudgetUnreadable, Mode, assess, fetch_key_status
from worker.ai.catalogue import (
    Change,
    diff,
    drop_changes,
    dropped_in,
    fetch_listing,
    ladder_report,
)
from worker.ai.client import (
    Attribution,
    BudgetExhausted,
    CallFailed,
    InvalidOutput,
    OpenRouterClient,
)
from worker.ai.gauntlet import (
    AGENT,
    CHALLENGERS_PER_TIER,
    CONCURRENCY,
    FREE_REQUEST_MARGIN,
    INCUMBENT_REUSE,
    MAX_VERIFIED_PER_TIER,
    MONTHLY_CAP_USD,
    RUN_CAP_USD,
    STAGE,
    TIER_TASK,
    Attempt,
    Decision,
    Listed,
    Measured,
    challengers,
    decide,
    estimate,
    failures,
    measure,
    proposal_body,
    proposal_payload,
    proposal_title,
    score,
)
from worker.ai.golden import (
    GoldenEvent,
    au_label_counts,
    blinded,
    digest,
    pin,
    stratify,
    subject_from_record,
)
from worker.ai.ladder import (
    DEFAULT_LADDER_PATH,
    CatalogueUnavailable,
    Ladder,
    LadderRejected,
    LadderUnusable,
    Tier,
    VerifiedCandidate,
    VerifiedLadder,
    verify_candidate,
    verify_ladder,
)
from worker.ai.tasks import Rejected, TaskName, messages
from worker.db import scout as db
from worker.db.digest import month_bounds
from worker.db.enrichment import load_subjects
from worker.db.ledger import record_to
from worker.db.session import get_engine
from worker.settings import Settings, get_settings

logger = logging.getLogger(__name__)

T = TypeVar("T")

# What each budget mode lets the gauntlet try. Tier 2 is the costly one, so CONSERVE leaves it;
# MINIMAL and FREE_ONLY leave everything that is paid for.
TIERS_BY_MODE: dict[Mode, tuple[Tier, ...]] = {
    Mode.FULL: (Tier.FREE, Tier.CHEAP, Tier.STRONG),
    Mode.CONSERVE: (Tier.FREE, Tier.CHEAP),
    Mode.MINIMAL: (Tier.FREE,),
    Mode.FREE_ONLY: (Tier.FREE,),
    Mode.OFF: (),
}


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _in(engine: Engine, fn: Callable[..., T], /, *args: Any, **kwargs: Any) -> T:
    with engine.begin() as conn:
        return fn(conn, *args, **kwargs)


def _client(settings: Settings, transport: httpx.AsyncBaseTransport | None) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=transport, headers={"User-Agent": settings.user_agent})


# ─── The daily scan ───────────────────────────────────────────────────────────────────────────────


@dataclass
class ScanSummary:
    models_listed: int | None = None
    changes: Counter[str] = field(default_factory=Counter)  # per kind
    new_free: list[str] = field(default_factory=list)  # new :free models that can do the work
    dropped: list[str] = field(default_factory=list)  # every model the guard keeps out now
    # The ladder as the guard left it, or None. `ladder_failed` says None means the ladder is
    # unusable, rather than that it could not be checked.
    verified: VerifiedLadder | None = None
    ladder_failed: bool = False
    errors: list[str] = field(default_factory=list)

    @property
    def changed_anything(self) -> bool:
        return bool(sum(self.changes.values()))


def _store_scan(
    conn: Connection,
    *,
    listing: Sequence[Any] | None,
    catalogue_changes: Sequence[Change],
    report: dict[str, Any] | None,
    now: datetime,
    error: str | None,
) -> list[Change]:
    if listing is not None:
        db.store_listing(conn, listing, catalogue_changes, now)
    drops = drop_changes(dropped_in(db.last_scan_ladder(conn)), report) if report else []
    db.insert_changes(conn, drops, now)
    db.record_scan(
        conn,
        ts=now,
        models_listed=len(listing) if listing is not None else None,
        changes=len(catalogue_changes) + len(drops),
        ladder=report,
        error=error,
    )
    return drops


async def scan_models(
    *,
    engine: Engine | None = None,
    settings: Settings | None = None,
    now: datetime | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> ScanSummary:
    """Read the model list, record what changed, and check the ladder. Sends no key."""
    settings = settings or get_settings()
    engine = engine or get_engine()
    now = now or _utcnow()
    summary = ScanSummary()
    listing = None
    catalogue_changes: list[Change] = []
    report: dict[str, Any] | None = None

    async with _client(settings, transport) as http:
        try:
            listing = await fetch_listing(http)
            known = await asyncio.to_thread(_in, engine, db.load_catalogue)
            catalogue_changes = diff(known, listing)
        except CatalogueUnavailable as exc:
            listing = None
            summary.errors.append(str(exc))
        else:
            summary.models_listed = len(listing)
            new = {c.slug for c in catalogue_changes if c.kind == "new"}
            summary.new_free = [m.slug for m in listing if m.slug in new and m.free and m.capable]

        try:
            configured = Ladder.load()
        except (OSError, yaml.YAMLError, ValidationError) as exc:
            summary.ladder_failed = True
            summary.errors.append(f"{DEFAULT_LADDER_PATH.name}: {exc}")
        else:
            try:
                summary.verified = await verify_ladder(configured, client=http)
            except LadderRejected as exc:
                summary.ladder_failed = True
                summary.errors.append(f"the ladder did not pass: {exc}")
                report = ladder_report(configured.tiers, None, exc.breaches)
            except CatalogueUnavailable as exc:
                summary.errors.append(f"the ladder could not be checked: {exc}")
            else:
                report = ladder_report(configured.tiers, summary.verified, summary.verified.dropped)

    drops = await asyncio.to_thread(
        _in,
        engine,
        _store_scan,
        listing=listing,
        catalogue_changes=catalogue_changes,
        report=report,
        now=now,
        error="; ".join(summary.errors) or None,
    )
    summary.changes = Counter(c.kind for c in [*catalogue_changes, *drops])
    summary.dropped = [
        f"{tier}: {d['slug']}: {d['reason']}"
        for tier, entry in (report or {}).items()
        for d in entry["dropped"]
    ]

    for slug in summary.new_free:
        logger.warning(
            "model scan: new free model %s can do the work; Sunday's gauntlet tries it", slug
        )
    for c in drops:
        if c.kind == "dropped":
            logger.warning(
                "model scan: %s left %s: %s", c.slug, c.detail["tier"], c.detail["reason"]
            )
        else:
            logger.info("model scan: %s is back in %s", c.slug, c.detail["tier"])
    logger.info(
        "model scan: %s models listed, %d changes%s%s",
        summary.models_listed if summary.models_listed is not None else "no",
        sum(summary.changes.values()),
        f", {len(summary.dropped)} dropped from the ladder" if summary.dropped else "",
        f"; {'; '.join(summary.errors)}" if summary.errors else "",
    )
    return summary


# ─── The golden set ───────────────────────────────────────────────────────────────────────────────


def _pin(conn: Connection, now: datetime) -> list[GoldenEvent]:
    ids = stratify(db.golden_candidates(conn))
    golden = [pin(s) for s in load_subjects(conn, ids)]
    db.replace_golden(conn, golden, now)
    return golden


def pin_golden_set(engine: Engine | None = None, now: datetime | None = None) -> list[GoldenEvent]:
    """Replace the golden set with a fresh one. Earlier gauntlet results stop being reused,
    because they were measured on a different set."""
    return _in(engine or get_engine(), _pin, now or _utcnow())


def _golden(conn: Connection, now: datetime) -> tuple[list[GoldenEvent], int]:
    golden = db.load_golden(conn)
    if not golden:
        golden = _pin(conn, now)
        if golden:
            logger.info("gauntlet: pinned a golden set of %d events", len(golden))
    return golden, db.golden_reviewed(conn)


# ─── The gauntlet ─────────────────────────────────────────────────────────────────────────────────


@dataclass
class GauntletSummary:
    skipped: str | None = None  # why nothing was tried
    run_id: int | None = None
    measured: list[Measured] = field(default_factory=list)
    proposals: list[str] = field(default_factory=list)  # titles of new proposals
    spent_usd: Decimal = Decimal(0)
    notes: list[str] = field(default_factory=list)

    @property
    def changed_anything(self) -> bool:
        return bool(self.proposals)


class _Purse:
    """What the run may still spend. A 402 stops it whatever is left."""

    def __init__(self, allowance: Decimal):
        self.left = allowance
        self.spent = Decimal(0)
        self.stopped: str | None = None

    def take(self, cost: Decimal | None) -> None:
        if cost:
            self.spent += cost
            self.left -= cost

    @property
    def empty(self) -> bool:
        return self.stopped is not None or self.left <= 0


async def _attempt(
    client: OpenRouterClient,
    candidate: VerifiedCandidate,
    g: GoldenEvent,
    gate: asyncio.Semaphore,
    purse: _Purse,
) -> Attempt:
    task = TIER_TASK[candidate.tier]
    subject = subject_from_record(g.event_id, g.record)
    if task.name is TaskName.SEVERITY:
        subject = blinded(subject)
    async with gate:
        free = candidate.slug.endswith(":free")
        if purse.stopped or (not free and purse.left <= 0):
            return Attempt(g.event_id, "skipped")
        try:
            completion = await client.trial(
                candidate,
                schema_name=task.schema_name,
                schema=task.schema,
                messages=messages(task, subject),
                max_tokens=task.max_tokens,
                # No event_id: the gauntlet's spend is RIPPERDOC's, not the event's.
                attribution=Attribution(agent=AGENT, stage=STAGE),
            )
        except InvalidOutput as exc:
            purse.take(exc.cost_usd)
            return Attempt(
                g.event_id,
                "refused" if exc.refused else "invalid",
                duration_ms=exc.duration_ms,
                cost_usd=exc.cost_usd,
            )
        except BudgetExhausted:
            purse.stopped = "OpenRouter turned a call away for budget (HTTP 402)"
            return Attempt(g.event_id, "error")
        except CallFailed:
            return Attempt(g.event_id, "error")
    purse.take(completion.cost_usd)
    try:
        result = task.parse(completion.data, subject)
    except (Rejected, KeyError, TypeError, ValueError):
        return Attempt(g.event_id, "rejected", None, completion.duration_ms, completion.cost_usd)
    return Attempt(
        g.event_id, "ok", score(result, g.labels), completion.duration_ms, completion.cost_usd
    )


@dataclass
class _Run:
    client: OpenRouterClient
    http: httpx.AsyncClient
    golden: list[GoldenEvent]
    golden_digest: str
    purse: _Purse
    now: datetime
    notes: list[str]

    async def model(
        self, candidate: VerifiedCandidate, row: Listed | None, *, incumbent: bool
    ) -> Measured | None:
        if not candidate.slug.endswith(":free"):
            expected = estimate(row, len(self.golden))
            if expected > self.purse.left:
                self.notes.append(
                    f"{candidate.tier.value}: {candidate.slug} not tried: it should cost about "
                    f"${expected:.4f}, and ${max(self.purse.left, Decimal(0)):.4f} is left"
                )
                return None
        gate = asyncio.Semaphore(CONCURRENCY)
        attempts = await asyncio.gather(
            *(_attempt(self.client, candidate, g, gate, self.purse) for g in self.golden)
        )
        m = measure(
            candidate.tier,
            candidate.slug,
            attempts,
            incumbent=incumbent,
            measured_at=self.now,
            golden_digest=self.golden_digest,
            note=self.purse.stopped,
        )
        logger.info(
            "gauntlet: %s on %s: %s",
            candidate.slug,
            candidate.tier.value,
            "; ".join(failures(m)) or f"clears every gate, agreement {m.agreement:.0%}",
        )
        return m

    async def tier(
        self,
        tier: Tier,
        verified: VerifiedLadder,
        previous: Sequence[Measured],
        listed: Sequence[Listed],
    ) -> tuple[list[Measured], Decision | None]:
        rows = {r.slug: r for r in listed}
        slug = verified.ladder.tiers[tier][0]
        reused = next(
            (m for m in previous if m.tier == tier.value and m.slug == slug and m.complete), None
        )
        out: list[Measured] = []
        if reused is not None:
            incumbent: Measured | None = dataclasses.replace(reused, reused=True)
        else:
            try:
                candidate = await verify_candidate(slug, tier, client=self.http)
            except LadderUnusable as exc:
                # Nothing to compare a challenger against, so nothing is spent on one.
                self.notes.append(f"{tier.value}: {slug} not tried: {exc}")
                return [], None
            incumbent = await self.model(candidate, rows.get(slug), incumbent=True)
        if incumbent is None:
            return [], None
        out.append(incumbent)

        # Every model the tier names, dropped or not, is already the ladder's, not a challenger.
        named = {
            *verified.ladder.tiers[tier],
            *(b.slug for b in verified.dropped if b.tier is tier),
        }
        found: list[VerifiedCandidate] = []
        for row in challengers(listed, tier, named, self.now)[:MAX_VERIFIED_PER_TIER]:
            if len(found) == CHALLENGERS_PER_TIER:
                break
            try:
                found.append(await verify_candidate(row.slug, tier, client=self.http))
            except LadderRejected:
                continue
            except CatalogueUnavailable as exc:
                self.notes.append(f"{tier.value}: challengers not checked: {exc}")
                break
        for candidate in found:
            if self.purse.stopped:
                break
            m = await self.model(candidate, rows.get(candidate.slug), incumbent=False)
            if m is not None:
                out.append(m)
        return out, decide(tier, incumbent, [m for m in out if not m.incumbent])


def _record(
    conn: Connection,
    *,
    started: datetime,
    finished: datetime,
    golden_digest: str,
    spent: Decimal,
    results: Sequence[Measured],
    note: str | None,
    decisions: Sequence[Decision],
    golden_size: int,
    reviewed: int,
    au_labels: tuple[int, int],
) -> tuple[int, list[str]]:
    run_id = db.record_gauntlet(
        conn,
        started_at=started,
        finished_at=finished,
        golden_digest=golden_digest,
        spent_usd=spent,
        results=results,
        note=note,
    )
    titles = []
    for d in decisions:
        title = proposal_title(d)
        made = db.propose(
            conn,
            title=title,
            body=proposal_body(d, golden_size=golden_size, reviewed=reviewed, au_labels=au_labels),
            payload=proposal_payload(d, gauntlet_run=run_id),
            now=finished,
        )
        if made is not None:
            titles.append(title)
    return run_id, titles


async def run_gauntlet(
    *,
    engine: Engine | None = None,
    settings: Settings | None = None,
    now: datetime | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> GauntletSummary:
    """Put each enrichment tier's default and its challengers through the golden set."""
    settings = settings or get_settings()
    engine = engine or get_engine()
    now = now or _utcnow()
    key = settings.openrouter_api_key
    if key is None:
        return GauntletSummary(skipped="OPENROUTER_API_KEY is not set")

    golden, reviewed = await asyncio.to_thread(_in, engine, _golden, now)
    if not golden:
        return GauntletSummary(skipped="no event qualifies for the golden set yet")
    listed = await asyncio.to_thread(_in, engine, db.load_listed)
    if not listed:
        return GauntletSummary(skipped="the model scan has not run yet")
    golden_digest = digest(golden)
    start, end = month_bounds(now)
    spent_month = await asyncio.to_thread(_in, engine, db.gauntlet_spend, start, end)
    previous = await asyncio.to_thread(
        _in, engine, db.recent_results, golden_digest, now - INCUMBENT_REUSE
    )

    summary = GauntletSummary()
    async with _client(settings, transport) as http:
        try:
            status = await fetch_key_status(http, key, settings.user_agent)
        except BudgetUnreadable as exc:
            return GauntletSummary(skipped=f"no budget reading: {exc}")
        reading = assess(status, settings.ai_monthly_budget_usd)
        tiers = list(TIERS_BY_MODE[reading.mode])
        if not tiers:
            return GauntletSummary(skipped=f"the budget mode is {reading.mode}: {reading.reason}")
        needed = len(golden) * (1 + CHALLENGERS_PER_TIER) + FREE_REQUEST_MARGIN
        free_left = status.free_requests_remaining
        if Tier.FREE in tiers and free_left is not None and free_left < needed:
            tiers.remove(Tier.FREE)
            summary.notes.append(f"tier0_free not tried: {free_left} free requests left today")
        allowance = min(RUN_CAP_USD, MONTHLY_CAP_USD - spent_month)
        if allowance <= 0 and set(tiers) - {Tier.FREE}:
            tiers = [t for t in tiers if t is Tier.FREE]
            summary.notes.append(
                f"paid tiers not tried: the month's ${MONTHLY_CAP_USD} for the gauntlet is spent"
            )
        if not tiers:
            return GauntletSummary(skipped="; ".join(summary.notes))
        try:
            verified = await verify_ladder(client=http)
        except LadderUnusable as exc:
            return GauntletSummary(skipped=f"the ladder did not pass: {exc}")

        client = OpenRouterClient(
            verified, key, http, functools.partial(record_to, engine), settings
        )
        run = _Run(client, http, golden, golden_digest, _Purse(allowance), now, summary.notes)
        decisions: list[Decision] = []
        for tier in tiers:
            if run.purse.stopped:
                break
            measured, decision = await run.tier(tier, verified, previous, listed)
            summary.measured += measured
            if decision is not None:
                decisions.append(decision)
        if run.purse.stopped:
            summary.notes.append(run.purse.stopped)

    summary.spent_usd = run.purse.spent
    summary.run_id, summary.proposals = await asyncio.to_thread(
        _in,
        engine,
        _record,
        started=now,
        finished=_utcnow(),
        golden_digest=golden_digest,
        spent=run.purse.spent,
        results=summary.measured,
        note="; ".join(summary.notes) or None,
        decisions=decisions,
        golden_size=len(golden),
        reviewed=reviewed,
        au_labels=au_label_counts(golden),
    )
    logger.info(
        "gauntlet: %d results, %d new proposals, $%.4f spent%s",
        len(summary.measured),
        len(summary.proposals),
        summary.spent_usd,
        f"; {'; '.join(summary.notes)}" if summary.notes else "",
    )
    return summary
