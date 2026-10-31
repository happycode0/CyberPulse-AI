"""The daily intelligence report (worker/ai/report.py): what the call is given, what it may answer,
and when the job writes one. No network and no database: the loaders, the store and the client
are fakes."""

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from worker.ai import report as report_mod
from worker.ai.budget import Governor, KeyStatus
from worker.ai.client import Completion
from worker.ai.enrich import Done
from worker.ai.ladder import Ladder, Tier, VerifiedLadder
from worker.ai.report import (
    MAX_ITEMS,
    REPORT_STAGE,
    parse_report,
    report_call,
    write_daily_report,
)
from worker.ai.tasks import Rejected
from worker.db.reports import ReportInput

NOW = datetime(2026, 10, 31, 20, 30, tzinfo=UTC)  # 07:30 on 1 November in Sydney
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


def ev(n, *, au=0.0, direct=False, severity="medium", cves=(), kev=False, title=None):
    return {
        "event_id": f"evt-2026-{n:06d}",
        "title": title or f"Story number {n} about a breach",
        "summary": f"Summary {n}: an attacker stole records from a retailer in story {n}.",
        "why_it_matters": None,
        "severity": severity,
        "status": "active",
        "au_relevance": au,
        "reported_in_australia": direct,
        "categories": ["data-breach"],
        "au_sectors": [],
        "cves": list(cves),
        "kev": kev,
        "first_seen": "2026-10-31",
    }


GAPS = [
    {"id": "category:ddos", "topic": "ddos", "kind": "category", "quiet_days": 30.0},
    {"id": "au_sector:health", "topic": "health", "kind": "au_sector", "quiet_days": 9.2},
]
GIVEN = ReportInput(
    events=[ev(1), ev(2, au=0.8), ev(3, severity="critical", cves=("CVE-2026-1234",), kev=True)],
    gaps=GAPS,
    window_hours=24,
)


def answer(**over):
    data = {
        "summary": "A quiet day for Australia apart from one retail breach; a KEV-listed flaw "
        "needs patching.",
        "items": [
            {"event_id": "evt-2026-000003", "why": "CVE-2026-1234 is exploited, so patch now."},
            {"event_id": "evt-2026-000002", "why": "Australian customers' data was taken."},
        ],
        "gaps": ["au_sector:health"],
    }
    return {**data, **over}


# ─── What the call is given ───────────────────────────────────────────────────────────────────────


def test_the_call_offers_only_the_given_events_and_quiet_topics():
    call = report_call(GIVEN)
    assert call.stage == REPORT_STAGE == "report.daily"
    assert call.work.tier is Tier.STRONG
    item = call.schema["properties"]["items"]["items"]
    assert item["properties"]["event_id"]["enum"] == [e["event_id"] for e in GIVEN.events]
    assert call.schema["properties"]["gaps"]["items"]["enum"] == ["category:ddos", "au_sector:health"]
    # The record is data in the user message, never the system prompt.
    assert "evt-2026-000001" in call.messages[1]["content"]
    assert "evt-2026-000001" not in call.messages[0]["content"]


def test_a_critical_or_kev_event_keeps_the_report_on_the_strong_tier_in_conserve():
    assert report_call(GIVEN).work.kev is True
    quiet = ReportInput(events=[ev(1)], gaps=[], window_hours=24)
    assert report_call(quiet).work.kev is False


def test_with_no_quiet_topic_the_gaps_are_free_text_that_is_then_dropped():
    given = ReportInput(events=[ev(1)], gaps=[], window_hours=24)
    gaps = report_call(given).schema["properties"]["gaps"]
    assert gaps["items"] == {"type": "string"}
    out = parse_report(answer(items=[], gaps=["category:ddos"]), given)
    assert out.gaps == ()


# ─── What it may answer ───────────────────────────────────────────────────────────────────────────


def test_a_good_answer_puts_australia_first_and_carries_the_quiet_days():
    out = parse_report(answer(), GIVEN)
    assert [i["event_id"] for i in out.items] == ["evt-2026-000002", "evt-2026-000003"]
    assert out.gaps == ({"id": "au_sector:health", "topic": "health", "kind": "au_sector",
                         "quiet_days": 9.2},)
    assert out.summary.startswith("A quiet day")


def test_an_event_it_was_not_given_is_refused():
    bad = answer(items=[{"event_id": "evt-2026-999999", "why": "Made up."}])
    with pytest.raises(Rejected, match="not one it was given"):
        parse_report(bad, GIVEN)


@pytest.mark.parametrize(
    "summary",
    [
        "Patch now: see https://evil.example/fix for details on this.",
        "CVE-2026-9999 is the one to worry about today, more than anything.",
    ],
)
def test_a_url_or_an_unknown_cve_in_the_summary_is_refused(summary):
    with pytest.raises(Rejected):
        parse_report(answer(summary=summary), GIVEN)


def test_an_item_whose_why_fails_its_checks_is_dropped_not_the_whole_report():
    """The first live report (2026-10-31 09:40) was refused whole for one item. An item is the
    model's word on one event: one that fails a check that trimming cannot mend (a URL, an
    unknown CVE) goes, as an invented entity name does, and the others stand."""
    items = [
        {"event_id": "evt-2026-000003", "why": "CVE-2026-1234 is exploited, so patch now."},
        {"event_id": "evt-2026-000002", "why": "Read https://evil.example/fix for the patch."},
        {"event_id": "evt-2026-000001", "why": "CVE-2026-9999 is the one to patch first."},
    ]
    out = parse_report(answer(items=items), GIVEN)
    assert [i["event_id"] for i in out.items] == ["evt-2026-000003"]
    assert out.dropped == 2


