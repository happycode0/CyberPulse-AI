"""MixRoute as the AI provider (AI_PROVIDER=mixroute): the reviewed price list, the guard, the
client's request and fallback, cost from tokens, and a budget read from our own ledger."""

import json
import logging
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest
from pydantic import SecretStr, ValidationError

from worker.ai import provider
from worker.ai.budget import (
    QUOTA_PAUSE,
    Governor,
    Mode,
    Work,
    assess,
    ledger_key_status,
)
from worker.ai.client import (
    Attribution,
    BudgetExhausted,
    CallFailed,
    MixRouteClient,
    RateLimited,
)
from worker.ai.enrich import AiLayer, Waiting, run_task
from worker.ai.ladder import (
    MAX_PROMPT_TOKENS,
    MIXROUTE_LADDER_PATH,
    CatalogueUnavailable,
    LadderRejected,
    MixRouteConfig,
    Tier,
    VerifiedLadder,
    is_free,
    verify_mixroute_candidate,
    verify_mixroute_ladder,
)
from worker.ai.tasks import BRIEF, Subject
from worker.models import Event, Severity
from worker.publish.validate import scan_for_secrets
from worker.settings import Settings

# Not shaped like any real key, so neither the publisher's scan nor ops/check-keys.sh flags it.
KEY = "fake-key-TESTONLY"
BASE = "https://mixroute.test/v1"
MODELS = f"{BASE}/models"
CHAT = f"{BASE}/chat/completions"
T0 = datetime(2026, 11, 3, 1, 0, tzinfo=UTC)

CONFIG = MixRouteConfig.model_validate(
    {
        "tiers": {
            "tier0_free": ["vz/free-one", "va/paid-zero"],
            "tier1_cheap": ["va/Cheap-One", "vb/cheap-two"],
            "tier2_strong": ["vb/strong", "va/Cheap-One"],
            "code": ["vc/code"],
            "audit": ["vd/audit"],
        },
        "models": {
            "vz/free-one": {"id": "free-one", "in": 0, "out": 0, "free": True},
            "va/paid-zero": {"id": "paid-zero", "in": 0.05, "out": 0.40, "cache_read": 0.005,
                             "max_tokens_field": "max_completion_tokens"},
            "va/Cheap-One": {"id": "Cheap-One", "in": 0.14, "out": 0.28, "cache_read": 0.0028},
            "vb/cheap-two": {"id": "cheap-two", "in": 0.10, "out": 0.40,
                             "request": {"enable_thinking": False}},
            "vb/strong": {"id": "strong", "in": 0.15, "out": 0.50},
            "vc/code": {"id": "code", "in": 0.41, "out": 0.819},
            "vd/audit": {"id": "audit", "in": 0.10, "out": 0.40},
            "ve/challenger": {"id": "challenger", "in": 0.10, "out": 0.50,
                              "above": [{"prompt_tokens": 100000, "in": 0.5, "out": 2.5}]},
        },
    }
)

SCHEMA = {
    "type": "object",
    "properties": {"category": {"type": "string"}},
    "required": ["category"],
    "additionalProperties": False,
}
WHO = Attribution(agent="worker", stage="enrich.brief", event_id="evt-2026-000042")


def settings(**overrides) -> Settings:
    values = {
        "_env_file": None,
        "database_url": "sqlite://",
        "ai_provider": "mixroute",
        "mix_router_url": BASE,
        "mix_router_key": SecretStr(KEY),
        "user_agent": "CyberPulse-AI/1.0",
    }
    values.update(overrides)
    return Settings(**values)


def listing(*ids):
    return httpx.Response(200, json={"object": "list", "data": [{"id": i, "object": "model"} for i in ids]})


ALL_IDS = [m.id for m in CONFIG.models.values()]


def answer(*, model="Cheap-One", prompt=1000, completion=200, cached=None, content=None):
    usage = {"prompt_tokens": prompt, "completion_tokens": completion, "total_tokens": prompt + completion}
    if cached is not None:
        usage["prompt_tokens_details"] = {"cached_tokens": cached}
    return httpx.Response(
        200,
        json={
            "id": "chatcmpl-1",
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "stop",
                    "message": {"role": "assistant", "content": content or json.dumps({"category": "breach"})},
                }
            ],
            "usage": usage,
        },
    )


