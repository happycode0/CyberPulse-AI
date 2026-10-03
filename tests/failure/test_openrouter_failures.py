"""§11 "OpenRouter down/exhausted": the deterministic pipeline goes on, the event stays
`pending_enrichment`, and it is enriched later. §11 "Bad model output": rejected, retried once,
then left pending. And the §7.4 budget tiers.

The real OpenRouterClient, Governor and run_task, over an httpx.MockTransport.
"""

import json
from datetime import timedelta
from types import SimpleNamespace

import httpx
import pytest
from pydantic import SecretStr

from tests.unit.test_assemble import item, source
from tests.unit.test_enrich import ANSWERS, T0, VERIFIED, governor, subject
from worker import scheduler
from worker.ai.budget import Mode, Work
from worker.ai.client import OpenRouterClient
from worker.ai.enrich import Done, Failed, Waiting, run_task
from worker.ai.ladder import Tier
from worker.ai.tasks import BRIEF, TRIAGE
from worker.db.jobs import JobRun
from worker.models import Severity
from worker.pipeline.assemble import build_new_event
from worker.settings import Settings

from .conftest import KEY


def answer(request, data):
    model = json.loads(request.content)["models"][0]
    body = {
        "id": "gen-1",
        "model": model,
        "provider": "SomeHost",
        "choices": [{"index": 0, "finish_reason": "stop",
                     "message": {"role": "assistant", "content": json.dumps(data)}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15, "cost": 0.0},
    }  # fmt: skip
    return httpx.Response(200, json=body)


def refusal(status, **metadata):
    body = {"error": {"code": status, "message": "nope", "metadata": metadata or None}}
    return httpx.Response(status, json=body)


async def run(handler, task, g=None, **event):
    requests, ledger = [], []

    def record(request):
        requests.append(json.loads(request.content)["models"])
        return handler(request)

    settings = Settings(_env_file=None, database_url="sqlite://")
    async with httpx.AsyncClient(transport=httpx.MockTransport(record)) as http:
        client = OpenRouterClient(VERIFIED, SecretStr(KEY), http, ledger.append, settings=settings)
        outcome = await run_task(client, g or governor(), task, subject(**event))
    return outcome, requests, ledger


def test_a_new_event_is_built_with_no_model_and_starts_pending_enrichment():
    """The deterministic pipeline builds the event; AI only adds to it, later."""
    event = build_new_event(item(), source(), "evt-2026-000001", now=T0)
    assert event.pending_enrichment is True and event.title


@pytest.mark.parametrize(
    "fail",
    [
        lambda r: httpx.Response(503),
        lambda r: refusal(500),
        lambda r: (_ for _ in ()).throw(httpx.ReadTimeout("slow", request=r)),
        lambda r: (_ for _ in ()).throw(httpx.ConnectError("refused", request=r)),
    ],
    ids=["503", "500", "timeout", "connect"],
)
async def test_openrouter_down_leaves_the_task_waiting_for_a_later_pass(fail):
    outcome, requests, ledger = await run(fail, TRIAGE)
    assert isinstance(outcome, Waiting) and "the call failed" in outcome.reason
    # One try, no retry storm, nothing billed: the next enrichment pass tries again.
    assert len(requests) == 1 and ledger == []


async def test_a_free_model_429_falls_through_to_tier_one():
    def handler(request):
        models = json.loads(request.content)["models"]
        if any(m.endswith(":free") for m in models):
            return refusal(429)
        return answer(request, ANSWERS[TRIAGE.schema_name])

    outcome, requests, _ = await run(handler, TRIAGE)
    assert isinstance(outcome, Done)
    assert any(m.endswith(":free") for m in requests[0])
    assert not any(m.endswith(":free") for m in requests[1])


async def test_a_paid_429_pauses_paid_calls_and_editorial_work_waits():
    g = governor()
    outcome, requests, _ = await run(lambda r: refusal(429), BRIEF, g)
    assert isinstance(outcome, Waiting) and len(requests) == 1
    assert g.mode()[0] is Mode.FREE_ONLY


@pytest.mark.parametrize(
    "limit, mode", [("openrouter_credits", Mode.OFF), ("openrouter_key_limit", Mode.FREE_ONLY)]
)
async def test_an_exhausted_budget_stops_or_narrows_spending(limit, mode):
    g = governor()
    outcome, _, _ = await run(lambda r: refusal(402, limit_source=limit), BRIEF, g)
    assert isinstance(outcome, Waiting) and g.mode()[0] is mode


async def test_bad_output_is_retried_once_then_left_pending():
    outcome, requests, ledger = await run(lambda r: answer(r, {"nonsense": True}), TRIAGE)
    assert isinstance(outcome, Failed) and len(requests) == 2
    assert [e.outcome for e in ledger] == ["invalid_output", "invalid_output"]


async def test_bad_output_then_a_good_answer_is_done():
    replies = iter([{"nonsense": True}, ANSWERS[TRIAGE.schema_name]])
    outcome, requests, _ = await run(lambda r: answer(r, next(replies)), TRIAGE)
    assert isinstance(outcome, Done) and len(requests) == 2


async def test_an_enrichment_pass_that_raises_is_recorded_as_failed(monkeypatch):
    recorded: list[JobRun] = []

    async def record(run):
        recorded.append(run)

    async def boom(**kwargs):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(scheduler, "_record", record)

    async def nothing(**kwargs):
        return SimpleNamespace(changed_anything=False, failed=0)

    monkeypatch.setattr(scheduler, "_layer", lambda: None)
    monkeypatch.setattr(scheduler, "enrich_pending", boom)
    monkeypatch.setattr(scheduler, "suggest_techniques", nothing)
    await scheduler._enrich_job()
    assert [(r.job, r.completed) for r in recorded] == [("enrichment", False)]


# ─── §7.4: budget remaining → behaviour ───────────────────────────────────────────────────────────


def route(usage, tier, severity=Severity.MEDIUM, **work):
    return governor(usage).route(Work(tier, severity, **work))


def test_over_half_the_budget_left_is_the_full_ladder():
    assert governor("0").mode()[0] is Mode.FULL
    assert route("0", Tier.STRONG).tier is Tier.STRONG


def test_half_or_less_left_keeps_tier_two_for_critical_and_kev_only():
    assert governor("12").mode()[0] is Mode.CONSERVE
    assert route("12", Tier.STRONG).tier is Tier.CHEAP
    assert route("12", Tier.STRONG, Severity.CRITICAL).tier is Tier.STRONG
    assert route("12", Tier.STRONG, kev=True).tier is Tier.STRONG


def test_under_a_fifth_left_is_tier_zero_for_what_matters_most_only():
    assert governor("17").mode()[0] is Mode.MINIMAL
    assert route("17", Tier.CHEAP) is None
    assert route("17", Tier.CHEAP, Severity.HIGH).tier is Tier.FREE
    assert route("17", Tier.STRONG, developing=True).tier is Tier.FREE


def test_an_exhausted_budget_is_free_models_only():
    assert governor("20").mode()[0] is Mode.FREE_ONLY
    r = route("20", Tier.CHEAP, Severity.CRITICAL)
    assert r.tier is Tier.FREE and r.models and all(m.endswith(":free") for m in r.models)


def test_a_stale_budget_reading_falls_back_to_free_only():
    g = governor("0")
    g._now = lambda: T0 + timedelta(days=2)
    assert g.mode()[0] is Mode.FREE_ONLY
