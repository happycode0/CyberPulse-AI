"""One enrichment pass: pending events get their triage, brief and severity calls (PLAN.md §7).

The pass is the AI layer's only caller in the worker. Before it spends anything:

1. The ladder must have passed the price guard within the last day.
2. A fresh budget reading must say what mode the month's money allows (ROGUE, §7.4).

Every call then asks the governor where it may go. Collection and publishing never wait on
any of this: with no key, a failed guard or no money, the pass returns without calling, and
events stay `pending_enrichment`, which the site already shows.

Each event's tasks run in order, but independently: a brief that fails does not stop the
severity judgment. Up to `MAX_CONCURRENT_EVENTS` events are in flight at once. The log
carries counts and our own reasons, never model output.
"""

import asyncio
import functools
import logging
from collections import Counter
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
from sqlalchemy import Engine

from worker.ai.budget import BudgetUnreadable, Governor, Mode, Route, Work, fetch_key_status
from worker.ai.client import (
    Attribution,
    BudgetExhausted,
    CallFailed,
    InvalidOutput,
    OpenRouterClient,
    RateLimited,
)
from worker.ai.ladder import LadderUnusable, Tier, VerifiedLadder, verify_ladder
from worker.ai.tasks import (
    TASKS,
    Brief,
    Rejected,
    Result,
    SeverityJudgment,
    Subject,
    Task,
    TaskName,
    Triage,
    messages,
)
from worker.db.enrichment import (
    TaskState,
    apply_brief,
    apply_severity,
    apply_triage,
    due_event_ids,
    load_subjects,
    mark_done,
    mark_failed,
    maybe_complete,
    task_states,
)
from worker.db.ledger import record_to
from worker.db.session import get_engine
from worker.models import EventStatus
from worker.pipeline.run import DEFAULT_SCORING_PATH, rescore
from worker.pipeline.score import ScoringConfig
from worker.publish.build import LIVE_MIN_PROMINENCE
from worker.settings import Settings, get_settings
from worker.version import ENRICHMENT_VERSION

logger = logging.getLogger(__name__)

DEFAULT_BATCH = 10
MAX_CONCURRENT_EVENTS = 3
# Candidates read per event enriched, so a batch can skip events whose tasks all wait.
CANDIDATE_FACTOR = 5
# The cost ledger's `agent` for calls the worker makes itself (PLAN.md §7.7).
AGENT = "worker"
# How long a ladder that passed the guard stays trusted before it is checked again. Prices
# and routes change, and a model can lose its last capable route overnight.
LADDER_MAX_AGE = timedelta(hours=24)
# Calls per task per pass, counting the ones the budget turned away.
MAX_CALLS = 3
# Unusable answers per task per pass: the first gets one retry (§11), the second fails.
MAX_BAD_ANSWERS = 2


def _utcnow() -> datetime:
    return datetime.now(UTC)


# ─── One task ─────────────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Done:
    result: Result
    model: str | None


@dataclass(frozen=True)
class Failed:
    """Nothing usable came back. Recorded, and retried after a backoff."""

    reason: str
    model: str | None = None


@dataclass(frozen=True)
class Waiting:
    """Not attempted, or stopped by the budget or an outage. Nothing is recorded, so the next
    pass tries again."""

    reason: str


Outcome = Done | Failed | Waiting


def _work(task: Task, subject: Subject) -> Work:
    e = subject.event
    return Work(
        task.tier,
        e.severity,
        kev=any(c.kev.listed for c in e.cves),
        developing=e.status is EventStatus.DEVELOPING,
    )


def _route(governor: Governor, task: Task, work: Work) -> Route | Waiting:
    route = governor.route(work)
    if route is None:
        return Waiting(f"the {governor.mode()[0]} budget mode has no route for it")
    if task.editorial and route.tier is Tier.FREE:
        # §7.1: tier 0 is mechanical work only.
        return Waiting("only tier 0 is open, and the task is editorial")
    return route