# ─── The price list ───────────────────────────────────────────────────────────────────────────────


def test_the_shipped_mixroute_ladder_loads_and_keeps_the_rules():
    config = MixRouteConfig.load(MIXROUTE_LADDER_PATH)
    ladder = config.ladder
    assert set(ladder.tiers) == set(Tier)
    # Every model a tier names is priced, and tier 0 ends in a paid one.
    assert ladder.slugs() <= set(config.models)
    assert not config.models[ladder.tiers[Tier.FREE][-1]].free
    assert config.models["zhipu/glm-4-flash"].free
    # The approved picks, in order.
    assert ladder.tiers[Tier.CHEAP][0] == "deepseek/deepseek-v4-flash"
    assert ladder.tiers[Tier.STRONG] == (
        "deepseek/deepseek-v4-flash",
        "zhipu/glm-5.3-flash",
        "xiaomi/mimo-v2.5-pro",
    )
    # Ids are sent exactly as written; the vendor prefix is ours.
    assert config.models["deepseek/deepseek-v4-flash"].id == "deepseek-v4-flash"


def test_every_shipped_ladder_model_is_under_the_ceiling():
    config = MixRouteConfig.load(MIXROUTE_LADDER_PATH)
    for slug in config.ladder.slugs():
        assert config.models[slug].worst_output() <= Decimal("1.00"), slug


def test_a_tier_model_with_no_price_is_refused():
    raw = CONFIG.model_dump(by_alias=True)
    del raw["models"]["vb/strong"]
    with pytest.raises(ValidationError, match="no price"):
        MixRouteConfig.model_validate(raw)


def test_tier0_must_end_in_a_paid_model_on_mixroute_too():
    raw = CONFIG.model_dump(by_alias=True)
    raw["tiers"]["tier0_free"] = ["va/paid-zero", "vz/free-one"]
    with pytest.raises(ValidationError, match="paid"):
        MixRouteConfig.model_validate(raw)


def test_a_free_model_must_cost_nothing():
    raw = CONFIG.model_dump(by_alias=True)
    raw["models"]["vz/free-one"]["out"] = 0.1
    with pytest.raises(ValidationError, match="free"):
        MixRouteConfig.model_validate(raw)


def test_code_and_audit_still_need_different_vendors():
    raw = CONFIG.model_dump(by_alias=True)
    raw["tiers"]["audit"] = ["vc/code"]
    with pytest.raises(ValidationError, match="share a vendor"):
        MixRouteConfig.model_validate(raw)


def test_free_means_the_price_list_says_so():
    assert is_free("vz/free-one", CONFIG) and not is_free("va/paid-zero", CONFIG)
    # Without a price list, OpenRouter's rule.
    assert is_free("v/model:free") and not is_free("v/model")


def test_a_tiered_price_is_charged_at_the_step_the_prompt_reaches():
    m = CONFIG.models["ve/challenger"]
    assert m.cost(1_000, 1_000, 0) == Decimal("0.0006")  # 0.10 + 0.50 per M, a thousand each
    assert m.cost(200_000, 1_000, 0) == Decimal("0.1025")  # 0.5 * 0.2 + 2.5 * 0.001


def test_cached_input_is_charged_at_cache_read_or_else_at_full_price():
    cheap = CONFIG.models["va/Cheap-One"]
    # 600 fresh at 0.14, 400 cached at 0.0028, 200 out at 0.28, per million.
    assert cheap.cost(1000, 200, 400) == Decimal("0.00014112")
    strong = CONFIG.models["vb/strong"]  # no cache_read listed
    assert strong.cost(1000, 0, 400) == strong.cost(1000, 0, 0)


# ─── The guard ────────────────────────────────────────────────────────────────────────────────────


async def test_a_ladder_mixroute_lists_and_prices_under_the_ceiling_passes(respx_mock):
    route = respx_mock.get(MODELS).mock(return_value=listing(*ALL_IDS))
    verified = await verify_mixroute_ladder(CONFIG, settings=settings())
    assert verified.ladder == CONFIG.ladder and verified.dropped == ()
    assert verified.prices is CONFIG
    assert route.calls.last.request.headers["authorization"] == f"Bearer {KEY}"


