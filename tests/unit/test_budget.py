"""The AI budget (worker/ai/budget.py): readings, modes, routing, and what failures do to them."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest
from pydantic import SecretStr

from worker.ai.budget import (
    KEY_URL,
    MAX_READING_AGE,
    BudgetUnreadable,
    Governor,
    KeyStatus,
    Mode,
    Route,
    Work,
    assess,
    fetch_key_status,
    parse_key_status,
    plan,
    stricter,
)
from worker.ai.client import BudgetExhausted, CallFailed, RateLimited
from worker.ai.ladder import Ladder, Tier, VerifiedLadder
from worker.models import Severity

KEY = "fake-key-TESTONLY"
BUDGET = Decimal("20")
T0 = datetime(2026, 10, 3, 1, 0, tzinfo=UTC)

LADDER = Ladder.model_validate(
    {
        "tiers": {
            "tier0_free": ["vendor-a/one:free", "vendor-a/two:free", "vendor-a/paid"],
            "tier1_cheap": ["vendor-a/paid", "vendor-b/cheap"],
            "tier2_strong": ["vendor-b/strong", "vendor-b/cheap"],
            "code": ["vendor-c/code"],
            "audit": ["vendor-d/audit"],
        }
    }
)
TIERS = LADDER.tiers


def key_payload(**data):
    """`GET /api/v1/key` as the docs show it, label fragment and all."""
    base = {
        "label": "fake-label-TESTONLY...890",
        "limit": 20,
        "limit_remaining": 20,
        "limit_reset": "monthly",
        "include_byok_in_limit": False,
        "usage": 0,
        "usage_daily": 0,
        "usage_weekly": 0,
        "usage_monthly": 0,
        "is_free_tier": False,
        "free_model_daily_requests": {"used": 12, "limit": 1000, "remaining": 988},
    }
    base.update(data)
    return {"data": base}


def status(usage_monthly="0", limit_remaining="20", free_left=988):
    return KeyStatus(
        limit=Decimal("20"),
        limit_remaining=None if limit_remaining is None else Decimal(limit_remaining),
        limit_reset="monthly",
        usage_daily=Decimal("0"),
        usage_monthly=Decimal(usage_monthly),
        free_requests_remaining=free_left,
    )


# ─── Reading the key ──────────────────────────────────────────────────────────────────────────────


def test_a_key_reading_is_parsed_and_its_label_is_dropped():
    s = parse_key_status(key_payload(usage_daily=0.25, usage_monthly=3.5, limit_remaining=16.5))
    assert (s.usage_daily, s.usage_monthly) == (Decimal("0.25"), Decimal("3.5"))
    assert (s.limit, s.limit_remaining) == (Decimal(20), Decimal("16.5"))
    assert s.limit_reset == "monthly"
    assert s.free_requests_remaining == 988
    assert "fake-label" not in repr(s)


def test_a_key_without_a_limit_reads_as_none():
    s = parse_key_status(key_payload(limit=None, limit_remaining=None, limit_reset=None))
    assert s.limit is None and s.limit_remaining is None and s.limit_reset is None


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"data": "nope"},
        key_payload(usage_monthly=None),
        key_payload(usage_monthly="lots"),
        key_payload(usage_daily="NaN"),
        key_payload(limit_remaining=True),
    ],
)
def test_an_unreadable_key_reading_raises(payload):
    with pytest.raises(BudgetUnreadable):
        parse_key_status(payload)


async def test_fetching_sends_the_key_and_parses_the_answer(respx_mock):
    route = respx_mock.get(KEY_URL).mock(return_value=httpx.Response(200, json=key_payload()))
    async with httpx.AsyncClient() as http:
        s = await fetch_key_status(http, SecretStr(KEY), "CyberPulse-AI/1.0")
    assert s.usage_monthly == 0
    assert route.calls.last.request.headers["authorization"] == f"Bearer {KEY}"


@pytest.mark.parametrize(
    "response, match",
    [
        (httpx.Response(401), "did not accept the key"),
        (httpx.Response(403), "did not accept the key"),
        (httpx.Response(500), "HTTP 500"),
        (httpx.Response(200, text="<html>"), "not JSON"),
    ],
)
async def test_a_failed_fetch_is_unreadable(respx_mock, response, match):
    respx_mock.get(KEY_URL).mock(return_value=response)
    async with httpx.AsyncClient() as http:
        with pytest.raises(BudgetUnreadable, match=match):
            await fetch_key_status(http, SecretStr(KEY), "CyberPulse-AI/1.0")


async def test_a_network_failure_is_unreadable(respx_mock):
    respx_mock.get(KEY_URL).mock(side_effect=httpx.ConnectTimeout("slow"))
    async with httpx.AsyncClient() as http:
        with pytest.raises(BudgetUnreadable, match="ConnectTimeout"):
            await fetch_key_status(http, SecretStr(KEY), "CyberPulse-AI/1.0")


# ─── The §7.4 table ───────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "spent, mode",
    [
        ("0", Mode.FULL),
        ("9.99", Mode.FULL),
        ("10", Mode.CONSERVE),  # exactly 50% left is not "over 50%"
        ("16", Mode.CONSERVE),  # exactly 20% left is not "under 20%"
        ("16.01", Mode.MINIMAL),
        ("19.99", Mode.MINIMAL),
        ("20", Mode.FREE_ONLY),
        ("23", Mode.FREE_ONLY),
    ],
)
def test_the_mode_follows_the_share_of_the_budget_left(spent, mode):
    reading = assess(status(usage_monthly=spent, limit_remaining=None), BUDGET)
    assert reading.mode is mode


def test_a_key_limit_below_the_budget_degrades_early():
    reading = assess(status(usage_monthly="0", limit_remaining="3"), BUDGET)
    assert reading.mode is Mode.MINIMAL and reading.remaining_usd == Decimal("3")
    assert "$3.00 of $20.00" in reading.reason


def test_no_budget_means_off():
    assert assess(status(), Decimal("0")).mode is Mode.OFF


def test_stricter_picks_the_more_degraded_mode():
    assert stricter(Mode.FULL, Mode.MINIMAL, Mode.CONSERVE) is Mode.MINIMAL
    assert stricter(Mode.OFF, Mode.FULL) is Mode.OFF


# ─── Where work goes in each mode ─────────────────────────────────────────────────────────────────

MEDIUM = Work(Tier.STRONG, Severity.MEDIUM)
CRITICAL = Work(Tier.STRONG, Severity.CRITICAL)
KEV = Work(Tier.STRONG, Severity.MEDIUM, kev=True)
HIGH = Work(Tier.STRONG, Severity.HIGH)
DEVELOPING = Work(Tier.CHEAP, Severity.UNKNOWN, developing=True)
TAGGING = Work(Tier.FREE, Severity.LOW)


def where(mode, work, **kw):
    route = plan(mode, work, LADDER, **kw)
    return None if route is None else route.tier


def test_full_gives_every_task_its_own_tier():
    assert where(Mode.FULL, MEDIUM) is Tier.STRONG
    assert where(Mode.FULL, TAGGING) is Tier.FREE
    assert plan(Mode.FULL, MEDIUM, LADDER).models == TIERS[Tier.STRONG]


def test_conserve_keeps_tier_2_for_critical_and_kev_only():
    assert where(Mode.CONSERVE, CRITICAL) is Tier.STRONG
    assert where(Mode.CONSERVE, KEV) is Tier.STRONG
    assert where(Mode.CONSERVE, HIGH) is Tier.CHEAP
    assert where(Mode.CONSERVE, MEDIUM) is Tier.CHEAP
    assert where(Mode.CONSERVE, TAGGING) is Tier.FREE


def test_minimal_is_tier_0_for_the_events_that_matter_most():
    for work in (CRITICAL, KEV, HIGH, DEVELOPING):
        assert where(Mode.MINIMAL, work) is Tier.FREE
    assert where(Mode.MINIMAL, MEDIUM) is None
    assert where(Mode.MINIMAL, TAGGING) is None
    # Tier 0 here still includes its paid tail: MINIMAL is about spending little, not nothing.
    assert plan(Mode.MINIMAL, CRITICAL, LADDER).models == TIERS[Tier.FREE]


def test_free_only_uses_only_free_models():
    route = plan(Mode.FREE_ONLY, CRITICAL, LADDER)
    assert route == Route(Tier.FREE, ("vendor-a/one:free", "vendor-a/two:free"))
    assert where(Mode.FREE_ONLY, MEDIUM) is None


def test_off_routes_nothing():
    for work in (CRITICAL, KEV, TAGGING):
        assert plan(Mode.OFF, work, LADDER) is None


@pytest.mark.parametrize("mode", list(Mode))
def test_code_and_audit_wait_rather_than_degrade_to_tier_0(mode):
    for tier in (Tier.CODE, Tier.AUDIT):
        route = plan(mode, Work(tier, Severity.CRITICAL), LADDER)
        expected = tier if mode in (Mode.FULL, Mode.CONSERVE) else None
        assert (route.tier if route else None) is expected


def test_a_free_cap_hit_steps_tier_0_up_to_tier_1_while_paid_tiers_are_open():
    assert where(Mode.FULL, TAGGING, free_cap_hit=True) is Tier.CHEAP
    assert where(Mode.CONSERVE, TAGGING, free_cap_hit=True) is Tier.CHEAP


def test_a_free_cap_hit_in_minimal_leaves_only_tier_0s_paid_tail():
    route = plan(Mode.MINIMAL, CRITICAL, LADDER, free_cap_hit=True)
    assert route == Route(Tier.FREE, ("vendor-a/paid",))


def test_a_free_cap_hit_in_free_only_leaves_nothing():
    assert plan(Mode.FREE_ONLY, CRITICAL, LADDER, free_cap_hit=True) is None


# ─── The exit test: degradation demonstrably works when the mode is forced (PLAN.md §9) ───────────


def test_forcing_the_mode_degrades_routing_step_by_step():
    clock = [T0]
    work = [CRITICAL, KEV, HIGH, MEDIUM, DEVELOPING, TAGGING]
    seen = {}
    for forced in Mode:
        g = governor(clock, force=forced)
        g.observe(status())  # plenty of money: anything stricter than FULL is the force
        seen[forced] = [(r.tier.value if (r := g.route(w)) else None) for w in work]
    assert seen == {
        Mode.FULL: ["tier2_strong"] * 4 + ["tier1_cheap", "tier0_free"],
        Mode.CONSERVE: ["tier2_strong", "tier2_strong", "tier1_cheap", "tier1_cheap",
                        "tier1_cheap", "tier0_free"],
        Mode.MINIMAL: ["tier0_free"] * 3 + [None, "tier0_free", None],
        Mode.FREE_ONLY: ["tier0_free"] * 3 + [None, "tier0_free", None],
        Mode.OFF: [None] * 6,
    }


# ─── The governor: state between polls ────────────────────────────────────────────────────────────


def governor(clock, force=None):
    verified = VerifiedLadder(ladder=LADDER, checked_at=T0)
    return Governor(verified, BUDGET, force=force, now=lambda: clock[0])


def test_no_reading_yet_means_free_models_only():
    g = governor([T0])
    assert g.mode() == (Mode.FREE_ONLY, "no budget reading yet")


def test_a_reading_sets_the_mode_until_it_goes_stale():
    clock = [T0]
    g = governor(clock)
    g.observe(status())
    assert g.mode()[0] is Mode.FULL
    clock[0] = T0 + MAX_READING_AGE
    assert g.mode()[0] is Mode.FULL
    clock[0] = T0 + MAX_READING_AGE + timedelta(seconds=1)
    assert g.mode() == (Mode.FREE_ONLY, "the last budget reading is stale")


def test_force_can_tighten_but_never_loosen():
    g = governor([T0], force=Mode.MINIMAL)
    g.observe(status())
    assert g.mode() == (Mode.MINIMAL, "forced by the operator")
    g = governor([T0], force=Mode.FULL)
    g.observe(status(usage_monthly="17"))
    assert g.mode()[0] is Mode.MINIMAL


def test_an_in_flight_402_pauses_paid_calls_for_its_retry_after():
    clock = [T0]
    g = governor(clock)
    g.observe(status())
    exc = BudgetExhausted("busy", limit_source="openrouter_in_flight_budget", retry_after=30)
    g.on_failure(exc, Route(Tier.CHEAP, TIERS[Tier.CHEAP]))
    assert g.mode() == (Mode.FREE_ONLY, "paid calls paused until Retry-After")
    clock[0] = T0 + timedelta(seconds=31)
    assert g.mode()[0] is Mode.FULL


@pytest.mark.parametrize("source", ["openrouter_key_limit", None])
def test_a_key_limit_402_stops_paid_calls_until_a_poll_shows_money(source):
    g = governor([T0])
    g.observe(status())
    g.on_failure(
        BudgetExhausted("spent", limit_source=source, retry_after=None),
        Route(Tier.STRONG, TIERS[Tier.STRONG]),
    )
    assert g.mode()[0] is Mode.FREE_ONLY
    assert g.route(CRITICAL).models == ("vendor-a/one:free", "vendor-a/two:free")
    g.observe(status(usage_monthly="1", limit_remaining="19"))  # e.g. the owner raised the limit
    assert g.mode()[0] is Mode.FULL


def test_a_credits_402_stops_everything_and_a_poll_cannot_restart_it():
    g = governor([T0])
    g.observe(status())
    g.on_failure(
        BudgetExhausted("broke", limit_source="openrouter_credits", retry_after=None),
        Route(Tier.CHEAP, TIERS[Tier.CHEAP]),
    )
    assert g.mode()[0] is Mode.OFF and "top it up" in g.stopped
    g.observe(status())
    assert g.mode()[0] is Mode.OFF and g.route(CRITICAL) is None


def test_a_429_on_a_free_route_marks_the_free_cap_until_a_poll_clears_it():
    g = governor([T0])
    g.observe(status())
    g.on_failure(RateLimited("cap", status=429), Route(Tier.FREE, TIERS[Tier.FREE]))
    assert g.route(TAGGING).tier is Tier.CHEAP
    g.observe(status(free_left=0))
    assert g.route(TAGGING).tier is Tier.CHEAP
    g.observe(status(free_left=500))  # a new UTC day
    assert g.route(TAGGING).tier is Tier.FREE


def test_a_429_on_a_paid_route_pauses_paid_calls():
    clock = [T0]
    g = governor(clock)
    g.observe(status())
    g.on_failure(RateLimited("slow down", status=429), Route(Tier.CHEAP, TIERS[Tier.CHEAP]))
    assert g.mode()[0] is Mode.FREE_ONLY
    clock[0] = T0 + timedelta(seconds=61)
    assert g.mode()[0] is Mode.FULL


def test_other_failures_leave_the_budget_alone():
    g = governor([T0])
    g.observe(status())
    g.on_failure(CallFailed("upstream died", status=503), Route(Tier.CHEAP, TIERS[Tier.CHEAP]))
    assert g.mode()[0] is Mode.FULL
