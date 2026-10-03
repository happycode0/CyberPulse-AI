"""One enrichment pass (worker/ai/enrich.py): what each call's outcome does, and what a pass
spends on. No network and no database: the client, the budget reading and the writes are fakes."""

from collections import Counter
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest
from pydantic import SecretStr

from worker.ai import enrich as enrich_mod
from worker.ai.budget import KEY_URL, Governor, KeyStatus, Mode, Work
from worker.ai.client import BudgetExhausted, CallFailed, Completion, InvalidOutput, RateLimited
from worker.ai.enrich import (
    AiLayer,
    Done,
    EnrichSummary,
    Failed,
    Waiting,
    after_triage,
    enrich_pending,
    run_task,
)
from worker.ai.ladder import Breach, Ladder, LadderUnusable, Tier, VerifiedLadder
from worker.ai.tasks import BRIEF, SEVERITY, TRIAGE, Brief, Subject, Triage
from worker.models import AiSignificance, AiSubdomain, Event, Severity, SeveritySource
from worker.settings import Settings

KEY = "fake-key-TESTONLY"
T0 = datetime(2026, 10, 3, 1, 0, tzinfo=UTC)
SOURCE_TEXT = "Acme patched a flaw in its SecureGate VPN that attackers used against hospitals."

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
VERIFIED = VerifiedLadder(ladder=LADDER, checked_at=T0)

TRIAGE_ANSWER = {
    "domains": ["cybersecurity"],
    "categories": ["vulnerability"],
    "ai_subdomain": None,
    "ai_significance": None,
    "entities": {
        "actors": [],
        "organisations": ["Acme"],
        "products": ["SecureGate VPN"],
        "countries": [],
        "industries": ["health"],
    },
    "tags": ["vpn"],
}
BRIEF_ANSWER = {
    "summary": "A SecureGate VPN bug let attackers into hospital networks; Acme has a fix.",
    "why_it_matters": None,
    "au": {"relevance": 0.1, "reasons": [], "sectors": []},
}
SEVERITY_ANSWER = {"severity": "high", "confidence": 0.8, "rationale": "Exploited in the wild."}
ANSWERS = {
    TRIAGE.schema_name: TRIAGE_ANSWER,
    BRIEF.schema_name: BRIEF_ANSWER,
    SEVERITY.schema_name: SEVERITY_ANSWER,
}


def subject(event_id="evt-2026-000001", severity=Severity.UNKNOWN, **changes) -> Subject:
    e = Event(
        event_id=event_id,
        first_seen=T0,
        last_seen=T0,
        title="SecureGate VPN flaw exploited",
        summary=SOURCE_TEXT,
        severity=severity,
        **changes,
    )
    return Subject(event=e, source_text=SOURCE_TEXT)


def completion(data, model="vendor-b/cheap") -> Completion:
    return Completion(
        data=data,
        model=model,
        upstream="provider",
        tokens_in=100,
        tokens_out=50,
        cost_usd=Decimal("0.0001"),
        generation_id="gen-1",
    )