async def test_a_model_mixroute_does_not_list_is_dropped(respx_mock):
    respx_mock.get(MODELS).mock(return_value=listing(*[i for i in ALL_IDS if i != "cheap-two"]))
    verified = await verify_mixroute_ladder(CONFIG, settings=settings())
    assert "vb/cheap-two" not in verified.ladder.tiers[Tier.CHEAP]
    [breach] = verified.dropped
    assert (breach.tier, breach.slug) == (Tier.CHEAP, "vb/cheap-two")
    assert "not on MixRoute" in breach.reason


async def test_an_id_is_matched_exactly_case_and_all(respx_mock):
    respx_mock.get(MODELS).mock(
        return_value=listing(*[i.lower() for i in ALL_IDS])  # "cheap-one", not "Cheap-One"
    )
    verified = await verify_mixroute_ladder(CONFIG, settings=settings())
    assert {b.slug for b in verified.dropped} == {"va/Cheap-One"}


async def test_a_price_over_the_ceiling_is_dropped(respx_mock):
    respx_mock.get(MODELS).mock(return_value=listing(*ALL_IDS))
    raw = CONFIG.model_dump(by_alias=True)
    raw["models"]["vb/strong"]["out"] = 1.2
    verified = await verify_mixroute_ladder(MixRouteConfig.model_validate(raw), settings=settings())
    [breach] = verified.dropped
    assert breach.slug == "vb/strong" and "$1.20/M output" in breach.reason
    assert verified.ladder.tiers[Tier.STRONG] == ("va/Cheap-One",)


async def test_a_tier_left_empty_by_the_ceiling_rejects_the_ladder(respx_mock):
    respx_mock.get(MODELS).mock(return_value=listing(*ALL_IDS))
    raw = CONFIG.model_dump(by_alias=True)
    raw["models"]["vc/code"]["out"] = 1.2
    with pytest.raises(LadderRejected, match="ceiling"):
        await verify_mixroute_ladder(MixRouteConfig.model_validate(raw), settings=settings())


async def test_a_dearer_step_a_worker_prompt_can_reach_counts(respx_mock):
    respx_mock.get(MODELS).mock(return_value=listing(*ALL_IDS))
    reachable = MAX_PROMPT_TOKENS // 2
    raw = CONFIG.model_dump(by_alias=True)
    raw["models"]["vb/strong"]["above"] = [{"prompt_tokens": reachable, "in": 0.5, "out": 2.5}]
    verified = await verify_mixroute_ladder(MixRouteConfig.model_validate(raw), settings=settings())
    [breach] = verified.dropped
    assert breach.slug == "vb/strong" and "ceiling" in breach.reason


async def test_a_dearer_step_past_any_worker_prompt_does_not(respx_mock):
    respx_mock.get(MODELS).mock(return_value=listing(*ALL_IDS))
    candidate = await verify_mixroute_candidate(
        "ve/challenger", Tier.CHEAP, CONFIG, settings=settings()
    )
    assert candidate.slug == "ve/challenger" and candidate.tier is Tier.CHEAP


async def test_a_candidate_with_no_price_is_rejected_without_a_fetch(respx_mock):
    route = respx_mock.get(MODELS).mock(return_value=listing(*ALL_IDS))
    with pytest.raises(LadderRejected, match="no price"):
        await verify_mixroute_candidate("vx/unknown", Tier.CHEAP, CONFIG, settings=settings())
    assert route.call_count == 0


@pytest.mark.parametrize(
    "response",
    [httpx.Response(401), httpx.Response(500), httpx.Response(200, json={"data": "x"})],
)
async def test_an_unreadable_model_list_fails_closed(respx_mock, response):
    respx_mock.get(MODELS).mock(return_value=response)
    with pytest.raises(CatalogueUnavailable):
        await verify_mixroute_ladder(CONFIG, settings=settings())


async def test_with_no_key_the_guard_cannot_check(respx_mock):
    with pytest.raises(CatalogueUnavailable, match="MIX_ROUTER_KEY"):
        await verify_mixroute_ladder(CONFIG, settings=settings(mix_router_key=None))


# ─── The client ───────────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def ledger():
    return []


@pytest.fixture
async def client(ledger):
    async with httpx.AsyncClient() as http:
        verified = VerifiedLadder(ladder=CONFIG.ladder, checked_at=T0, prices=CONFIG)
        yield MixRouteClient(verified, SecretStr(KEY), http, ledger.append, settings=settings())


