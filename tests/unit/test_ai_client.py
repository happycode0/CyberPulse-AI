"""The OpenRouter client (worker/ai/client.py): request discipline, the ledger, and failures."""

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from email.utils import format_datetime

import httpx
import pytest
from pydantic import SecretStr

from worker.ai.client import (
    CHAT_URL,
    Attribution,
    BudgetExhausted,
    CallFailed,
    InvalidOutput,
    OpenRouterClient,
    RateLimited,
    _retry_after,
    check_strict_schema,
)
from worker.ai.ladder import Ladder, Tier, VerifiedLadder
from worker.settings import Settings

# Not shaped like any real key, so neither the publisher's scan nor ops/check-keys.sh flags it.
KEY = "fake-key-TESTONLY"

LADDER = Ladder.model_validate(
    {
        "tiers": {
            "tier0_free": ["vendor-a/free-model:free", "vendor-a/paid-model"],
            "tier1_cheap": ["vendor-a/paid-model", "vendor-b/backup-model"],
            "tier2_strong": ["vendor-b/strong-model"],
            "code": ["vendor-c/code-model"],
            "audit": ["vendor-d/audit-model"],
        }
    }
)

SCHEMA = {
    "type": "object",
    "properties": {
        "category": {"type": "string", "enum": ["vulnerability", "breach", "other"]},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
    },
    "required": ["category", "confidence"],
    "additionalProperties": False,
}

GOOD = {"category": "breach", "confidence": 0.9}

WHO = Attribution(agent="VOIGHT", stage="classify", event_id="evt-2026-000042", run_id="run-1")


def settings():
    return Settings(
        _env_file=None,
        database_url="sqlite://",
        openrouter_app_title="CyberPulse-AI",
        openrouter_app_url="https://example.test/cyberpulse",
        user_agent="CyberPulse-AI/1.0",
    )


def answer(content=None, *, model="vendor-a/paid-model", cost=0.0000273, finish="stop", **extra):
    """A chat completion as OpenRouter returns it."""
    message = {"role": "assistant", "content": json.dumps(GOOD) if content is None else content}
    message.update(extra.pop("message", {}))
    body = {
        "id": "gen-123",
        "model": model,
        "provider": "SomeHost",
        "choices": [{"index": 0, "finish_reason": finish, "message": message}],
        "usage": {"prompt_tokens": 210, "completion_tokens": 18, "total_tokens": 228, "cost": cost},
    }
    body.update(extra)
    return httpx.Response(200, json=body)


def error(status, message="nope", headers=None, **metadata):
    body = {"error": {"code": status, "message": message, "metadata": metadata or None}}
    return httpx.Response(status, json=body, headers=headers)


@pytest.fixture
def ledger():
    return []


@pytest.fixture
async def client(ledger):
    async with httpx.AsyncClient() as http:
        verified = VerifiedLadder(ladder=LADDER, checked_at=datetime.now(UTC))
        yield OpenRouterClient(verified, SecretStr(KEY), http, ledger.append, settings=settings())


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


def sent(route):
    return json.loads(route.calls.last.request.content)


# ─── The request: PLAN.md §7.2 on every call ──────────────────────────────────────────────────────


async def test_the_request_carries_the_whole_discipline(respx_mock, client):
    route = respx_mock.post(CHAT_URL).mock(return_value=answer())
    await call(client)
    body = sent(route)
    assert body["models"] == ["vendor-a/paid-model", "vendor-b/backup-model"]
    assert "model" not in body
    assert body["response_format"] == {
        "type": "json_schema",
        "json_schema": {"name": "classification", "strict": True, "schema": SCHEMA},
    }
    assert body["provider"] == {
        "require_parameters": True,
        "sort": "price",
        "max_price": {"completion": "1.00"},
    }
    assert body["max_tokens"] == 200
    assert "tools" not in body and "tool_choice" not in body


async def test_only_tier_2_pins_quantisation(respx_mock, client):
    route = respx_mock.post(CHAT_URL).mock(return_value=answer(model="vendor-b/strong-model"))
    await call(client, Tier.STRONG)
    assert sent(route)["provider"]["quantizations"] == ["bf16", "fp8", "unknown"]
    await call(client, Tier.FREE)
    assert "quantizations" not in sent(route)["provider"]


async def test_every_call_is_attributed_and_hidden(respx_mock, client):
    route = respx_mock.post(CHAT_URL).mock(return_value=answer())
    await call(client)
    await call(client)
    assert route.call_count == 2
    for c in route.calls:
        h = c.request.headers
        assert h["authorization"] == f"Bearer {KEY}"
        assert h["http-referer"] == "https://example.test/cyberpulse"
        assert h["x-openrouter-title"] == "CyberPulse-AI"
        assert h["x-openrouter-app-visibility"] == "hidden"
        assert h["user-agent"] == "CyberPulse-AI/1.0"


# ─── A good answer: returned, and in the ledger at the billed model and cost ──────────────────────