class FakeClient:
    """Answers each call with the next scripted reply: an exception to raise, a Completion, or
    nothing, which means the task's good answer."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = []

    async def complete(self, tier, *, schema_name, attribution, models, **_):
        self.calls.append((tier, models, attribution))
        reply = self.replies.pop(0) if self.replies else None
        if isinstance(reply, BaseException):
            raise reply
        return reply or completion(ANSWERS[schema_name])


def key_status(usage_monthly="0", free_left=988) -> KeyStatus:
    return KeyStatus(
        limit=Decimal("20"),
        limit_remaining=Decimal("20") - Decimal(usage_monthly),
        limit_reset="monthly",
        usage_daily=Decimal("0"),
        usage_monthly=Decimal(usage_monthly),
        free_requests_remaining=free_left,
    )


def governor(usage_monthly="0", budget="20") -> Governor:
    g = Governor(VERIFIED, Decimal(budget), now=lambda: T0)
    g.observe(key_status(usage_monthly))
    return g


# ─── One task ─────────────────────────────────────────────────────────────────────────────────────


async def test_a_good_answer_is_done_and_attributed_to_the_event():
    client = FakeClient()
    outcome = await run_task(client, governor(), TRIAGE, subject())
    assert isinstance(outcome, Done) and isinstance(outcome.result, Triage)
    assert outcome.model == "vendor-b/cheap"
    [(tier, models, attribution)] = client.calls
    assert tier is Tier.FREE and models == LADDER.tiers[Tier.FREE]
    assert (attribution.agent, attribution.stage, attribution.event_id) == (
        "worker",
        "enrich.triage",
        "evt-2026-000001",
    )


async def test_an_unusable_answer_gets_one_retry():
    client = FakeClient(InvalidOutput("not the schema", model="vendor-a/paid"))
    outcome = await run_task(client, governor(), BRIEF, subject())
    assert isinstance(outcome, Done) and isinstance(outcome.result, Brief)
    assert len(client.calls) == 2


async def test_two_unusable_answers_fail_with_our_reason_and_the_model():
    bad = {**BRIEF_ANSWER, "summary": "See https://evil.example for the patch."}
    client = FakeClient(completion(bad, "m1"), completion(bad, "m2"))
    outcome = await run_task(client, governor(), BRIEF, subject())
    assert isinstance(outcome, Failed) and "URL" in outcome.reason and outcome.model == "m2"
    assert len(client.calls) == 2


async def test_an_answer_missing_a_field_is_an_unusable_answer():
    client = FakeClient(completion({}), completion({}))
    outcome = await run_task(client, governor(), TRIAGE, subject())
    assert outcome == Failed("the answer could not be read", "vendor-b/cheap")


async def test_a_spent_key_limit_tightens_the_governor_and_the_task_waits():
    g = governor()
    client = FakeClient(
        BudgetExhausted("402", limit_source="openrouter_key_limit", retry_after=None)
    )
    outcome = await run_task(client, g, BRIEF, subject())
    assert isinstance(outcome, Waiting) and len(client.calls) == 1
    assert g.mode()[0] is Mode.FREE_ONLY


async def test_out_of_credits_stops_the_ai_layer():
    g = governor()
    client = FakeClient(BudgetExhausted("402", limit_source="openrouter_credits", retry_after=None))
    outcome = await run_task(client, g, SEVERITY, subject())
    assert isinstance(outcome, Waiting) and g.mode()[0] is Mode.OFF


async def test_a_free_models_daily_cap_steps_triage_up_a_tier():
    client = FakeClient(RateLimited("429", status=429))
    outcome = await run_task(client, governor(), TRIAGE, subject())
    assert isinstance(outcome, Done)
    (first, *_), (second, models, _) = client.calls
    assert first is Tier.FREE and second is Tier.CHEAP
    assert not any(m.endswith(":free") for m in models)


@pytest.mark.parametrize("status", [None, 500, 503, 408])
async def test_an_outage_waits_for_the_next_pass_without_retrying(status):
    client = FakeClient(CallFailed("down", status=status))
    outcome = await run_task(client, governor(), BRIEF, subject())
    assert isinstance(outcome, Waiting) and len(client.calls) == 1


async def test_a_request_openrouter_refuses_fails():
    client = FakeClient(CallFailed("bad request", status=400))
    outcome = await run_task(client, governor(), BRIEF, subject())
    assert outcome == Failed("OpenRouter refused the request (HTTP 400)")


async def test_an_editorial_task_never_goes_to_tier_0():
    # MINIMAL sends what matters most to tier 0, which §7.1 keeps for mechanical work.
    client = FakeClient()
    g = governor(usage_monthly="17")
    assert g.mode()[0] is Mode.MINIMAL
    outcome = await run_task(client, g, BRIEF, subject(severity=Severity.CRITICAL))
    assert outcome == Waiting("only tier 0 is open, and the task is editorial")
    assert await run_task(client, g, TRIAGE, subject(severity=Severity.CRITICAL))
    assert [tier for tier, _, _ in client.calls] == [Tier.FREE]  # triage only


async def test_work_the_mode_does_not_cover_waits_without_a_call():
    client = FakeClient()
    outcome = await run_task(client, governor(usage_monthly="17"), TRIAGE, subject())
    assert isinstance(outcome, Waiting) and client.calls == []


async def test_a_call_that_cannot_be_accounted_for_stops_the_task():
    client = FakeClient(RuntimeError("ledger write failed"))
    with pytest.raises(RuntimeError, match="ledger"):
        await run_task(client, governor(), TRIAGE, subject())


# ─── The AI layer between passes ──────────────────────────────────────────────────────────────────


def settings(key=KEY, budget="20") -> Settings:
    return Settings(
        _env_file=None,
        database_url="sqlite://",
        openrouter_api_key=SecretStr(key) if key else None,
        ai_monthly_budget_usd=Decimal(budget),
    )


def key_body(**data):
    base = {
        "label": "fake-label-TESTONLY...890",
        "limit": 20,
        "limit_remaining": 20,
        "limit_reset": "monthly",
        "usage_daily": 0,
        "usage_monthly": 0,
        "free_model_daily_requests": {"used": 0, "limit": 1000, "remaining": 1000},
    }
    base.update(data)
    return {"data": base}


class Verifier:
    def __init__(self, raises=None):
        self.raises = raises
        self.count = 0

    async def __call__(self):
        self.count += 1
        if self.raises:
            raise self.raises
        return VERIFIED


async def test_with_no_key_the_ai_layer_is_off_and_checks_nothing():
    verify = Verifier()
    layer = AiLayer(settings(key=None), verify=verify)
    async with httpx.AsyncClient() as http:
        assert await layer.ready(http) is None
    assert verify.count == 0


async def test_a_ladder_that_fails_the_guard_turns_the_ai_layer_off(caplog):
    layer = AiLayer(settings(), verify=Verifier(raises=LadderUnusable("over the ceiling")))
    async with httpx.AsyncClient() as http:
        assert await layer.ready(http) is None
    assert layer.verified is None and "did not pass" in caplog.text


async def test_the_ladder_is_checked_again_after_a_day_and_the_governor_kept(respx_mock):
    respx_mock.get(KEY_URL).mock(return_value=httpx.Response(200, json=key_body()))
    clock = [T0]
    verify = Verifier()
    layer = AiLayer(settings(), verify=verify, clock=lambda: clock[0])
    async with httpx.AsyncClient() as http:
        first = await layer.ready(http)
        assert first is not None and first.mode()[0] is Mode.FULL
        clock[0] = T0 + timedelta(hours=23)
        assert await layer.ready(http) is first and verify.count == 1
        clock[0] = T0 + timedelta(hours=25)
        assert await layer.ready(http) is first and verify.count == 2


async def test_a_ladder_the_scan_checked_is_used_without_checking_again(respx_mock, caplog):
    respx_mock.get(KEY_URL).mock(return_value=httpx.Response(200, json=key_body()))
    verify = Verifier()
    layer = AiLayer(settings(), verify=verify, clock=lambda: T0 + timedelta(hours=1))
    pruned = Ladder.model_validate(
        {"tiers": {**LADDER.tiers, Tier.CHEAP: LADDER.tiers[Tier.CHEAP][1:]}}
    )
    gone = Breach(Tier.CHEAP, LADDER.tiers[Tier.CHEAP][0], "not on OpenRouter")
    layer.adopt(VerifiedLadder(ladder=pruned, checked_at=T0, dropped=(gone,)))
    async with httpx.AsyncClient() as http:
        g = await layer.ready(http)
    assert g is not None and verify.count == 0
    assert g.route(Work(Tier.CHEAP, Severity.HIGH)).models == pruned.tiers[Tier.CHEAP]
    assert "is dropped for now" in caplog.text


async def test_a_scan_that_rejected_the_ladder_makes_the_next_pass_check(respx_mock):
    respx_mock.get(KEY_URL).mock(return_value=httpx.Response(200, json=key_body()))
    verify = Verifier()
    layer = AiLayer(settings(), verify=verify, clock=lambda: T0)
    layer.adopt(VERIFIED)
    layer.adopt(None)
    async with httpx.AsyncClient() as http:
        assert await layer.ready(http) is not None
    assert verify.count == 1


async def test_an_unreadable_budget_leaves_only_free_models(respx_mock, caplog):
    respx_mock.get(KEY_URL).mock(return_value=httpx.Response(503))
    layer = AiLayer(settings(), verify=Verifier(), clock=lambda: T0)
    async with httpx.AsyncClient() as http:
        g = await layer.ready(http)
    assert g is not None and g.mode() == (Mode.FREE_ONLY, "no budget reading yet")
    assert "no budget reading this pass" in caplog.text


# ─── One pass ─────────────────────────────────────────────────────────────────────────────────────


class FakeLayer:
    def __init__(self, g, *, budget="20"):
        self.governor = g
        self.verified = VERIFIED
        self.settings = settings(budget=budget)

    async def ready(self, http):
        return self.governor


@pytest.fixture
def pass_(monkeypatch):
    """`enrich_pending` with the database and the client replaced by recorders."""
    state = {"candidates": [], "settled": 0, "complete": True, "client": FakeClient()}
    log = []

    def load_due(engine, now, limit):
        log.append(("load", limit))
        return state["candidates"], state["settled"]

    def write(engine, task, subj, outcome, now):
        log.append(("write", subj.event.event_id, task.name, type(outcome).__name__))

    def complete(engine, event_id):
        log.append(("complete", event_id))
        return state["complete"]

    def rescore(engine, scoring, touched, *, now):
        log.append(("rescore", sorted(touched)))
        return []

    monkeypatch.setattr(enrich_mod, "_load_due", load_due)
    monkeypatch.setattr(enrich_mod, "_write", write)
    monkeypatch.setattr(enrich_mod, "_complete", complete)
    monkeypatch.setattr(enrich_mod, "rescore", rescore)
    monkeypatch.setattr(enrich_mod, "OpenRouterClient", lambda *a: state["client"])
    return state, log


async def test_an_ai_layer_that_is_off_reads_nothing(pass_):
    _, log = pass_
    summary = await enrich_pending(engine="engine", layer=FakeLayer(None), now=T0)
    assert summary == EnrichSummary() and log == []


async def test_mode_off_reads_nothing(pass_):
    _, log = pass_
    layer = FakeLayer(governor(budget="0"))
    summary = await enrich_pending(engine="engine", layer=layer, now=T0)
    assert summary.mode == "off" and log == []


async def test_a_pass_runs_each_events_tasks_and_rescores_what_changed(pass_):
    state, log = pass_
    a, b = subject("evt-2026-000010"), subject("evt-2026-000020")
    state["candidates"] = [(a, [TRIAGE, BRIEF]), (b, [SEVERITY])]
    summary = await enrich_pending(engine="engine", layer=FakeLayer(governor()), batch=4, now=T0)

    assert log[0] == ("load", 20)
    writes = [entry for entry in log if entry[0] == "write"]
    assert sorted(writes) == [
        ("write", "evt-2026-000010", "brief", "Done"),
        ("write", "evt-2026-000010", "triage", "Done"),
        ("write", "evt-2026-000020", "severity", "Done"),
    ]
    # Each event's tasks in order.
    assert writes.index(("write", "evt-2026-000010", "triage", "Done")) < writes.index(
        ("write", "evt-2026-000010", "brief", "Done")
    )
    assert log[-1] == ("rescore", ["evt-2026-000010", "evt-2026-000020"])
    assert summary.mode == "full" and summary.events == 2
    assert summary.done == Counter(triage=1, brief=1, severity=1)
    assert summary.completed == 2 and summary.rescored == 2 and summary.changed_anything


async def test_events_whose_tasks_all_wait_are_passed_over_for_ones_that_can_run(pass_):
    state, log = pass_
    low, high = subject("evt-2026-000030"), subject("evt-2026-000040", severity=Severity.HIGH)
    state["candidates"] = [(low, [TRIAGE]), (high, [TRIAGE, BRIEF])]
    g = governor(usage_monthly="17")  # MINIMAL: only what matters most, and only on tier 0
    summary = await enrich_pending(engine="engine", layer=FakeLayer(g), batch=1, now=T0)
    assert summary.mode == "minimal" and summary.blocked == 1 and summary.events == 1
    assert [e for e in log if e[0] == "write"] == [
        ("write", "evt-2026-000040", "triage", "Done"),
        ("write", "evt-2026-000040", "brief", "Waiting"),
    ]
    assert summary.done == Counter(triage=1) and summary.waiting == 1


async def test_a_batch_takes_the_first_events_only(pass_):
    state, log = pass_
    state["candidates"] = [(subject(f"evt-2026-{i:06d}"), [TRIAGE]) for i in range(5)]
    summary = await enrich_pending(engine="engine", layer=FakeLayer(governor()), batch=2, now=T0)
    assert summary.events == 2
    assert {e[1] for e in log if e[0] == "write"} == {"evt-2026-000000", "evt-2026-000001"}


async def test_a_pass_where_nothing_was_done_does_not_rescore(pass_):
    state, log = pass_
    state["client"] = FakeClient(CallFailed("down", status=503))
    state["candidates"] = [(subject(), [TRIAGE])]
    summary = await enrich_pending(engine="engine", layer=FakeLayer(governor()), now=T0)
    assert summary.waiting == 1 and not summary.changed_anything
    assert not any(e[0] in ("rescore", "complete") for e in log)


async def test_events_settled_while_loading_count_as_completed(pass_):
    state, _ = pass_
    state["settled"] = 3
    summary = await enrich_pending(engine="engine", layer=FakeLayer(governor()), now=T0)
    assert summary.completed == 3 and summary.changed_anything


async def test_a_ledger_failure_stops_the_whole_pass(pass_):
    state, _ = pass_
    state["client"] = FakeClient(RuntimeError("ledger write failed"))
    state["candidates"] = [(subject("evt-2026-000010"), [TRIAGE])]
    with pytest.raises(ExceptionGroup) as raised:
        await enrich_pending(engine="engine", layer=FakeLayer(governor()), now=T0)
    assert raised.group_contains(RuntimeError, match="ledger")


def test_severity_is_skipped_for_an_event_with_an_official_score():
    official = subject(severity=Severity.HIGH, severity_source=SeveritySource.NVD)
    assert enrich_mod._due_tasks(official, {}) == [TRIAGE, BRIEF]
    assert enrich_mod._due_tasks(subject(), {}) == [TRIAGE, BRIEF, SEVERITY]


# ─── The AI beat (docs/wiki/ai-news-beat.md) ──────────────────────────────────────────────────────

AI_TRIAGE = {
    **TRIAGE_ANSWER,
    "domains": ["ai"],
    "categories": ["model-release"],
    "ai_subdomain": "AI_INDUSTRY",
    "ai_significance": "major",
    "entities": {**TRIAGE_ANSWER["entities"], "organisations": [], "products": []},
}


def ai_triage(*domains, significance=AiSignificance.MAJOR) -> Triage:
    return Triage(
        domains=domains,
        categories=("model-release",),
        ai_subdomain=AiSubdomain.AI_INDUSTRY if "ai" in domains else None,
        actors=(),
        organisations=(),
        products=(),
        countries=(),
        industries=(),
        tags=(),
        ai_significance=significance if "ai" in domains else None,
    )


def test_an_ai_only_story_is_not_given_a_severity_task():
    assert enrich_mod._due_tasks(subject(domains=["ai"]), {}) == [TRIAGE, BRIEF]
    assert enrich_mod._due_tasks(subject(domains=["cybersecurity", "ai"]), {}) == [
        TRIAGE,
        BRIEF,
        SEVERITY,
    ]


def test_after_triage_an_ai_only_story_drops_a_models_cyber_rating():
    s = subject(severity=Severity.HIGH, severity_source=SeveritySource.AI_ESTIMATE)
    after = after_triage(s, ai_triage("ai"))
    e = after.event
    assert e.domains == ["ai"] and e.ai_significance is AiSignificance.MAJOR
    assert (e.severity, e.severity_source) == (Severity.UNKNOWN, SeveritySource.UNKNOWN)
    assert not SEVERITY.applies(after)
    assert s.event.severity is Severity.HIGH  # the original is untouched


def test_after_triage_an_official_score_and_a_story_on_both_desks_keep_theirs():
    official = subject(severity=Severity.LOW, severity_source=SeveritySource.NVD)
    assert after_triage(official, ai_triage("ai")).event.severity is Severity.LOW
    estimate = subject(severity=Severity.HIGH, severity_source=SeveritySource.AI_ESTIMATE)
    both = after_triage(estimate, ai_triage("cybersecurity", "ai")).event
    assert (both.severity, both.beat.value) == (Severity.HIGH, "both")


async def test_an_ai_industry_story_seeded_as_cyber_gets_no_severity_call(pass_):
    state, log = pass_
    state["client"] = client = FakeClient(completion(AI_TRIAGE))
    seeded = subject("evt-2026-000050", domains=["cybersecurity"])
    state["candidates"] = [(seeded, [TRIAGE, BRIEF, SEVERITY])]
    summary = await enrich_pending(engine="engine", layer=FakeLayer(governor()), now=T0)
    assert [e for e in log if e[0] == "write"] == [
        ("write", "evt-2026-000050", "triage", "Done"),
        ("write", "evt-2026-000050", "brief", "Done"),
    ]
    assert [tier for tier, _, _ in client.calls] == [Tier.FREE, Tier.CHEAP]
    assert summary.done == Counter(triage=1, brief=1)


async def test_a_major_ai_story_matters_most_when_money_is_short(pass_):
    state, log = pass_
    minor = subject("evt-2026-000060", domains=["ai"], ai_significance=AiSignificance.MINOR)
    major = subject("evt-2026-000070", domains=["ai"], ai_significance=AiSignificance.MAJOR)
    state["candidates"] = [(minor, [TRIAGE]), (major, [TRIAGE])]
    g = governor(usage_monthly="17")  # MINIMAL
    summary = await enrich_pending(engine="engine", layer=FakeLayer(g), batch=1, now=T0)
    assert summary.mode == "minimal" and summary.blocked == 1
    assert [e for e in log if e[0] == "write"] == [("write", "evt-2026-000070", "triage", "Done")]