def call(client, tier=Tier.CHEAP, **overrides):
    kwargs = {
        "schema_name": "classification",
        "schema": SCHEMA,
        "messages": [{"role": "user", "content": "Classify this."}],
        "max_tokens": 200,
        "attribution": WHO,
    }
    kwargs.update(overrides)
    return client.complete(tier, **kwargs)


def bodies(route):
    return [json.loads(c.request.content) for c in route.calls]


async def test_the_request_names_one_model_by_its_exact_id(respx_mock, client):
    route = respx_mock.post(CHAT).mock(return_value=answer())
    await call(client)
    [body] = bodies(route)
    assert body["model"] == "Cheap-One"
    assert "models" not in body and "provider" not in body
    assert body["response_format"] == {
        "type": "json_schema",
        "json_schema": {"name": "classification", "strict": True, "schema": SCHEMA},
    }
    assert body["max_tokens"] == 200


async def test_per_model_extras_and_the_max_tokens_field_are_honoured(respx_mock, client):
    route = respx_mock.post(CHAT).mock(
        side_effect=[httpx.Response(503), answer(model="cheap-two"), answer(model="paid-zero")]
    )
    await call(client)  # Cheap-One is unavailable, so cheap-two answers
    await call(client, Tier.FREE, models=("va/paid-zero",))
    first, second, third = bodies(route)
    assert second["model"] == "cheap-two" and second["enable_thinking"] is False
    assert third["model"] == "paid-zero" and third["max_completion_tokens"] == 200
    assert "max_tokens" not in third


async def test_the_request_carries_no_openrouter_headers(respx_mock, client):
    route = respx_mock.post(CHAT).mock(return_value=answer())
    await call(client)
    h = route.calls.last.request.headers
    assert h["authorization"] == f"Bearer {KEY}" and h["user-agent"] == "CyberPulse-AI/1.0"
    assert not any(k.lower().startswith(("x-openrouter", "http-referer")) for k in h)


@pytest.mark.parametrize("status", [429, 503, 504, 500])
async def test_a_busy_or_missing_model_falls_through_to_the_next(respx_mock, client, ledger, status):
    route = respx_mock.post(CHAT).mock(side_effect=[httpx.Response(status), answer(model="cheap-two")])
    done = await call(client)
    assert [b["model"] for b in bodies(route)] == ["Cheap-One", "cheap-two"]
    assert done.model == "vb/cheap-two"
    [entry] = ledger
    assert (entry.requested_model, entry.model) == ("va/Cheap-One", "vb/cheap-two")


async def test_a_timeout_falls_through_too(respx_mock, client):
    respx_mock.post(CHAT).mock(side_effect=[httpx.ReadTimeout("slow"), answer(model="cheap-two")])
    assert (await call(client)).model == "vb/cheap-two"


async def test_every_model_rate_limited_is_rate_limited(respx_mock, client):
    route = respx_mock.post(CHAT).mock(return_value=httpx.Response(429))
    with pytest.raises(RateLimited):
        await call(client)
    assert route.call_count == 2


async def test_a_bad_request_is_not_retried_on_another_model(respx_mock, client):
    route = respx_mock.post(CHAT).mock(return_value=httpx.Response(400, json={"error": {"message": "bad"}}))
    with pytest.raises(CallFailed) as exc:
        await call(client)
    assert exc.value.status == 400 and route.call_count == 1


@pytest.mark.parametrize("status", [403, 402])
async def test_a_spent_key_quota_is_budget_exhausted_and_not_retried(respx_mock, client, status):
    route = respx_mock.post(CHAT).mock(return_value=httpx.Response(status))
    with pytest.raises(BudgetExhausted) as exc:
        await call(client)
    assert exc.value.limit_source == "mixroute_key_quota" and not exc.value.transient
    assert route.call_count == 1


async def test_the_cost_is_our_tokens_at_the_reviewed_price(respx_mock, client, ledger):
    respx_mock.post(CHAT).mock(return_value=answer(prompt=1000, completion=200, cached=400))
    done = await call(client)
    assert done.cost_usd == Decimal("0.00014112")
    [entry] = ledger
    assert (entry.provider, entry.upstream) == ("mixroute", None)
    assert (entry.tokens_in, entry.tokens_out, entry.cost_usd) == (1000, 200, Decimal("0.00014112"))