async def test_a_good_answer_is_returned_and_recorded(respx_mock, client, ledger):
    respx_mock.post(CHAT_URL).mock(return_value=answer(model="vendor-b/backup-model"))
    done = await call(client)
    assert done.data == GOOD
    assert done.model == "vendor-b/backup-model"
    assert done.cost_usd == Decimal("0.0000273")
    [entry] = ledger
    assert entry.outcome == "ok"
    assert (entry.provider, entry.upstream) == ("openrouter", "SomeHost")
    # Asked for the head of the chain; a fallback answered, and that is what was billed.
    assert (entry.requested_model, entry.model) == ("vendor-a/paid-model", "vendor-b/backup-model")
    assert (entry.agent, entry.stage, entry.event_id, entry.run_id) == (
        "VOIGHT",
        "classify",
        "evt-2026-000042",
        "run-1",
    )
    assert (entry.tier, entry.tokens_in, entry.tokens_out) == ("tier1_cheap", 210, 18)
    assert entry.cost_usd == Decimal("0.0000273") and entry.generation_id == "gen-123"
    assert entry.duration_ms is not None and entry.duration_ms >= 0


@pytest.mark.parametrize("cost", [None, "a lot", -1, "NaN", True])
async def test_an_unreported_cost_is_recorded_as_unknown_never_zero(
    respx_mock, client, ledger, cost
):
    respx_mock.post(CHAT_URL).mock(return_value=answer(cost=cost))
    done = await call(client)
    assert done.cost_usd is None and ledger[0].cost_usd is None


async def test_a_free_call_is_recorded_as_zero(respx_mock, client, ledger):
    respx_mock.post(CHAT_URL).mock(return_value=answer(model="vendor-a/free-model:free", cost=0))
    await call(client, Tier.FREE)
    assert ledger[0].cost_usd == Decimal("0")


# ─── A billed but unusable answer: raised, and still in the ledger ────────────────────────────────


@pytest.mark.parametrize(
    "response, reason",
    [
        (answer("SECRET-LOOKING-OUTPUT is not json"), "not JSON"),
        (
            answer(json.dumps({"category": "SECRET-LOOKING-OUTPUT", "confidence": 2})),
            "fails the schema",
        ),
        (answer(json.dumps({"category": "breach"})), "fails the schema"),
        (answer(json.dumps({**GOOD, "extra": "SECRET-LOOKING-OUTPUT"})), "fails the schema"),
        (answer('{"category": "bre', finish="length"), "finish_reason 'length'"),
        (answer("", message={"refusal": "SECRET-LOOKING-OUTPUT"}), "refused"),
        (answer(""), "no content"),
    ],
)
async def test_an_unusable_answer_is_raised_and_still_billed(
    respx_mock, client, ledger, response, reason
):
    respx_mock.post(CHAT_URL).mock(return_value=response)
    with pytest.raises(InvalidOutput, match=reason) as exc:
        await call(client)
    # The reason says what was wrong without quoting the output, which is unvetted text.
    assert "SECRET-LOOKING-OUTPUT" not in str(exc.value)
    [entry] = ledger
    assert entry.outcome == "invalid_output" and entry.cost_usd == Decimal("0.0000273")


async def test_a_failed_ledger_write_is_not_swallowed(respx_mock, ledger):
    def broken(entry):
        raise RuntimeError("database is down")

    respx_mock.post(CHAT_URL).mock(return_value=answer())
    async with httpx.AsyncClient() as http:
        verified = VerifiedLadder(ladder=LADDER, checked_at=datetime.now(UTC))
        c = OpenRouterClient(verified, SecretStr(KEY), http, broken, settings=settings())
        with pytest.raises(RuntimeError, match="database is down"):
            await call(c)


# ─── Failures: typed, never billed, never quoting a credential ────────────────────────────────────


@pytest.mark.parametrize(
    "source, transient",
    [
        ("openrouter_in_flight_budget", True),
        ("openrouter_key_limit", False),
        ("openrouter_credits", False),
        (None, False),
    ],
)
async def test_a_402_says_which_limit_was_hit(respx_mock, client, ledger, source, transient):
    metadata = {"limit_source": source} if source else {}
    response = error(402, headers={"Retry-After": "7"}, **metadata)
    respx_mock.post(CHAT_URL).mock(return_value=response)
    with pytest.raises(BudgetExhausted) as exc:
        await call(client)
    assert exc.value.limit_source == source and exc.value.transient is transient
    assert exc.value.retry_after == 7.0 and exc.value.status == 402
    assert ledger == []


async def test_a_429_is_rate_limited_with_its_retry_after(respx_mock, client, ledger):
    respx_mock.post(CHAT_URL).mock(return_value=error(429, headers={"Retry-After": "30"}))
    with pytest.raises(RateLimited) as exc:
        await call(client)
    assert exc.value.retry_after == 30.0 and ledger == []


