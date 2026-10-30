"""The routine wakes (worker/notify/routines.py) without a database: when the worker fires the
crew's Follow-up and Source discovery routines, and what it records itself. The SQL is exercised
against Postgres in tests/integration/test_followup_db.py."""

from datetime import UTC, datetime, timedelta

import pytest

from worker.db.notifications import Claim
from worker.notify import routines
from worker.notify.routines import (
    DISCOVERY_EVERY,
    FOLLOWUP_EVERY,
    FollowupState,
    WaitingSites,
    wake_routines,
)
from worker.notify.telegram import SendResult
from worker.settings import Settings

NOW = datetime(2026, 10, 30, 6, 15, tzinfo=UTC)
HOUR = timedelta(hours=1)


class FakeRoutine:
    name = "fake"

    def __init__(self, result=None):
        self.fired: list[tuple[dict, str | None]] = []
        self.result = result or SendResult(True)

    async def fire(self, payload, *, idempotency_key=None) -> SendResult:
        self.fired.append((payload, idempotency_key))
        return self.result


@pytest.fixture
def db(monkeypatch):
    """The database stood in for: the queue, the found sites, and the wakes claimed."""
    state = {
        "quiet": ["evt-1", "evt-2"],
        "followup": FollowupState(due=3, room=20, batch=5),
        "sites": WaitingSites(tried=4, new=2),
        "last": {},  # key prefix -> when it last fired
        "claimed": {},
        "settled": [],
        "closed_with": [],
    }

    def close_quiet(engine, *, now):
        state["closed_with"].append(now)
        return list(state["quiet"])

    def followup_state(engine, *, now):
        return state["followup"]

    def waiting_sites(engine, *, since):
        state["since"] = since
        return state["sites"]

    def last_fired(engine, *, prefix):
        return state["last"].get(prefix)

    def claim(engine, *, kind, key, now, channel):
        assert (kind, channel) == ("routine_wake", "paperclip")
        if key in state["claimed"]:
            return None
        state["claimed"][key] = now
        return Claim(len(state["claimed"]), 1)

    def settle(engine, claimed, outcome, *, now, error=None):
        state["settled"].append((claimed.id, outcome, error))

    for name, fake in [("close_quiet", close_quiet), ("followup_state", followup_state),
                       ("waiting_sites", waiting_sites), ("last_fired", last_fired),
                       ("claim", claim), ("settle", settle)]:
        monkeypatch.setattr(routines, name, fake)
    return state


def settings() -> Settings:
    return Settings(_env_file=None, database_url="postgresql://x")


async def wake(followup=None, discovery=None, *, now=NOW):
    return await wake_routines(None, settings(), now=now, followup=followup, discovery=discovery)


async def test_quiet_checks_are_recorded_even_with_no_routine_set_up(db):
    """DECKARD's skill said: nothing_new is true, report no_change straight away, with no read.
    The worker does that itself now, so nobody is woken to say it."""
    summary = await wake()
    assert summary.quiet_recorded == 2 and db["closed_with"] == [NOW]
    assert summary.followup == "not set up" and summary.discovery == "not set up"
    assert db["claimed"] == {}


async def test_the_follow_up_fires_when_tasks_with_news_are_due(db):
    crew = FakeRoutine()
    summary = await wake(followup=crew)
    assert summary.followup == "fired"
    [(payload, key)] = crew.fired
    assert payload == {"routine": "follow-up", "due": 3, "batch": 5, "read": "GET /ops/followup"}
    assert key == "routine:follow-up:2026-10-30T06" and list(db["claimed"]) == [key]
    assert db["settled"] == [(1, "sent", None)]


async def test_the_follow_up_is_not_fired_twice_in_its_interval(db):
    crew = FakeRoutine()
    db["last"]["routine:follow-up:"] = NOW - FOLLOWUP_EVERY + HOUR
    assert (await wake(followup=crew)).followup == "fired recently"
    db["last"]["routine:follow-up:"] = NOW - FOLLOWUP_EVERY
    assert (await wake(followup=crew)).followup == "fired"
    assert len(crew.fired) == 1


@pytest.mark.parametrize(
    "state, said",
    [
        (FollowupState(due=0, room=20, batch=5), "nothing due"),
        (FollowupState(due=4, room=0, batch=5), "day's cap reached"),
    ],
)
async def test_the_follow_up_is_not_fired_with_nothing_to_hand_out(db, state, said):
    crew = FakeRoutine()
    db["followup"] = state
    assert (await wake(followup=crew)).followup == said
    assert crew.fired == [] and db["claimed"] == {}


async def test_the_batch_is_what_the_day_has_left(db):
    crew = FakeRoutine()
    db["followup"] = FollowupState(due=9, room=2, batch=5)
    await wake(followup=crew)
    assert crew.fired[0][0]["batch"] == 2


async def test_a_wake_already_claimed_this_hour_is_not_fired_again(db):
    crew = FakeRoutine()
    db["claimed"]["routine:follow-up:2026-10-30T06"] = NOW
    assert (await wake(followup=crew)).followup == "already claimed"
    assert crew.fired == []


async def test_a_failed_fire_is_settled_as_failed(db):
    crew = FakeRoutine(SendResult(False, error="HTTP 503"))
    summary = await wake(followup=crew)
    assert summary.followup == "failed" and db["settled"] == [(1, "failed", "HTTP 503")]


async def test_discovery_fires_for_sites_found_since_its_last_wake(db):
    finder = FakeRoutine()
    db["last"]["routine:source-discovery:"] = NOW - DISCOVERY_EVERY - HOUR
    summary = await wake(discovery=finder)
    assert summary.discovery == "fired" and db["since"] == NOW - DISCOVERY_EVERY - HOUR
    [(payload, key)] = finder.fired
    assert payload == {
        "routine": "source-discovery", "waiting_for_a_feed": 4, "new_since_last_wake": 2,
        "read": "GET /ops/candidates",
    }
    assert key == "routine:source-discovery:2026-10-30T06"


async def test_discovery_never_fired_counts_the_last_day(db):
    await wake(discovery=FakeRoutine())
    assert db["since"] == NOW - DISCOVERY_EVERY


async def test_discovery_waits_a_day_and_for_new_sites(db):
    finder = FakeRoutine()
    db["last"]["routine:source-discovery:"] = NOW - 3 * HOUR
    assert (await wake(discovery=finder)).discovery == "fired recently"
    db["last"].clear()
    db["sites"] = WaitingSites(tried=4, new=0)
    assert (await wake(discovery=finder)).discovery == "no new sites"
    assert finder.fired == []


async def test_one_routine_failing_does_not_stop_the_other(db, monkeypatch):
    def broken(engine, *, now):
        raise RuntimeError("database gone")

    monkeypatch.setattr(routines, "followup_state", broken)
    finder = FakeRoutine()
    summary = await wake(followup=FakeRoutine(), discovery=finder)
    assert summary.followup == "error" and summary.discovery == "fired" and len(finder.fired) == 1


def test_the_payloads_carry_no_evidence_only_counts_and_where_to_read():
    """What reaches Paperclip is counts and an endpoint: the agent reads the rest from the ops
    API, so nothing fetched from the open web goes through Paperclip."""
    assert set(routines.followup_payload(FollowupState(due=1, room=1, batch=5))) == {
        "routine", "due", "batch", "read"}
    assert set(routines.discovery_payload(WaitingSites(tried=1, new=1))) == {
        "routine", "waiting_for_a_feed", "new_since_last_wake", "read"}