async def test_a_free_model_costs_nothing(respx_mock, client, ledger):
    respx_mock.post(CHAT).mock(return_value=answer(model="free-one"))
    await call(client, Tier.FREE)
    assert ledger[0].cost_usd == Decimal("0")


async def test_a_client_needs_a_ladder_checked_against_mixroute(ledger):
    async with httpx.AsyncClient() as http:
        unchecked = VerifiedLadder(ladder=CONFIG.ladder, checked_at=T0)
        with pytest.raises(ValueError, match="MixRoute"):
            MixRouteClient(unchecked, SecretStr(KEY), http, ledger.append, settings=settings())


# ─── The budget, from our own ledger ──────────────────────────────────────────────────────────────


def test_the_ledger_reading_counts_this_months_and_todays_mixroute_spend():
    asked = []

    def spend(provider_name, start, end):
        asked.append((provider_name, start, end))
        return Decimal("7.5") if start.day == 1 else Decimal("0.25")

    status = ledger_key_status(None, T0, spend=spend)
    assert (status.usage_monthly, status.usage_daily) == (Decimal("7.5"), Decimal("0.25"))
    assert status.limit is None and status.limit_remaining is None
    assert status.free_requests_remaining is None
    assert asked[0] == ("mixroute", datetime(2026, 11, 1, tzinfo=UTC), datetime(2026, 12, 1, tzinfo=UTC))
    assert asked[1][1:] == (datetime(2026, 11, 3, tzinfo=UTC), datetime(2026, 11, 4, tzinfo=UTC))


def test_a_12_dollar_month_conserves_once_half_is_spent():
    status = ledger_key_status(None, T0, spend=lambda p, s, e: Decimal("7") if s.day == 1 else 0)
    assert assess(status, Decimal("12")).mode is Mode.CONSERVE


def mixroute_governor(clock):
    verified = VerifiedLadder(ladder=CONFIG.ladder, checked_at=T0, prices=CONFIG)
    g = Governor(verified, Decimal("12"), now=lambda: clock[0])
    g.observe(ledger_key_status(None, clock[0], spend=lambda p, s, e: Decimal(0)))
    return g


def test_free_only_on_mixroute_means_the_models_priced_free():
    clock = [T0]
    g = mixroute_governor(clock)
    g.on_failure(BudgetExhausted("403", limit_source="mixroute_key_quota", retry_after=None),
                 g.route(Work(Tier.CHEAP, Severity.HIGH)))
    route = g.route(Work(Tier.CHEAP, Severity.CRITICAL))
    assert g.mode()[0] is Mode.FREE_ONLY
    assert route is not None and route.models == ("vz/free-one",)


def test_a_spent_key_quota_pauses_paid_calls_even_past_a_fresh_ledger_reading():
    clock = [T0]
    g = mixroute_governor(clock)
    g.on_failure(BudgetExhausted("403", limit_source="mixroute_key_quota", retry_after=None),
                 g.route(Work(Tier.CHEAP, Severity.HIGH)))
    # Our ledger cannot see the key's quota, so a reading saying money is left does not lift it.
    g.observe(ledger_key_status(None, T0, spend=lambda p, s, e: Decimal(0)))
    mode, why = g.mode()
    assert mode is Mode.FREE_ONLY and "MixRoute" in why
    clock[0] = T0 + QUOTA_PAUSE + timedelta(seconds=1)
    g.observe(ledger_key_status(None, clock[0], spend=lambda p, s, e: Decimal(0)))
    assert g.mode()[0] is Mode.FULL


class OneReply:
    label = "MixRoute"

    def __init__(self, exc):
        self.exc = exc

    async def complete(self, tier, **_):
        raise self.exc


async def test_enrichment_waits_rather_than_fails_on_a_spent_quota():
    clock = [T0]
    g = mixroute_governor(clock)
    e = Event(event_id="evt-2026-000001", first_seen=T0, last_seen=T0, title="t", summary="s")
    outcome = await run_task(
        OneReply(BudgetExhausted("403", limit_source="mixroute_key_quota", retry_after=None)),
        g,
        BRIEF,
        Subject(event=e, source_text="s"),
    )
    assert isinstance(outcome, Waiting)
    assert g.mode()[0] is Mode.FREE_ONLY