def _status(exc: CallFailed) -> str:
    return f"HTTP {exc.status}" if exc.status is not None else "no response"


async def run_task(
    client: OpenRouterClient, governor: Governor, task: Task, subject: Subject
) -> Outcome:
    """Call `task` for `subject` and check the answer. Never raises for a failed call.

    An exception that is not a failed call, such as a ledger write that failed, propagates:
    a call that cannot be accounted for is one the worker must stop making.
    """
    work = _work(task, subject)
    attribution = Attribution(agent=AGENT, stage=task.stage, event_id=subject.event.event_id)
    prompt = messages(task, subject)
    outcome: Failed | Waiting = Waiting("no call was made")
    bad = 0
    for _ in range(MAX_CALLS):
        route = _route(governor, task, work)
        if isinstance(route, Waiting):
            return route
        try:
            completion = await client.complete(
                route.tier,
                schema_name=task.schema_name,
                schema=task.schema,
                messages=prompt,
                max_tokens=task.max_tokens,
                attribution=attribution,
                models=route.models,
            )
        except (BudgetExhausted, RateLimited) as exc:
            # The governor tightens, and the next turn of the loop asks it again.
            governor.on_failure(exc, route)
            outcome = Waiting(f"OpenRouter turned the call away ({_status(exc)})")
            continue
        except InvalidOutput as exc:
            reason, model = "the answer did not match the schema", exc.model
        except CallFailed as exc:
            if exc.status is None or exc.status >= 500 or exc.status == 408:
                return Waiting(f"the call failed ({_status(exc)})")
            # A 4xx: the request itself is wrong, and sending it again will not fix it.
            return Failed(f"OpenRouter refused the request ({_status(exc)})")
        else:
            try:
                return Done(task.parse(completion.data, subject), completion.model)
            except Rejected as exc:
                reason, model = str(exc), completion.model
            except (KeyError, TypeError, ValueError):
                reason, model = "the answer could not be read", completion.model
        bad += 1
        outcome = Failed(reason, model)
        if bad >= MAX_BAD_ANSWERS:
            break
    return outcome


# ─── The AI layer's state between passes ──────────────────────────────────────────────────────────


class AiLayer:
    """The verified ladder and ROGUE's governor, kept from one pass to the next.

    The governor has to outlive a pass: a 402 that stopped paid calls, or an account out of
    credits, must still hold on the next one.
    """

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        verify: Callable[[], Awaitable[VerifiedLadder]] = verify_ladder,
        clock: Callable[[], datetime] = _utcnow,
    ):
        self.settings = settings or get_settings()
        self._verify = verify
        self._clock = clock
        self.verified: VerifiedLadder | None = None
        self.governor: Governor | None = None

    def adopt(self, verified: VerifiedLadder | None) -> None:
        """Take a ladder checked elsewhere — the daily model scan (worker/ai/catalogue.py) — or
        None when it did not pass, which makes the next pass check again before it calls."""
        self.verified = verified
        if verified is None:
            return
        for breach in verified.dropped:
            logger.warning("model ladder: %s is dropped for now", breach)
        if self.governor is not None:
            self.governor.adopt(verified)

    async def ready(self, http: httpx.AsyncClient) -> Governor | None:
        """The governor, with the ladder checked and the budget read, or None if the AI layer is
        off: no key, or a ladder that does not pass."""
        key = self.settings.openrouter_api_key
        if key is None:
            logger.info("enrichment: OPENROUTER_API_KEY is not set, so the AI layer is off")
            return None
        if self.verified is None or self._clock() - self.verified.checked_at > LADDER_MAX_AGE:
            try:
                verified = await self._verify()
            except LadderUnusable as exc:
                self.verified = None
                logger.error("enrichment: the model ladder did not pass, so nothing runs: %s", exc)
                return None
            self.adopt(verified)
        assert self.verified is not None
        if self.governor is None:
            self.governor = Governor(
                self.verified, self.settings.ai_monthly_budget_usd, now=self._clock
            )
        try:
            self.governor.observe(await fetch_key_status(http, key, self.settings.user_agent))
        except BudgetUnreadable as exc:
            # The last reading stands until it is stale; then only free models run.
            logger.warning("enrichment: no budget reading this pass: %s", exc)
        return self.governor


