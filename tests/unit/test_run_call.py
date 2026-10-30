"""One structured call that is not an event's enrichment task (worker/ai/enrich.py `run_call`): the
worker's daily report, follow-up reports and incident diagnoses. It keeps `run_task`'s rules,
because `run_task` is now one use of it. No network: the client is a fake."""

from datetime import UTC, datetime
from decimal import Decimal

from worker.ai.budget import Governor, KeyStatus, Work
from worker.ai.client import BudgetExhausted, Completion, InvalidOutput
from worker.ai.enrich import Call, Done, Failed, Waiting, run_call
from worker.ai.ladder import Ladder, Tier, VerifiedLadder
from worker.ai.tasks import Rejected
from worker.models import Severity

T0 = datetime(2026, 10, 31, 1, 0, tzinfo=UTC)
LADDER = Ladder.model_validate(
    {
        "tiers": {
            "tier0_free": ["vendor-a/one:free", "vendor-a/paid"],
            "tier1_cheap": ["vendor-a/paid", "vendor-b/cheap"],
            "tier2_strong": ["vendor-b/strong", "vendor-b/cheap"],
            "code": ["vendor-c/code"],
            "audit": ["vendor-d/audit"],
        }
    }
)
SCHEMA = {
    "type": "object",
    "properties": {"note": {"type": "string"}},
    "required": ["note"],
    "additionalProperties": False,
}


def governor(usage_monthly="0", budget="20") -> Governor:
    g = Governor(VerifiedLadder(ladder=LADDER, checked_at=T0), Decimal(budget), now=lambda: T0)
    g.observe(
        KeyStatus(
            limit=Decimal("20"),
            limit_remaining=Decimal("20") - Decimal(usage_monthly),
            limit_reset="monthly",
            usage_daily=Decimal("0"),
            usage_monthly=Decimal(usage_monthly),
            free_requests_remaining=988,
        )
    )
    return g


def completion(data, model="vendor-b/cheap") -> Completion:
    return Completion(
        data=data, model=model, upstream=None, tokens_in=100, tokens_out=50,
        cost_usd=Decimal("0.0001"), generation_id=None,
    )


class FakeClient:
    label = "MixRoute"

    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = []

    async def complete(self, tier, *, schema_name, attribution, models, messages, **_):
        self.calls.append((tier, models, attribution, schema_name, messages))
        reply = self.replies.pop(0) if self.replies else None
        if isinstance(reply, BaseException):
            raise reply
        return reply or completion({"note": "fine"})


def parse(data):
    if data["note"] == "bad":
        raise Rejected("the note is bad")
    return data["note"].upper()


def call(tier=Tier.CHEAP, severity=Severity.UNKNOWN, editorial=True) -> Call:
    return Call(
        stage="report.daily",
        work=Work(tier, severity),
        schema_name="report_daily",
        schema=SCHEMA,
        messages=[{"role": "user", "content": "{}"}],
        parse=parse,
        editorial=editorial,
    )


async def test_a_good_answer_is_parsed_and_attributed_to_its_stage():
    client = FakeClient()
    outcome = await run_call(client, governor(), call())
    assert outcome == Done("FINE", "vendor-b/cheap")
    [(tier, models, attribution, schema_name, _)] = client.calls
    assert tier is Tier.CHEAP and models == LADDER.tiers[Tier.CHEAP]
    assert (attribution.agent, attribution.stage, attribution.event_id) == (
        "worker", "report.daily", None,
    )
    assert schema_name == "report_daily"


async def test_a_rejected_answer_gets_one_retry_then_fails_with_our_reason():
    client = FakeClient(completion({"note": "bad"}, "m1"), completion({"note": "bad"}, "m2"))
    outcome = await run_call(client, governor(), call())
    assert outcome == Failed("the note is bad", "m2")
    assert len(client.calls) == 2


async def test_an_unusable_answer_then_a_good_one_is_done():
    client = FakeClient(InvalidOutput("not the schema", model="m1"))
    assert isinstance(await run_call(client, governor(), call()), Done)


async def test_a_spent_quota_waits_and_tells_the_governor():
    g = governor()
    client = FakeClient(
        BudgetExhausted("403", limit_source="mixroute_key_quota", retry_after=None),
        BudgetExhausted("403", limit_source="mixroute_key_quota", retry_after=None),
        BudgetExhausted("403", limit_source="mixroute_key_quota", retry_after=None),
    )
    outcome = await run_call(client, g, call())
    assert isinstance(outcome, Waiting)


async def test_editorial_work_waits_when_only_tier_0_is_open():
    """Under MINIMAL the governor sends what matters to tier 0, and editorial work never runs
    there (PLAN.md §8): the report, a follow-up and a diagnosis all wait for money."""
    client = FakeClient()
    outcome = await run_call(client, governor("19"), call(severity=Severity.CRITICAL))
    assert isinstance(outcome, Waiting) and client.calls == []