@pytest.mark.parametrize(
    "response, status",
    [
        (error(503, "no provider available"), 503),
        (httpx.Response(502, text="<html>bad gateway</html>"), 502),
        (error(400, "max_price excludes every provider"), 400),
        (httpx.Response(200, text="<html>maintenance</html>"), 200),
        (httpx.Response(200, json={"error": {"code": 502, "message": "upstream died"}}), 502),
        (httpx.Response(200, json={"choices": []}), 200),
    ],
)
async def test_other_failures_are_call_failed(respx_mock, client, ledger, response, status):
    respx_mock.post(CHAT_URL).mock(return_value=response)
    with pytest.raises(CallFailed) as exc:
        await call(client)
    assert type(exc.value) is CallFailed and exc.value.status == status
    assert ledger == []


async def test_a_network_failure_has_no_status(respx_mock, client, ledger):
    respx_mock.post(CHAT_URL).mock(side_effect=httpx.ConnectError("refused"))
    with pytest.raises(CallFailed) as exc:
        await call(client)
    assert exc.value.status is None and "ConnectError" in str(exc.value)


async def test_error_text_echoing_the_key_is_withheld(respx_mock, client, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", KEY)
    respx_mock.post(CHAT_URL).mock(return_value=error(401, f"key {KEY} is not valid"))
    with pytest.raises(CallFailed) as exc:
        await call(client)
    assert KEY not in str(exc.value) and "withheld" in str(exc.value)


async def test_long_error_text_is_cut_short(respx_mock, client):
    respx_mock.post(CHAT_URL).mock(return_value=error(400, "word " * 200))
    with pytest.raises(CallFailed) as exc:
        await call(client)
    assert len(str(exc.value)) <= 200


def test_retry_after_may_be_seconds_or_a_date():
    assert _retry_after("12") == 12.0
    assert _retry_after(None) is None and _retry_after("soon") is None
    later = format_datetime(datetime.now(UTC) + timedelta(seconds=90), usegmt=True)
    assert 80 <= _retry_after(later) <= 90
    earlier = format_datetime(datetime.now(UTC) - timedelta(seconds=90), usegmt=True)
    assert _retry_after(earlier) == 0.0


# ─── Wrong before it is sent: refused without a request ───────────────────────────────────────────


def loose(**changes):
    return {**SCHEMA, **changes}


@pytest.mark.parametrize(
    "schema, problem",
    [
        ({k: v for k, v in SCHEMA.items() if k != "additionalProperties"}, "additionalProperties"),
        (loose(required=["category"]), "require every property"),
        (
            loose(
                properties={
                    **SCHEMA["properties"],
                    "nested": {"type": "object", "properties": {"x": {"type": "string"}}},
                },
                required=["category", "confidence", "nested"],
            ),
            "#/properties/nested",
        ),
        ({"type": "array", "items": {"type": "string"}}, "object at the root"),
    ],
)
async def test_a_schema_strict_mode_cannot_take_is_refused(respx_mock, client, schema, problem):
    route = respx_mock.post(CHAT_URL).mock(return_value=answer())
    with pytest.raises(ValueError, match=problem):
        await call(client, schema=schema)
    assert route.call_count == 0


@pytest.mark.parametrize(
    "overrides", [{"schema_name": "has spaces"}, {"schema_name": ""}, {"max_tokens": 0}]
)
async def test_a_malformed_request_is_refused(respx_mock, client, overrides):
    route = respx_mock.post(CHAT_URL).mock(return_value=answer())
    with pytest.raises(ValueError):
        await call(client, **overrides)
    assert route.call_count == 0


def test_a_strict_schema_passes_the_check():
    check_strict_schema(SCHEMA)
    check_strict_schema(
        {
            "type": "object",
            "properties": {
                "items": {
                    "type": "array",
                    "items": {
                        "type": ["object", "null"],
                        "properties": {"id": {"type": "string"}},
                        "required": ["id"],
                        "additionalProperties": False,
                    },
                }
            },
            "required": ["items"],
            "additionalProperties": False,
        }
    )


# ─── Narrowing the chain: what the budget does when only some models may be used ─────────────────


async def test_models_can_narrow_the_chain(respx_mock, client, ledger):
    route = respx_mock.post(CHAT_URL).mock(return_value=answer(model="vendor-b/backup-model"))
    await call(client, models=["vendor-b/backup-model"])
    assert sent(route)["models"] == ["vendor-b/backup-model"]
    assert ledger[0].requested_model == "vendor-b/backup-model"


@pytest.mark.parametrize(
    "models",
    [
        [],
        ["vendor-x/never-checked"],
        ["vendor-b/backup-model", "vendor-a/paid-model"],
        ["vendor-a/paid-model", "vendor-a/paid-model"],
    ],
)
async def test_models_can_only_drop_from_the_chain(respx_mock, client, models):
    route = respx_mock.post(CHAT_URL).mock(return_value=answer())
    with pytest.raises(ValueError, match="part of the tier1_cheap chain"):
        await call(client, models=models)
    assert route.call_count == 0