# ─── One pass ─────────────────────────────────────────────────────────────────────────────────────


@dataclass
class EnrichSummary:
    mode: str | None = None  # the budget mode at the start; None when the AI layer is off
    events: int = 0
    blocked: int = 0  # candidates passed over because the budget lets none of their tasks run
    done: Counter[str] = field(default_factory=Counter)  # per task
    failed: int = 0  # tasks
    waiting: int = 0  # tasks
    completed: int = 0  # events that left pending_enrichment
    dropped_names: int = 0  # entity names a model gave that the record does not contain
    rescored: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def changed_anything(self) -> bool:
        """Whether anything the site publishes moved, and so whether to republish."""
        return bool(sum(self.done.values()) or self.completed)


def _is_done(state: TaskState | None) -> bool:
    return state is not None and state.version == ENRICHMENT_VERSION and state.status == "done"


def _due_tasks(subject: Subject, states: Mapping[str, TaskState]) -> list[Task]:
    return [t for t in TASKS if t.applies(subject) and not _is_done(states.get(t.name))]


def _load_due(
    engine: Engine, now: datetime, limit: int
) -> tuple[list[tuple[Subject, list[Task]]], int]:
    """Candidates with the tasks each still needs, and how many were already complete.

    An event can be pending with nothing left to do: an official score arrived while its
    severity judgment was waiting, so severity no longer applies. It is completed here.
    """
    with engine.begin() as conn:
        ids = due_event_ids(
            conn, version=ENRICHMENT_VERSION, now=now, floor=LIVE_MIN_PROMINENCE, limit=limit
        )
        states = task_states(conn, ids)
        due, settled = [], 0
        for subject in load_subjects(conn, ids):
            tasks = _due_tasks(subject, states.get(subject.event.event_id, {}))
            if tasks:
                due.append((subject, tasks))
            elif maybe_complete(conn, subject.event.event_id, version=ENRICHMENT_VERSION):
                settled += 1
    return due, settled


_APPLY = {
    TaskName.TRIAGE: apply_triage,
    TaskName.BRIEF: apply_brief,
}


def _write(engine: Engine, task: Task, subject: Subject, outcome: Outcome, now: datetime) -> None:
    """One task's answer and its bookkeeping, in one transaction."""
    event_id = subject.event.event_id
    with engine.begin() as conn:
        if isinstance(outcome, Done):
            result = outcome.result
            if isinstance(result, SeverityJudgment):
                apply_severity(
                    conn, subject, result, model=outcome.model, version=ENRICHMENT_VERSION
                )
            else:
                assert isinstance(result, Triage | Brief)
                _APPLY[task.name](conn, subject, result)
            mark_done(
                conn, event_id, task.name, version=ENRICHMENT_VERSION, model=outcome.model, now=now
            )
        elif isinstance(outcome, Failed):
            mark_failed(
                conn,
                event_id,
                task.name,
                version=ENRICHMENT_VERSION,
                model=outcome.model,
                reason=outcome.reason,
                now=now,
            )


def _complete(engine: Engine, event_id: str) -> bool:
    with engine.begin() as conn:
        return maybe_complete(conn, event_id, version=ENRICHMENT_VERSION)


