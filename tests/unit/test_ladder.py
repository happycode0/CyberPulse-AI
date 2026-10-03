"""The model ladder and the price-ceiling guard (worker/ai/ladder.py)."""

import json
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError

from worker.ai.ladder import (
    DEFAULT_LADDER_PATH,
    ENDPOINTS_URL,
    CatalogueUnavailable,
    Ladder,
    LadderRejected,
    Tier,
    VerifiedLadder,
    breach_reason,
    check_ladder,
    verify_ladder,
)

FIXTURE = Path(__file__).parent / "fixtures" / "openrouter_routes_2026-10-03.json"
CAPABLE = ["tools", "structured_outputs", "response_format", "max_tokens"]


@pytest.fixture(scope="module")
def captured():
    """Real routes, trimmed, as OpenRouter listed them on 2026-10-03."""
    return json.loads(FIXTURE.read_text())["routes"]


def route(out="0.00000028", prompt="0.00000014", quantization="fp8", params=CAPABLE):
    return {
        "tag": "test",
        "quantization": quantization,
        "pricing": {"prompt": prompt, "completion": out},
        "supported_parameters": list(params),
    }


def tiers(**overrides):
    base = {
        "tier0_free": ["vendor-a/free-model:free", "vendor-a/paid-model"],
        "tier1_cheap": ["vendor-a/paid-model"],
        "tier2_strong": ["vendor-b/strong-model"],
        "code": ["vendor-c/code-model"],
        "audit": ["vendor-d/audit-model"],
    }
    base.update(overrides)
    return {"tiers": base}


def ladder(**overrides):
    return Ladder.model_validate(tiers(**overrides))


def routes_for(lad, **specific):
    """One capable route per model, unless a model is given its own list."""
    return {slug: specific.get(slug, [route()]) for slug in lad.slugs()}


# ─── The exit test: an over-priced model is rejected (PLAN.md §9, Stage 2) ────────────────────────


def test_an_over_priced_model_is_rejected():
    lad = ladder(tier1_cheap=["vendor-a/paid-model", "vendor-e/pricey"])
    breaches = check_ladder(lad, routes_for(lad, **{"vendor-e/pricey": [route(out="0.00000128")]}))
    assert [(b.tier, b.slug) for b in breaches] == [(Tier.CHEAP, "vendor-e/pricey")]
    assert "$1.28/M output" in breaches[0].reason and "ceiling" in breaches[0].reason


def test_exactly_at_the_ceiling_passes():
    assert breach_reason([route(out="0.000001")], Tier.CHEAP) is None


def test_a_hair_over_the_ceiling_fails():
    assert breach_reason([route(out="0.0000010001")], Tier.CHEAP) is not None


def test_real_over_priced_models_are_rejected(captured):
    """gemini-3.8-flash at $1.88; mimo-v2.5 lists $0.28 but every capable route is $2.00."""
    for slug in ("google/gemini-3.8-flash", "xiaomi/mimo-v2.5"):
        reason = breach_reason(captured[slug], Tier.CHEAP)
        assert reason is not None and "over the $1.00/M ceiling" in reason, slug


def test_the_check_is_per_route_not_the_headline_price(captured):
    """0731 is listed at $0.008/$1.28, which is relace/fp4's price, and that route has no structured
    outputs. Some capable routes are over the ceiling too (open-inference, $1.60); others are not.
    """
    routes = captured["deepseek/deepseek-v4-flash-0731"]
    headline = next(r for r in routes if r["tag"] == "relace/fp4")
    assert headline["pricing"]["completion"] == "0.00000128"
    assert "structured_outputs" not in headline["supported_parameters"]
    capable_over = [
        r
        for r in routes
        if "structured_outputs" in r["supported_parameters"]
        and float(r["pricing"]["completion"]) * 1e6 > 1.0
    ]
    assert capable_over
    assert breach_reason(routes, Tier.CHEAP) is None


def test_a_cheap_route_without_structured_outputs_does_not_count():
    routes = [route(out="0.0000001", params=["tools"]), route(out="0.0000015")]
    assert "over the $1.00/M ceiling" in breach_reason(routes, Tier.CHEAP)


def test_a_model_without_structured_outputs_anywhere_is_rejected(captured):
    reason = breach_reason(captured["minimax/minimax-m2.7"], Tier.STRONG)
    assert reason == "no route offers both tools and structured outputs"


def test_tools_are_required_too():
    assert breach_reason([route(params=["structured_outputs"])], Tier.CHEAP) is not None


def test_tier2_needs_a_capable_route_at_a_pinned_quantisation():
    fp4_only = [route(quantization="fp4")]
    assert breach_reason(fp4_only, Tier.CHEAP) is None
    assert "quantisation" in breach_reason(fp4_only, Tier.STRONG)
    assert breach_reason([route(quantization="unknown")], Tier.STRONG) is None


def test_the_same_model_can_pass_one_tier_and_fail_another():
    lad = ladder(tier1_cheap=["vendor-b/strong-model"])
    breaches = check_ladder(lad, routes_for(lad, **{"vendor-b/strong-model": [route(quantization="fp4")]}))
    assert [(b.tier, b.slug) for b in breaches] == [(Tier.STRONG, "vendor-b/strong-model")]