async def test_the_ai_layer_on_mixroute_reads_the_ledger_not_openrouter(respx_mock):
    async def verify():
        return VerifiedLadder(ladder=CONFIG.ladder, checked_at=T0, prices=CONFIG)

    layer = AiLayer(
        settings(ai_monthly_budget_usd=Decimal("12")),
        verify=verify,
        clock=lambda: T0,
        ledger_status=lambda: ledger_key_status(None, T0, spend=lambda p, s, e: Decimal("1")),
    )
    async with httpx.AsyncClient() as http:
        g = await layer.ready(http)
    assert g is not None and g.mode()[0] is Mode.FULL
    assert len(respx_mock.calls) == 0  # nothing asked of OpenRouter


async def test_with_no_mixroute_key_the_ai_layer_is_off(caplog):
    caplog.set_level(logging.INFO)
    called = []

    async def verify():
        called.append(1)

    layer = AiLayer(settings(mix_router_key=None), verify=verify)
    async with httpx.AsyncClient() as http:
        assert await layer.ready(http) is None
    assert called == [] and "MIX_ROUTER_KEY is not set" in caplog.text


# ─── Settings and secrets ─────────────────────────────────────────────────────────────────────────


def test_openrouter_stays_the_default_provider():
    s = Settings(_env_file=None, database_url="sqlite://")
    assert s.ai_provider == "openrouter" and not provider.is_mixroute(s)
    assert s.mix_router_url == "https://api.mixroute.ai/v1" and s.mix_router_key is None


def test_the_mixroute_settings_come_from_the_env_names_on_the_vm(monkeypatch):
    monkeypatch.setenv("AI_PROVIDER", "mixroute")
    monkeypatch.setenv("MIX_ROUTER_URL", "https://example.test/v1")
    monkeypatch.setenv("MIX_ROUTER_KEY", "mixroute-literal-TESTONLY-123")
    s = Settings(_env_file=None, database_url="sqlite://")
    assert provider.is_mixroute(s) and s.mix_router_url == "https://example.test/v1"
    assert provider.api_key(s).get_secret_value() == "mixroute-literal-TESTONLY-123"
    assert provider.key_name(s) == "MIX_ROUTER_KEY" and provider.label(s) == "MixRoute"


def test_an_unknown_provider_is_refused():
    with pytest.raises(ValidationError):
        Settings(_env_file=None, database_url="sqlite://", ai_provider="somewhere")


def test_repr_does_not_leak_the_mixroute_key():
    text = repr(settings(mix_router_key=SecretStr("mixroute-literal-TESTONLY-123")))
    assert "mixroute-literal-TESTONLY-123" not in text and "mix_router_key" in text


def test_the_secret_scan_catches_the_mixroute_key_wherever_it_appears(monkeypatch):
    monkeypatch.setenv("MIX_ROUTER_KEY", "mixroute-literal-TESTONLY-123")
    findings = scan_for_secrets({"events": [{"summary": "oops mixroute-literal-TESTONLY-123"}]})
    assert findings and all("TESTONLY" not in f for f in findings)


# ─── The ops API's key status ─────────────────────────────────────────────────────────────────────


def test_the_ops_api_reads_mixroutes_spend_from_the_ledger(monkeypatch):
    from worker import ops_api

    seen = []

    def fake_status(engine, now):
        seen.append(engine)
        return ledger_key_status(None, T0, spend=lambda p, s, e: Decimal("3"))

    monkeypatch.setattr(ops_api, "ledger_key_status", fake_status)
    read = ops_api.key_reader(settings(), "engine")
    assert read().usage_monthly == Decimal("3") and seen == ["engine"]


def test_the_ops_api_says_when_no_mixroute_key_is_set():
    from worker import ops_api
    from worker.ai.budget import BudgetUnreadable

    read = ops_api.key_reader(settings(mix_router_key=None), "engine")
    with pytest.raises(BudgetUnreadable, match="MixRoute"):
        read()


def test_the_ops_api_still_reads_openrouters_key_on_openrouter():
    from worker import ops_api

    s = Settings(_env_file=None, database_url="sqlite://")
    with pytest.raises(Exception, match="OpenRouter"):
        ops_api.key_reader(s, "engine")()