async def _enrich_event(
    client: OpenRouterClient,
    governor: Governor,
    engine: Engine,
    subject: Subject,
    tasks: Sequence[Task],
    now: datetime,
    summary: EnrichSummary,
    touched: set[str],
) -> None:
    event_id = subject.event.event_id
    for task in tasks:
        outcome = await run_task(client, governor, task, subject)
        await asyncio.to_thread(_write, engine, task, subject, outcome, now)
        match outcome:
            case Done(result=result):
                summary.done[task.name] += 1
                touched.add(event_id)
                if isinstance(result, Triage):
                    summary.dropped_names += result.dropped
            case Failed(reason=reason):
                summary.failed += 1
                logger.info("enrichment %s %s failed: %s", event_id, task.name, reason)
            case Waiting(reason=reason):
                summary.waiting += 1
                logger.debug("enrichment %s %s waits: %s", event_id, task.name, reason)
    if event_id in touched and await asyncio.to_thread(_complete, engine, event_id):
        summary.completed += 1


async def enrich_pending(
    *,
    engine: Engine | None = None,
    layer: AiLayer | None = None,
    batch: int = DEFAULT_BATCH,
    now: datetime | None = None,
    scoring_path: Path | None = None,
) -> EnrichSummary:
    """Enrich up to `batch` pending events, most prominent first, then rescore what changed.

    `layer` carries the ladder and the governor between passes; the scheduler keeps one.
    `engine`, `now` and `scoring_path` default to the process database, the wall clock and
    `config/scoring.yaml`, as in `run_lane`.
    """
    engine = engine or get_engine()
    layer = layer or AiLayer()
    moment = now if now is not None else _utcnow()
    summary = EnrichSummary()
    touched: set[str] = set()

    async with httpx.AsyncClient() as http:
        governor = await layer.ready(http)
        if governor is None:
            return summary
        mode, why = governor.mode()
        summary.mode = mode.value
        if mode is Mode.OFF:
            logger.info("enrichment: mode off (%s), so nothing is called", why)
            return summary

        candidates, summary.completed = await asyncio.to_thread(
            _load_due, engine, moment, max(batch, 0) * CANDIDATE_FACTOR
        )
        routable = [
            (subject, tasks)
            for subject, tasks in candidates
            if any(not isinstance(_route(governor, t, _work(t, subject)), Waiting) for t in tasks)
        ]
        summary.blocked = len(candidates) - len(routable)
        work = routable[: max(batch, 0)]
        summary.events = len(work)

        assert layer.verified is not None and layer.settings.openrouter_api_key is not None
        client = OpenRouterClient(
            layer.verified,
            layer.settings.openrouter_api_key,
            http,
            functools.partial(record_to, engine),
            layer.settings,
        )
        gate = asyncio.Semaphore(MAX_CONCURRENT_EVENTS)

        async def one(subject: Subject, tasks: Sequence[Task]) -> None:
            async with gate:
                await _enrich_event(
                    client, governor, engine, subject, tasks, moment, summary, touched
                )

        # A TaskGroup, so an error that is not a failed call (a ledger or database write)
        # cancels the events still in flight instead of letting them keep spending.
        async with asyncio.TaskGroup() as group:
            for subject, tasks in work:
                group.create_task(one(subject, tasks))

    if touched:
        scoring = ScoringConfig.load(scoring_path or DEFAULT_SCORING_PATH)
        errors = await asyncio.to_thread(rescore, engine, scoring, touched, now=moment)
        summary.errors.extend(errors)
        summary.rescored = len(touched)

    logger.info(
        "enrichment: mode %s (%s); %d events, %d blocked; done triage=%d brief=%d severity=%d; "
        "failed=%d waiting=%d completed=%d dropped_names=%d rescored=%d errors=%d",
        summary.mode,
        why,
        summary.events,
        summary.blocked,
        summary.done[TaskName.TRIAGE],
        summary.done[TaskName.BRIEF],
        summary.done[TaskName.SEVERITY],
        summary.failed,
        summary.waiting,
        summary.completed,
        summary.dropped_names,
        summary.rescored,
        len(summary.errors),
    )
    return summary
