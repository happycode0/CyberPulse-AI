"""Waking the crew's routines only when there is work for them.

The Follow-up and Source discovery routines in Paperclip ran on a timer until 2026-10-30, and a
timer wakes an agent whether or not there is anything to do: 13 of 15 follow-up sweeps and 20
of 22 story follow-ups found nothing, and each spent tokens finding that out. They are webhook
routines now, and this pass, every hour, fires each one only when the worker's own data says
there is work:

- **Follow-up** (DECKARD). First the pass records `no_change` itself on every due check whose
  event has gained nothing since its last check: DECKARD's skill sent exactly that, without a
  read. Then, if tasks are still due and the day's cap on DECKARD's reports has room, it fires
  the routine, at most every FOLLOWUP_EVERY.
- **Source discovery** (TACHIKOMA). When the gate has read the home page of a found site and
  found no feed in it, since the routine was last fired, at most every DISCOVERY_EVERY. That is
  the one thing the worker cannot do itself: name a feed it could not find.

Each fire is claimed in `notifications` (kind `routine_wake`, channel `paperclip`) under a key
for the hour, and carries that key as its Idempotency-Key, so a wake is never fired twice. What
it carries is counts and where to read: the agent reads the rest from the ops API. A routine
whose URL or secret is not set is never fired; the quiet checks are recorded anyway.
"""

import asyncio
import functools
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import Engine

from worker.db.followup import close_quiet_checks, count_accepted, count_due
from worker.db.notifications import claim, last_fired, settle
from worker.db.wakes import WaitingSites, waiting_sites as _waiting_sites
from worker.notify.telegram import SendResult
from worker.pipeline.status import FollowupConfig
from worker.settings import Settings
from worker.watchdog.paperclip import RoutineWebhook, discovery_routine, followup_routine

logger = logging.getLogger(__name__)

# The old timer's cadence: four sweeps a day of `batch` tasks each is the day's cap
# (config/followup.yaml), so DECKARD is never woken more often than the cap can use.
FOLLOWUP_EVERY = timedelta(hours=6)
# TACHIKOMA's old nightly run: the search runs once a night, so its finds come once a day.
DISCOVERY_EVERY = timedelta(hours=24)
FOLLOWUP_KEY = "routine:follow-up:"
DISCOVERY_KEY = "routine:source-discovery:"
# GET /ops/followup counts the day's reports over the last 24 hours, as this does.
FOLLOWUP_DAY = timedelta(hours=24)

_FROM_SETTINGS: Any = object()


@dataclass(frozen=True)
class FollowupState:
    due: int  # tasks due now, after the quiet checks were recorded
    room: int  # reports DECKARD may still make in the last 24 hours
    batch: int  # the most one sweep hands out


@dataclass
class WakeSummary:
    quiet_recorded: int = 0
    followup: str = "not set up"
    discovery: str = "not set up"


def close_quiet(engine: Engine, *, now: datetime) -> list[str]:
    with engine.begin() as conn:
        return close_quiet_checks(conn, now=now)


def followup_state(engine: Engine, *, now: datetime) -> FollowupState:
    rule = FollowupConfig.load().followup
    with engine.connect() as conn:
        due = count_due(conn, now=now)
        accepted = count_accepted(conn, since=now - FOLLOWUP_DAY)
    return FollowupState(due=due, room=max(0, rule.per_day - accepted), batch=rule.batch)


def waiting_sites(engine: Engine, *, since: datetime) -> WaitingSites:
    return _waiting_sites(engine, since=since)


def followup_payload(state: FollowupState) -> dict[str, Any]:
    return {
        "routine": "follow-up",
        "due": state.due,
        "batch": min(state.batch, state.room),
        "read": "GET /ops/followup",
    }


def discovery_payload(sites: WaitingSites) -> dict[str, Any]:
    return {
        "routine": "source-discovery",
        "waiting_for_a_feed": sites.tried,
        "new_since_last_wake": sites.new,
        "read": "GET /ops/candidates",
    }


async def _fire(
    engine: Engine, routine: RoutineWebhook, payload: dict[str, Any], key: str, now: datetime
) -> str:
    claimed = await asyncio.to_thread(
        claim, engine, kind="routine_wake", key=key, now=now, channel="paperclip"
    )
    if claimed is None:
        return "already claimed"
    try:
        result = await routine.fire(payload, idempotency_key=key)
    except Exception as exc:  # noqa: BLE001 - a claim is always settled
        result = SendResult(False, error=type(exc).__name__)
    outcome = "sent" if result.sent else "withheld" if result.withheld else "failed"
    await asyncio.to_thread(settle, engine, claimed, outcome, now=now, error=result.error)
    if outcome == "sent":
        logger.info("fired the %s routine (%s)", routine.name, key)
        return "fired"
    logger.warning("%s routine not fired: %s: %s", routine.name, outcome, result.error)
    return outcome


async def _wake_followup(engine: Engine, routine: RoutineWebhook, now: datetime) -> str:
    state = await asyncio.to_thread(followup_state, engine, now=now)
    if state.due == 0:
        return "nothing due"
    if state.room == 0:
        return "day's cap reached"
    last = await asyncio.to_thread(last_fired, engine, prefix=FOLLOWUP_KEY)
    if last is not None and now - last < FOLLOWUP_EVERY:
        return "fired recently"
    key = f"{FOLLOWUP_KEY}{now:%Y-%m-%dT%H}"
    return await _fire(engine, routine, followup_payload(state), key, now)


async def _wake_discovery(engine: Engine, routine: RoutineWebhook, now: datetime) -> str:
    last = await asyncio.to_thread(last_fired, engine, prefix=DISCOVERY_KEY)
    if last is not None and now - last < DISCOVERY_EVERY:
        return "fired recently"
    sites = await asyncio.to_thread(
        waiting_sites, engine, since=last if last is not None else now - DISCOVERY_EVERY
    )
    if sites.new == 0:
        return "no new sites"
    key = f"{DISCOVERY_KEY}{now:%Y-%m-%dT%H}"
    return await _fire(engine, routine, discovery_payload(sites), key, now)


async def _guarded(name: str, wake: functools.partial) -> str:
    try:
        return await wake()
    except Exception:
        logger.exception("the %s routine wake failed", name)
        return "error"


async def wake_routines(
    engine: Engine,
    settings: Settings,
    *,
    now: datetime,
    followup: RoutineWebhook | None = _FROM_SETTINGS,
    discovery: RoutineWebhook | None = _FROM_SETTINGS,
) -> WakeSummary:
    """One pass: record the quiet checks, then fire each routine there is work for. Never
    raises for one routine: the other is still tried, and the next pass is an hour away."""
    if followup is _FROM_SETTINGS:
        followup = followup_routine(settings)
    if discovery is _FROM_SETTINGS:
        discovery = discovery_routine(settings)
    summary = WakeSummary()
    closed = await asyncio.to_thread(close_quiet, engine, now=now)
    summary.quiet_recorded = len(closed)
    if closed:
        logger.info("follow-up: recorded no_change on %d quiet checks", len(closed))
    if followup is not None:
        summary.followup = await _guarded(
            "follow-up", functools.partial(_wake_followup, engine, followup, now)
        )
    if discovery is not None:
        summary.discovery = await _guarded(
            "source discovery", functools.partial(_wake_discovery, engine, discovery, now)
        )
    return summary
