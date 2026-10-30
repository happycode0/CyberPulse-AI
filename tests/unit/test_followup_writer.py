"""The worker's follow-up writer (worker/ai/followup_writer.py): what a call is shown, what a report
may say, and what a pass does. No network and no database: the loads, writes and client are
fakes."""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from worker.ai import followup_writer as fw
from worker.ai.budget import Governor, KeyStatus
from worker.ai.client import Completion
from worker.ai.ladder import Ladder, Tier, VerifiedLadder
from worker.ai.tasks import Rejected
from worker.db.followup import DueTask, EvidenceSource, Submitted
from worker.models import Event, MaterialChange, Severity
from worker.pipeline.followup import EventContext, Report

NOW = datetime(2026, 10, 31, 9, 45, tzinfo=UTC)
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
SUMMARY = "Acme released a fixed SecureGate build that closes the flaw attackers used."


def event(**over) -> Event:
    return Event(
        event_id="evt-2026-000001", first_seen=NOW - timedelta(days=2),
        last_seen=NOW - timedelta(hours=1), title="SecureGate VPN flaw exploited",
        summary="Attackers used a SecureGate VPN flaw against hospitals.",
        severity=Severity.HIGH, **over,
    )


def task(kind="check", last=NOW - timedelta(hours=12)) -> DueTask:
    return DueTask(7, kind, "evt-2026-000001", NOW - timedelta(hours=1), 0, None, last)


SOURCES = [
    EvidenceSource("s1", "acme_psirt", "https://acme.example/advisories/sg-2026-1", "Acme fixes "
                   "SecureGate flaw", NOW - timedelta(hours=3), "VENDOR"),
    EvidenceSource("s2", "itnews", "https://itnews.example/sg", "Hospitals hit by VPN bug",
                   NOW - timedelta(hours=5), "NEWS"),
]
CONTEXT = EventContext(cves=frozenset(), types=frozenset({MaterialChange.NEW_FACT}),
                       summaries=frozenset(), agent_entries=0, first_seen=date(2026, 10, 29))


# ─── The call ─────────────────────────────────────────────────────────────────────────────────────


def test_a_check_shows_the_reports_by_ref_and_never_their_urls():
    call = fw.followup_call(task(), event(), SOURCES, CONTEXT, today=NOW.date())
    assert call.stage == "followup.report" and call.event_id == "evt-2026-000001"
    assert call.work.tier is Tier.CHEAP and call.work.severity is Severity.HIGH
    change = call.schema["properties"]["changes"]["items"]["properties"]
    assert change["source"]["enum"] == ["s1", "s2"]
    assert "MaterialChange.NEW_PATCH" not in str(change["type"]["enum"])
    assert "NEW_PATCH" in change["type"]["enum"] and "NO_MATERIAL_CHANGE" not in change["type"]["enum"]
    user = call.messages[1]["content"]
    assert "Acme fixes SecureGate flaw" in user and "acme.example" not in user


def test_a_final_summary_asks_for_the_closing_summary_only():
    call = fw.followup_call(task("final_summary"), event(), SOURCES, CONTEXT, today=NOW.date())
    assert call.schema["required"] == ["summary"]


# ─── The answer ───────────────────────────────────────────────────────────────────────────────────


def check_call():
    return fw.followup_call(task(), event(), SOURCES, CONTEXT, today=NOW.date())


def test_a_change_gets_the_url_of_the_report_it_names():
    body = check_call().parse({"outcome": "changed", "changes": [
        {"type": "NEW_PATCH", "summary": SUMMARY, "source": "s1", "date": "2026-10-31"}]})
    assert body == {"outcome": "changed", "changes": [
        {"type": "NEW_PATCH", "summary": SUMMARY,
         "url": "https://acme.example/advisories/sg-2026-1", "date": "2026-10-31"}]}


def test_no_change_carries_nothing_else():
    body = check_call().parse({"outcome": "no_change", "changes": [
        {"type": "NEW_PATCH", "summary": SUMMARY, "source": "s1", "date": None}]})
    assert body == {"outcome": "no_change"}


def test_a_report_it_was_not_shown_is_refused():
    with pytest.raises(Rejected, match="not one it was shown"):
        check_call().parse({"outcome": "changed", "changes": [
            {"type": "NEW_PATCH", "summary": SUMMARY, "source": "s9", "date": None}]})


