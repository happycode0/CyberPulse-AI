"""Incident diagnosis (worker/ai/diagnosis.py): what the call is given, what it may answer, and
when one is made. No network and no database: the client, the counts and the store are fakes."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from worker.ai import diagnosis as dx
from worker.ai.budget import Governor, KeyStatus
from worker.ai.client import Completion
from worker.ai.ladder import Ladder, Tier, VerifiedLadder
from worker.ai.tasks import Rejected
from worker.db.incidents import Incident
from worker.models import Severity

NOW = datetime(2026, 10, 31, 10, 2, tzinfo=UTC)
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
RUNBOOK = """# Watchdog incidents

## For every incident

Read the evidence first.

## feed-failing

One feed keeps failing. Check its URL with curl; a moved feed gets a new URL in
config/sources.yaml.

## push-failure

The publish token has expired.
"""


def incident(**over) -> Incident:
    values = dict(
        id=7, kind="feed-failing", subject="acsc-alerts", severity="high", status="open",
        title="Acsc Alerts has failed its last 5 checks", evidence={"failures_in_a_row": 5},
        opened_at=NOW - timedelta(minutes=10), last_seen=NOW, checks=3, clear_since=None,
        resolved_at=None, fix_failures=0, needs_human=False, reopened=0,
    )
    return Incident(**{**values, **over})


def test_the_runbook_section_for_its_kind_is_found(tmp_path):
    path = tmp_path / "watchdog-incidents.md"
    path.write_text(RUNBOOK, encoding="utf-8")
    section = dx.runbook_section("feed-failing", path)
    assert section.startswith("One feed keeps failing") and "push-failure" not in section
    assert dx.runbook_section("site-stale", path) == ""
    assert dx.runbook_section("feed-failing", tmp_path / "missing.md") == ""


def test_the_call_carries_the_incident_and_its_runbook_and_never_the_system_prompt():
    call = dx.diagnosis_call(incident(), runbook="One feed keeps failing. Check its URL.")
    assert call.stage == "incident.diagnosis" and call.work.tier is Tier.CHEAP
    assert call.work.severity is Severity.HIGH
    assert call.schema["properties"]["owner"]["enum"] == ["board", "code"]
    user = call.messages[1]["content"]
    assert "acsc-alerts" in user and "failures_in_a_row" in user and "Check its URL" in user
    assert "acsc-alerts" not in call.messages[0]["content"]


def test_a_critical_incident_is_routed_as_critical():
    assert dx.diagnosis_call(incident(severity="critical"), runbook="").work.severity \
        is Severity.CRITICAL


GOOD = {
    "likely_cause": "The ACSC alerts feed has moved or is blocking the worker's requests.",
    "suggested_fix": "Fetch the feed by hand; if it moved, put the new address in the "
    "source registry.",
    "owner": "code",
}


def test_a_good_answer_is_kept_as_it_is():
    out = dx.diagnosis_call(incident(), runbook="").parse(GOOD)
    assert out == GOOD


@pytest.mark.parametrize("field", ["likely_cause", "suggested_fix"])
def test_a_url_in_the_answer_is_refused(field):
    bad = {**GOOD, field: "See https://evil.example/fix for what to do about the feed."}
    with pytest.raises(Rejected, match="URL"):
        dx.diagnosis_call(incident(), runbook="").parse(bad)


# ─── When one is made ─────────────────────────────────────────────────────────────────────────────


class FakeLayer:
    settings = None

    def __init__(self, governor):
        self._governor = governor

    async def ready(self, http):
        return self._governor


def governor(usage="0") -> Governor:
    g = Governor(VerifiedLadder(ladder=LADDER, checked_at=NOW), Decimal("12"), now=lambda: NOW)
    g.observe(KeyStatus(limit=None, limit_remaining=None, limit_reset=None,
                        usage_daily=Decimal(0), usage_monthly=Decimal(usage),
                        free_requests_remaining=None))
    return g


@pytest.fixture
def made(monkeypatch):
    state = {"today": 0, "stored": [], "calls": 0}

    class Client:
        label = "MixRoute"

        async def complete(self, tier, **kwargs):
            state["calls"] += 1
            return Completion(data=GOOD, model="vendor-b/cheap", upstream=None, tokens_in=1200,
                              tokens_out=150, cost_usd=Decimal("0.0002"), generation_id=None)

    monkeypatch.setattr(dx, "_made_today", lambda engine, now: state["today"])
    monkeypatch.setattr(dx, "_store", lambda engine, incident_id, diagnosis:
                        state["stored"].append((incident_id, diagnosis)))
    monkeypatch.setattr(dx, "_client", lambda layer, http, engine: Client())
    monkeypatch.setattr(dx, "runbook_section", lambda kind, path=None: "Check its URL.")
    return state


async def test_a_diagnosis_is_made_stored_and_returned(made):
    out = await dx.diagnose_incident(None, incident(), NOW, layer=FakeLayer(governor()))
    assert out["likely_cause"] == GOOD["likely_cause"] and out["owner"] == "code"
    assert out["model"] == "vendor-b/cheap" and out["at"] == NOW.isoformat()
    assert made["stored"] == [(7, out)]


async def test_the_days_cap_holds_further_diagnoses_back(made):
    made["today"] = dx.MAX_PER_DAY
    assert await dx.diagnose_incident(None, incident(), NOW, layer=FakeLayer(governor())) is None
    assert made["calls"] == 0


async def test_with_the_ai_layer_off_there_is_no_diagnosis(made):
    assert await dx.diagnose_incident(None, incident(), NOW, layer=FakeLayer(None)) is None


async def test_with_no_money_for_it_there_is_no_diagnosis(made):
    assert await dx.diagnose_incident(None, incident(), NOW, layer=FakeLayer(governor("11"))) \
        is None
    assert made["calls"] == 0