@pytest.mark.parametrize("bad", ["-1", None, "", "free", "NaN", "Infinity"])
def test_a_price_that_is_not_fixed_does_not_pass(bad):
    """-1 is how OpenRouter marks a router priced by whatever model it picks."""
    assert breach_reason([route(out=bad)], Tier.CHEAP) is not None
    assert breach_reason([route(prompt=bad)], Tier.CHEAP) is not None


def test_free_routes_pass(captured):
    assert breach_reason(captured["nvidia/nemotron-3-super-120b-a12b:free"], Tier.FREE) is None


def test_a_model_openrouter_does_not_list_is_rejected():
    lad = ladder()
    routes = routes_for(lad)
    routes["vendor-c/code-model"] = None
    assert [b.slug for b in check_ladder(lad, routes)] == ["vendor-c/code-model"]


def test_every_breach_is_reported_not_only_the_first():
    lad = ladder()
    over = [route(out="0.000002")]
    breaches = check_ladder(lad, {slug: over for slug in lad.slugs()})
    assert len(breaches) == sum(len(chain) for chain in lad.tiers.values())


# ─── The ladder file itself, checked without the network ───────────────────────────────────────────


def test_the_shipped_ladder_loads():
    lad = Ladder.load(DEFAULT_LADDER_PATH)
    assert set(lad.tiers) == set(Tier)


def test_a_missing_tier_is_refused():
    data = tiers()
    del data["tiers"]["audit"]
    with pytest.raises(ValidationError, match="tiers missing"):
        Ladder.model_validate(data)


def test_an_unknown_key_is_refused():
    with pytest.raises(ValidationError):
        Ladder.model_validate({**tiers(), "max_output_price_per_mtok": 5})


@pytest.mark.parametrize(
    "slug, message",
    [
        ("openai/gpt-6-luna:batch", "batch"),
        ("openrouter/free", "router"),
        ("openrouter/auto", "router"),
        ("gpt-oss-20b", "author/model"),
    ],
)
def test_ineligible_slugs_are_refused(slug, message):
    with pytest.raises(ValidationError, match=message):
        ladder(tier1_cheap=[slug])


def test_tier0_must_end_in_a_paid_model():
    with pytest.raises(ValidationError, match="paid"):
        ladder(tier0_free=["vendor-a/one:free", "vendor-a/two:free"])


def test_code_and_audit_must_not_share_a_vendor_anywhere_in_the_chain():
    with pytest.raises(ValidationError, match="share a vendor"):
        ladder(code=["vendor-c/code-model"], audit=["vendor-d/audit-model", "vendor-c/other"])


def test_a_model_named_twice_in_one_chain_is_refused():
    with pytest.raises(ValidationError, match="twice"):
        ladder(tier1_cheap=["vendor-a/paid-model", "vendor-a/paid-model"])


def test_an_empty_chain_is_refused():
    with pytest.raises(ValidationError, match="empty"):
        ladder(code=[])


# ─── verify_ladder: fetch, check, fail closed ──────────────────────────────────────────────────────


def mock_catalogue(respx_mock, lad, **overrides):
    for slug in lad.slugs():
        response = overrides.get(slug, httpx.Response(200, json={"data": {"endpoints": [route()]}}))
        respx_mock.get(ENDPOINTS_URL.format(slug=slug)).mock(return_value=response)


async def test_a_passing_ladder_comes_back_verified(respx_mock):
    lad = ladder()
    mock_catalogue(respx_mock, lad)
    verified = await verify_ladder(lad)
    assert isinstance(verified, VerifiedLadder) and verified.ladder is lad


async def test_an_over_priced_model_raises_ladder_rejected(respx_mock):
    lad = ladder()
    pricey = httpx.Response(200, json={"data": {"endpoints": [route(out="0.0000012")]}})
    mock_catalogue(respx_mock, lad, **{"vendor-d/audit-model": pricey})
    with pytest.raises(LadderRejected) as exc:
        await verify_ladder(lad)
    assert [b.slug for b in exc.value.breaches] == ["vendor-d/audit-model"]


async def test_a_404_is_a_withdrawn_model_not_an_outage(respx_mock):
    lad = ladder()
    mock_catalogue(respx_mock, lad, **{"vendor-c/code-model": httpx.Response(404)})
    with pytest.raises(LadderRejected, match="not on OpenRouter"):
        await verify_ladder(lad)


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(503),
        httpx.Response(200, text="<html>maintenance</html>"),
        httpx.Response(200, json={"data": {"endpoints": "none"}}),
    ],
)
async def test_an_unreadable_catalogue_fails_closed(respx_mock, response):
    lad = ladder()
    mock_catalogue(respx_mock, lad, **{"vendor-b/strong-model": response})
    with pytest.raises(CatalogueUnavailable):
        await verify_ladder(lad)


async def test_a_network_failure_fails_closed(respx_mock):
    lad = ladder()
    mock_catalogue(respx_mock, lad)
    respx_mock.get(ENDPOINTS_URL.format(slug="vendor-b/strong-model")).mock(
        side_effect=httpx.ConnectTimeout("down")
    )
    with pytest.raises(CatalogueUnavailable):
        await verify_ladder(lad)


async def test_the_check_sends_no_credentials(respx_mock, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "fake-key-TESTONLY")
    lad = ladder()
    mock_catalogue(respx_mock, lad)
    await verify_ladder(lad)
    for call in respx_mock.calls:
        assert "authorization" not in {k.lower() for k in call.request.headers}
        assert "TESTONLY" not in str(call.request.url)