def test_a_change_the_report_checks_refuse_is_an_unusable_answer():
    # check_report refuses a summary under 20 characters; with nothing kept the answer is unusable.
    with pytest.raises(Rejected, match="no change passed"):
        check_call().parse({"outcome": "changed", "changes": [
            {"type": "NEW_PATCH", "summary": "Patched.", "source": "s1", "date": None}]})


def test_a_final_summary_is_sent_as_one():
    call = fw.followup_call(task("final_summary"), event(), SOURCES, CONTEXT, today=NOW.date())
    text = ("Attackers used a SecureGate VPN flaw against hospitals. Acme released a fix and "
            "the attacks stopped.")
    assert call.parse({"summary": text}) == {"outcome": "summary", "summary": text}


# ─── A pass ───────────────────────────────────────────────────────────────────────────────────────


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
def run(monkeypatch):
    state = {
        "quiet": ["evt-2026-000009"], "accepted": 0, "work": [(task(), event(), SOURCES, CONTEXT)],
        "answers": [{"outcome": "changed", "changes": [
            {"type": "NEW_PATCH", "summary": SUMMARY, "source": "s1", "date": None}]}],
        "submitted": [], "refused": [], "calls": 0,
    }

    class Client:
        label = "MixRoute"

        async def complete(self, tier, **kwargs):
            state["calls"] += 1
            data = state["answers"].pop(0) if state["answers"] else {"outcome": "no_change",
                                                                      "changes": []}
            return Completion(data=data, model="vendor-b/cheap", upstream=None, tokens_in=1500,
                              tokens_out=200, cost_usd=Decimal("0.0003"), generation_id=None)

    def submit(engine, task_id, body, now, by):
        state["submitted"].append((task_id, body, by))
        return Submitted("recorded", "", task_id, "evt-2026-000001", Report(body["outcome"]))

    monkeypatch.setattr(fw, "_close_quiet", lambda engine, now: state["quiet"])
    monkeypatch.setattr(fw, "_accepted", lambda engine, now: state["accepted"])
    monkeypatch.setattr(fw, "_load", lambda engine, now, limit: state["work"][:limit])
    monkeypatch.setattr(fw, "_submit", submit)
    monkeypatch.setattr(fw, "_refuse", lambda engine, task_id, reason, now:
                        state["refused"].append((task_id, reason)))
    monkeypatch.setattr(fw, "_client", lambda layer, http, engine: Client())
    return state


async def test_a_pass_closes_quiet_checks_and_reports_on_the_rest(run):
    done = await fw.write_followups(engine=None, layer=FakeLayer(governor()), now=NOW)
    assert (done.quiet, done.recorded, done.failed) == (1, 1, 0)
    [(task_id, body, by)] = run["submitted"]
    assert task_id == 7 and by == "worker-ai" and body["outcome"] == "changed"
    assert body["changes"][0]["url"] == "https://acme.example/advisories/sg-2026-1"


async def test_a_check_with_no_new_report_is_closed_without_a_call(run):
    run["work"] = [(task(), event(), [], CONTEXT)]
    done = await fw.write_followups(engine=None, layer=FakeLayer(governor()), now=NOW)
    assert run["calls"] == 0 and done.no_reports == 1
    [(_, body, by)] = run["submitted"]
    assert body == {"outcome": "no_change"} and by == "worker"


async def test_the_days_cap_holds_the_pass_back(run):
    run["accepted"] = 20
    done = await fw.write_followups(engine=None, layer=FakeLayer(governor()), now=NOW)
    assert run["calls"] == 0 and "cap" in done.skipped and done.quiet == 1


async def test_an_answer_that_fails_twice_counts_an_attempt_on_the_task(run):
    bad = {"outcome": "changed", "changes": [
        {"type": "NEW_PATCH", "summary": "Patched.", "source": "s1", "date": None}]}
    run["answers"] = [bad, bad]
    done = await fw.write_followups(engine=None, layer=FakeLayer(governor()), now=NOW)
    assert done.failed == 1 and run["submitted"] == []
    [(task_id, reason)] = run["refused"]
    assert task_id == 7 and "no change passed" in reason


async def test_with_no_money_for_it_the_pass_stops_and_nothing_is_counted(run):
    # MINIMAL sends editorial work to tier 0, where it waits; the task is not refused.
    done = await fw.write_followups(engine=None, layer=FakeLayer(governor("11")), now=NOW)
    assert done.waiting == 1 and run["refused"] == [] and run["submitted"] == []