def test_a_report_whose_every_item_fails_its_checks_is_refused():
    """Then the answer as a whole is unusable, and the pass's retry asks again."""
    items = [{"event_id": "evt-2026-000002", "why": "See https://evil.example/fix today."}]
    with pytest.raises(Rejected, match="every item"):
        parse_report(answer(items=items), GIVEN)


def test_the_prompt_states_the_character_limits():
    """MixRoute's models do not keep to a schema's lengths, and the prompt only gave sentence
    counts: the first live reports ran every "why" past 200 characters and the summary past 600."""
    prompt = report_call(GIVEN).messages[0]["content"]
    assert "600 characters" in prompt and "200 characters" in prompt


def test_an_overlong_why_or_summary_is_cut_back_to_its_last_whole_sentence():
    first = "Attackers exploited the flaw within hours of its disclosure, so patching is urgent."
    second = (" Australian councils, hospitals and universities run the affected gateway in large "
              "numbers, which widens the exposure well beyond the usual targets.")
    assert len(first) <= 200 < len(first + second)
    sentence = "A busy day for breaches across the region, with several new victims named. "
    summary = (sentence * 9).strip()
    assert len(summary) > 600
    items = [{"event_id": "evt-2026-000002", "why": first + second}]
    out = parse_report(answer(summary=summary, items=items), GIVEN)
    assert out.items[0]["why"] == first
    assert len(out.summary) <= 600 and out.summary.endswith(".")
    assert out.summary.count(". ") + 1 <= 4  # and to its sentence limit
    assert out.dropped == 0


def test_one_sentence_longer_than_the_limit_is_cut_at_a_word():
    why = "Exposure " + "widens " * 40 + "today."
    out = parse_report(answer(items=[{"event_id": "evt-2026-000002", "why": why}]), GIVEN)
    cut = out.items[0]["why"]
    assert len(cut) <= 200 and cut.endswith("…") and not cut.endswith(" …")


def test_at_most_five_items_and_three_gaps_each_once():
    many = ReportInput(events=[ev(n) for n in range(1, 9)], gaps=GAPS, window_hours=24)
    items = [{"event_id": f"evt-2026-{n:06d}", "why": f"Reason {n} for the reader."}
             for n in (1, 1, 2, 3, 4, 5, 6, 7)]
    out = parse_report(answer(items=items, gaps=["category:ddos", "category:ddos"]), many)
    assert len(out.items) == MAX_ITEMS
    assert len({i["event_id"] for i in out.items}) == MAX_ITEMS
    assert [g["id"] for g in out.gaps] == ["category:ddos"]


# ─── The job ──────────────────────────────────────────────────────────────────────────────────────


class FakeLayer:
    def __init__(self, governor):
        self._governor = governor
        self.settings = None

    async def ready(self, http):
        return self._governor


def governor() -> Governor:
    g = Governor(VerifiedLadder(ladder=LADDER, checked_at=NOW), Decimal("12"), now=lambda: NOW)
    g.observe(KeyStatus(limit=None, limit_remaining=None, limit_reset=None,
                        usage_daily=Decimal(0), usage_monthly=Decimal(0),
                        free_requests_remaining=None))
    return g


@pytest.fixture
def job(monkeypatch):
    state = {"exists": False, "stored": [], "given": GIVEN, "answer": answer()}

    class Client:
        label = "MixRoute"

        async def complete(self, tier, **kwargs):
            state["call"] = (tier, kwargs["attribution"])
            return Completion(data=state["answer"], model="vendor-b/strong", upstream=None,
                              tokens_in=3000, tokens_out=400, cost_usd=Decimal("0.0006"),
                              generation_id=None)

    monkeypatch.setattr(report_mod, "_exists", lambda engine, day: state["exists"])
    monkeypatch.setattr(report_mod, "_load", lambda engine, now: state["given"])
    monkeypatch.setattr(report_mod, "_store", lambda engine, **kw: state["stored"].append(kw) or True)
    monkeypatch.setattr(report_mod, "_client", lambda layer, http, engine: Client())
    return state


async def test_the_job_writes_one_report_for_the_sydney_date(job):
    done = await write_daily_report(engine=None, layer=FakeLayer(governor()), now=NOW)
    assert done.written and done.changed_anything
    [stored] = job["stored"]
    assert stored["report_date"] == date(2026, 11, 1)
    assert stored["model"] == "vendor-b/strong" and stored["window_hours"] == 24
    tier, attribution = job["call"]
    assert tier is Tier.STRONG and attribution.stage == "report.daily"


async def test_a_date_that_has_its_report_is_not_written_again(job):
    job["exists"] = True
    done = await write_daily_report(engine=None, layer=FakeLayer(governor()), now=NOW)
    assert not done.written and done.skipped == "the report for 2026-11-01 is written"
    assert job["stored"] == []


async def test_a_window_with_no_events_writes_nothing(job):
    job["given"] = ReportInput(events=[], gaps=GAPS, window_hours=24)
    done = await write_daily_report(engine=None, layer=FakeLayer(governor()), now=NOW)
    assert not done.written and "no events" in done.skipped


async def test_with_the_ai_layer_off_nothing_is_called(job):
    done = await write_daily_report(engine=None, layer=FakeLayer(None), now=NOW)
    assert not done.written and done.skipped == "the AI layer is off"


async def test_an_answer_that_fails_its_checks_twice_is_not_stored(job):
    job["answer"] = answer(summary="See https://evil.example now, it is all explained there.")
    done = await write_daily_report(engine=None, layer=FakeLayer(governor()), now=NOW)
    assert not done.written and "URL" in done.failed
    assert job["stored"] == []
