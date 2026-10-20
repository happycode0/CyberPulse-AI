"""worker/notify/messages.py and worker/notify/jobs.py, with no database and no bot."""

import json
import re
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import get_args

import httpx
import pytest
from pydantic import SecretStr

from tests.unit.test_gauntlet import measured
from worker.ai.gauntlet import decide, proposal_body, proposal_title
from worker.ai.ladder import Tier
from worker.db.notifications import AlertEvent, Claim, Kind, UpdateEntry
from worker.db.scout import LadderChange, Proposal, ScanNotice
from worker.notify import jobs
from worker.notify.messages import (
    critical_au_alert,
    daily_digest,
    developing_update,
    event_url,
    ladder_changed,
    model_proposal,
    monthly_cost,
    source_activated,
)
from worker.notify.telegram import SendResult, Telegram
from worker.publish.validate import scan_text_for_secrets
from worker.settings import Settings

SITE = "https://example.org/site/"
# 07:10 in Sydney on Saturday 3 October 2026 (AEST, UTC+10): when the digest goes.
NOW = datetime(2026, 10, 2, 21, 10, tzinfo=UTC)


def event(event_id="evt-2026-000001", **overrides):
    row = {
        "event_id": event_id, "title": "A  title\nwith breaks", "severity": "high",
        "status": "new", "prominence": 0.8, "au_relevance": 0.91, "au_directly_reported": True,
        "first_seen": NOW - timedelta(hours=2), "last_material_update": NOW,
    }
    return {**row, **overrides}


def digest(**overrides):
    base = {
        "window": {"hours": 24, "from": NOW - timedelta(hours=24), "to": NOW},
        "events": {"new": 42, "new_archived_on_arrival": 30, "updated": 7, "merged": 3,
                   "standing": 812},
        "top": [event(), event("evt-2026-000002", severity="critical", au_relevance=None,
                              au_directly_reported=False)],
        "escalation_candidates": [{**event(), "lanes": ["fast"], "fast_lane": True}],
        "source_health_changes": [
            {"source_id": "acsc-alerts", "was": "ok", "now": "error", "checked_at": NOW,
             "error": "HTTP 503"},
            {"source_id": "new-feed", "was": None, "now": "ok", "checked_at": NOW, "error": None},
        ],
        "runs": {"fast": {"runs": 24, "unfinished": 1, "new_events": 12, "updated_events": 3,
                          "archived_events": 0, "errors": 2}},
        "cost_this_month": {"month": "2026-10", "calls": 437, "cost_usd": 0.0461,
                            "unknown_cost_calls": 0},
    }
    return {**base, **overrides}


def alert(event_id="evt-2026-000009", **overrides) -> AlertEvent:
    fields = {
        "event_id": event_id, "title": "Exploited flaw in a gateway", "severity": "critical",
        "status": "new", "au_relevance": 0.8, "au_directly_reported": False,
        "first_seen": NOW - timedelta(minutes=5), "kev_cves": (),
    }
    return AlertEvent(**{**fields, **overrides})


# --- Messages -----------------------------------------------------------------------------------


def test_the_digest_reads_as_a_morning_briefing():
    text = daily_digest(digest(), site_url=SITE)
    lines = text.split("\n")
    assert lines[0] == "CyberPulse-AI · daily digest · Sat 3 Oct 2026"
    assert lines[1] == "The 24 hours to 07:10 Sydney time."
    assert "42 new, 7 updated, 3 merged; 812 standing." in text
    assert "30 of the new were a feed's back catalogue, archived on arrival." in text
    assert "1. HIGH · A title with breaks (AU 0.91, reported in Australia)" in text
    assert "   https://example.org/site/event.html?id=evt-2026-000001" in text
    assert "2. CRITICAL · A title with breaks\n" in text  # no AU reading, no brackets
    assert "- acsc-alerts: ok → error" in text and "- new-feed: new → ok" in text
    assert "- fast: 24 runs, 12 new events, 2 errors, 1 unfinished" in text
    assert "437 calls, $0.05." in text
    assert lines[-1] == SITE


def test_an_empty_day_says_so_rather_than_leaving_gaps():
    text = daily_digest(
        digest(top=[], escalation_candidates=[], source_health_changes=[], runs={},
               events={"new": 0, "new_archived_on_arrival": 0, "updated": 0, "merged": 0,
                       "standing": 5}),
        site_url=SITE,
    )
    assert "Nothing is prominent enough for the live page." in text
    assert "Nothing new or updated." in text
    assert "No source changed status." in text
    assert "⚠ No collection ran in the window." in text
    assert "back catalogue" not in text


def test_long_lists_are_cut_with_a_count():
    many = [{**event(f"evt-2026-{i:06d}"), "lanes": [], "fast_lane": False} for i in range(11)]
    text = daily_digest(digest(escalation_candidates=many), site_url=SITE)
    assert "… and 3 more on the site." in text


def test_an_alert_names_why_and_links_the_event():
    text = critical_au_alert(alert(), site_url=SITE)
    assert text.split("\n") == [
        "CyberPulse-AI · critical · Australia",
        "",
        "CRITICAL · Exploited flaw in a gateway",
        "AU 0.80",
        "First seen 07:05 Sydney time, Sat 3 Oct 2026 · new",
        "",
        "https://example.org/site/event.html?id=evt-2026-000009",
    ]


def test_a_kev_alert_lists_the_cves():
    text = critical_au_alert(
        alert(severity="high", au_relevance=None, au_directly_reported=True,
              kev_cves=("CVE-2026-0001", "CVE-2026-0002")),
        site_url=SITE,
    )
    assert text.startswith("CyberPulse-AI · known exploited · Australia")
    assert "reported in Australia" in text
    assert "Known exploited (CISA KEV): CVE-2026-0001, CVE-2026-0002" in text


def test_event_urls_do_not_double_the_slash():
    assert event_url("https://x.org/s", "evt-1") == event_url("https://x.org/s/", "evt-1")


def test_the_morning_digest_reads_the_standing_top_not_the_agents_window(monkeypatch):
    """Its "most prominent now" means now: only the agents' copy is limited to the window."""
    asked = {}

    class Engine:
        @contextmanager
        def connect(self):
            yield None

    def load_digest(conn, **kwargs):
        asked.update(kwargs)
        return {}

    monkeypatch.setattr(jobs, "load_digest", load_digest)
    jobs._read_digest(Engine(), NOW)
    assert asked == {"now": NOW, "hours": jobs.DIGEST_HOURS, "top_in_window": False}


# --- Passes -------------------------------------------------------------------------------------


class Channel:
    def __init__(self, *results):
        self.results = list(results)
        self.sent = []

    async def send(self, message):
        self.sent.append(message)
        result = self.results.pop(0) if self.results else SendResult(True)
        if isinstance(result, Exception):
            raise result
        return result


@pytest.fixture
def db(monkeypatch):
    """claim and settle against a dict: a key once claimed is not claimed again unless failed."""
    state = {"rows": {}, "settled": [], "alerts": [], "digest_reads": 0}

    def claim(engine, *, kind, key, now, event_id=None):
        row = state["rows"].get(key)
        if row is not None and row != "failed":
            return None
        state["rows"][key] = "sending"
        return Claim(len(state["rows"]), 1)

    def settle(engine, claimed, outcome, *, now, error=None):
        key = next(k for k, v in state["rows"].items() if v == "sending")
        state["rows"][key] = outcome
        state["settled"].append((key, outcome, error))

    def read_digest(engine, now):
        state["digest_reads"] += 1
        return digest()

    monkeypatch.setattr(jobs, "claim", claim)
    monkeypatch.setattr(jobs, "settle", settle)
    monkeypatch.setattr(jobs, "_read_digest", read_digest)
    monkeypatch.setattr(jobs, "_read_alerts", lambda engine, since: state["alerts"])
    monkeypatch.setattr(jobs, "_read_updates", lambda engine, since: state.setdefault("updates", []))
    return state


SETTINGS = Settings(_env_file=None, database_url="postgresql://x", site_url=SITE)


async def test_with_no_bot_nothing_is_read_or_claimed(db):
    summary = await jobs.send_daily_digest(None, SETTINGS, now=NOW)
    assert summary.skipped and db["rows"] == {} and db["digest_reads"] == 0
    summary = await jobs.send_critical_alerts(None, SETTINGS, now=NOW)
    assert summary.skipped and db["rows"] == {}


async def test_the_digest_goes_once_per_sydney_date(db):
    channel = Channel()
    first = await jobs.send_daily_digest(None, SETTINGS, now=NOW, channel=channel)
    again = await jobs.send_daily_digest(None, SETTINGS, now=NOW + timedelta(hours=1),
                                         channel=channel)
    assert (first.sent, again.sent, len(channel.sent)) == (1, 0, 1)
    # 07:10 Sydney is still 2 October in UTC; the key is the Sydney date.
    assert list(db["rows"]) == ["daily_digest:2026-10-03"]


async def test_a_failed_digest_is_tried_again_by_the_next_pass(db):
    channel = Channel(SendResult(False, error="HTTP 502"))
    first = await jobs.send_daily_digest(None, SETTINGS, now=NOW, channel=channel)
    second = await jobs.send_daily_digest(None, SETTINGS, now=NOW + timedelta(hours=1),
                                          channel=channel)
    assert (first.failed, second.sent) == (1, 1)
    assert db["settled"][0] == ("daily_digest:2026-10-03", "failed", "HTTP 502")


async def test_a_send_that_raises_is_still_settled(db):
    channel = Channel(RuntimeError("boom"))
    summary = await jobs.send_daily_digest(None, SETTINGS, now=NOW, channel=channel)
    assert summary.failed == 1 and db["settled"] == [
        ("daily_digest:2026-10-03", "failed", "RuntimeError")
    ]


async def test_a_withheld_message_is_recorded_as_withheld(db):
    channel = Channel(SendResult(False, withheld=True, error="withheld by the secret scan"))
    summary = await jobs.send_daily_digest(None, SETTINGS, now=NOW, channel=channel)
    assert summary.withheld == 1 and db["rows"]["daily_digest:2026-10-03"] == "withheld"


async def test_each_event_alerts_once_and_a_backlog_waits_its_turn(db):
    db["alerts"] = [alert(f"evt-2026-{i:06d}") for i in range(7)]
    channel = Channel()
    first = await jobs.send_critical_alerts(None, SETTINGS, now=NOW, channel=channel)
    second = await jobs.send_critical_alerts(None, SETTINGS, now=NOW, channel=channel)
    third = await jobs.send_critical_alerts(None, SETTINGS, now=NOW, channel=channel)
    assert (first.sent, second.sent, third.sent) == (jobs.MAX_ALERTS_PER_PASS, 2, 0)
    assert len(channel.sent) == 7 and len(set(channel.sent)) == 7


async def test_the_alert_window_is_measured_back_from_now(db, monkeypatch):
    seen = []
    monkeypatch.setattr(jobs, "_read_alerts", lambda engine, since: seen.append(since) or [])
    await jobs.send_critical_alerts(None, SETTINGS, now=NOW, channel=Channel())
    assert seen == [NOW - jobs.ALERT_WINDOW]


# --- Developing updates ---------------------------------------------------------------------------


def update(entry_id=1, **overrides) -> UpdateEntry:
    values = {
        "entry_id": entry_id, "event_id": "evt-2026-000042", "title": "Hospital  records\nbreach",
        "severity": "critical", "status": "developing", "au_relevance": 0.9,
        "au_directly_reported": True, "type": "NEW_PATCH",
        "summary": "The vendor released\n a fix for the flaw.", "created_at": NOW, "link": None,
    }
    return UpdateEntry(**{**values, **overrides})


def test_an_update_says_what_changed_and_links_the_event():
    text = developing_update(update(), site_url=SITE)
    assert text.splitlines()[0] == "CyberPulse-AI · update · Australia"
    assert "CRITICAL · Hospital records breach" in text
    assert "Patch out: The vendor released a fix for the flaw." in text
    assert "Status developing · 07:10 Sydney time, Sat 3 Oct 2026" in text
    assert text.endswith(event_url(SITE, "evt-2026-000042"))
    assert "Source:" not in text


def test_an_update_from_deckard_names_its_source():
    text = developing_update(update(type="NEW_FACT", link="https://example.org/a"), site_url=SITE)
    assert "New: " in text and "Source: https://example.org/a" in text


async def test_each_change_is_sent_once(db):
    db["updates"] = [update(i) for i in range(7)]
    channel = Channel()
    first = await jobs.send_developing_updates(None, SETTINGS, now=NOW, channel=channel)
    second = await jobs.send_developing_updates(None, SETTINGS, now=NOW, channel=channel)
    third = await jobs.send_developing_updates(None, SETTINGS, now=NOW, channel=channel)
    assert (first.sent, second.sent, third.sent) == (jobs.MAX_ALERTS_PER_PASS, 2, 0)
    assert sorted(db["rows"]) == [f"developing_update:{i}" for i in range(7)]


async def test_with_no_bot_no_update_is_read(db):
    summary = await jobs.send_developing_updates(None, SETTINGS, now=NOW)
    assert summary.skipped and db["rows"] == {} and "updates" not in db


# --- New sources ---------------------------------------------------------------------------------


def activation(**overrides):
    from worker.db.discovery import Activation

    values = {
        "source_id": "found_itnews_com_au", "name": "iTnews  Security\n", "host": "itnews.com.au",
        "region": "au", "found_by": "tachikoma", "passes": 6,
        "last_result": {"recent": 12, "on_beat": 7}, "activated_at": NOW,
    }
    return Activation(**{**values, **overrides})


def test_a_new_source_says_who_found_it_and_what_it_counts_for():
    text = source_activated(activation(), site_url=SITE)
    assert text.split("\n") == [
        "CyberPulse-AI · new source",
        "",
        "iTnews Security · itnews.com.au · Australia",
        "Found by TACHIKOMA; passed SERAPH's gate with 6 healthy probes in a row.",
        "Last probe: 12 recent items, 7 on the beat.",
        ("Collected from the next normal run as community evidence, which never confirms an "
         "event on its own."),
        "",
        SITE,
    ]


def test_a_new_source_without_a_last_probe_leaves_the_line_out():
    text = source_activated(activation(found_by="search", region="global", last_result=None),
                            site_url=SITE)
    assert "Found by the nightly search" in text and "Australia" not in text
    assert "Last probe" not in text


async def test_each_new_source_is_announced_once(db, monkeypatch):
    monkeypatch.setattr(jobs, "_read_activations", lambda engine, since: [activation()])
    channel = Channel()
    first = await jobs.send_source_activations(None, SETTINGS, now=NOW, channel=channel)
    again = await jobs.send_source_activations(None, SETTINGS, now=NOW, channel=channel)
    assert (first.sent, again.sent, len(channel.sent)) == (1, 0, 1)
    assert list(db["rows"]) == ["source_activated:found_itnews_com_au"]


# --- Models and money (RIPPERDOC's notices, since its retirement on 2026-10-20) ----------------

# 03:20 in Sydney on Tuesday 20 October 2026 (AEDT, UTC+11): the nightly model scan.
SCANNED = datetime(2026, 10, 19, 16, 20, tzinfo=UTC)
OVER = "cheapest capable route is $1.28/M output, over the $1.00/M ceiling"
GONE = "not on OpenRouter (withdrawn, or the slug is wrong)"


def scan(*changes, usable=True, at=SCANNED) -> ScanNotice:
    return ScanNotice(at, usable, tuple(changes))


def dropped(tier="tier1_cheap", slug="deepseek/deepseek-v4-flash-0731", reason=OVER):
    return LadderChange(tier, slug, "dropped", reason)


def restored(tier="tier0_free", slug="qwen/qwen3.8-27b:free"):
    return LadderChange(tier, slug, "restored", None)


def proposal(proposal_id=7, **overrides) -> Proposal:
    d = decide(Tier.CHEAP, measured("v/now", incumbent=True, cost="0.001"),
               [measured("v/cheap", cost="0.0001")])
    values = {
        "id": proposal_id, "title": proposal_title(d),
        "body": proposal_body(d, golden_size=30, reviewed=0, au_labels=(6, 24)),
        "created_at": SCANNED, "payload": {"tier": "tier1_cheap", "slug": "v/cheap"},
    }
    return Proposal(**{**values, **overrides})


def cost_rows(*rows):
    return [{"key": key, "calls": calls, "cost_usd": cost, "unknown_cost_calls": 0,
             "tokens_in": 0, "tokens_out": 0} for key, calls, cost in rows]


def cost(**overrides):
    # worker.db.digest.cost_report's shape: what GET /ops/cost answers for the month.
    base = {
        "month": "2026-10",
        "ledger": {"calls": 1234, "cost_usd": 1.2345, "unknown_cost_calls": 3},
        "by_stage": cost_rows(("enrich.brief", 600, 0.8), ("gauntlet", 90, 0.2),
                              ("enrich.triage", 544, 0.0)),
        "by_model": cost_rows(("xiaomi/mimo-v2.6-flash", 700, 1.0),
                              ("nvidia/nemotron-3-super-120b-a12b:free", 534, 0.0)),
        "by_agent": cost_rows(("none", 1144, 1.0345), ("ripperdoc", 90, 0.2)),
        "by_outcome": cost_rows(("ok", 1222, 1.2), ("invalid_output", 12, 0.0345)),
    }
    return {**base, **overrides}


def test_a_ladder_notice_names_the_tier_the_model_and_the_guards_reason():
    text = ladder_changed(scan(dropped(), dropped("audit", "qwen/qwen3.8-flash", GONE)))
    lines = text.split("\n")
    assert lines[:2] == [
        "CyberPulse-AI · model ladder",
        "The model scan at 03:20 Sydney time, Tue 20 Oct 2026.",
    ]
    assert f"- tier1_cheap · deepseek/deepseek-v4-flash-0731: {OVER}" in lines
    assert f"- audit · qwen/qwen3.8-flash: {GONE}" in lines
    # The guard has acted already; what is left to the owner is a config edit, and a Paperclip
    # agent's model, which the ladder does not set.
    assert "needs no approval" in text and "config/models.yaml" in text
    assert "change it there" in text and "BACK" not in text


def test_a_ladder_left_unusable_says_enrichment_is_off():
    text = ladder_changed(scan(dropped(), usable=False))
    assert "not a usable ladder, so AI enrichment is off" in text
    assert "Collection and publishing carry on" in text and "needs no approval" not in text


def test_a_model_back_in_the_ladder_is_its_all_clear():
    text = ladder_changed(scan(restored()))
    assert "BACK: it passes the guard again\n- tier0_free · qwen/qwen3.8-27b:free" in text
    assert "DROPPED" not in text and "change it there" not in text
    assert text.endswith("Enrichment can use them again from its next pass.")


def test_a_proposal_goes_as_the_worker_wrote_it_for_the_owner_to_decide():
    p = proposal()
    text = model_proposal(p)
    assert text.split("\n")[:3] == [
        "CyberPulse-AI · model proposal", "", "[MODEL] Proposal: tier1_cheap -> v/cheap",
    ]
    assert text.endswith(p.body.strip())
    assert "the owner's decision" in text and "RIPPERDOC" not in text


def test_the_monthly_cost_is_the_ledgers_month_by_stage_and_model():
    text = monthly_cost(cost())
    lines = text.split("\n")
    assert lines[0] == "CyberPulse-AI · AI cost · October 2026"
    assert "1234 calls, $1.23." in lines
    assert "3 calls with no billed cost, so the total is a floor." in lines
    assert "12 calls ended invalid output." in lines
    assert "- enrich.brief: 600 calls, $0.8000" in lines
    assert "- enrich.triage: 544 calls, $0.0000" in lines
    assert "- nvidia/nemotron-3-super-120b-a12b:free: 534 calls, $0.0000" in lines
    # By stage and model, not by agent: the gauntlet's rows still carry RIPPERDOC's label.
    assert "ripperdoc" not in text and "BY AGENT" not in text
    assert "Paperclip agents' own runs are not in the ledger" in text


def test_a_month_with_no_calls_says_so_and_a_long_breakdown_is_cut():
    empty = monthly_cost(cost(ledger={"calls": 0, "cost_usd": 0.0, "unknown_cost_calls": 0},
                              by_stage=[], by_model=[], by_outcome=[]))
    assert "No model calls were recorded." in empty and "BY STAGE" not in empty
    many = cost_rows(*((f"m/{i}", 10, 0.01) for i in range(9)))
    text = monthly_cost(cost(by_model=many))
    assert "- m/5: 10 calls, $0.0100" in text and "m/6" not in text
    assert "… and 3 more." in text


async def test_with_no_bot_no_model_notice_is_read(db, monkeypatch):
    def unread(*args):
        raise AssertionError("read with no bot to send to")

    for reader in ("_read_ladder", "_read_proposals", "_read_cost"):
        monkeypatch.setattr(jobs, reader, unread)
    for send in (jobs.send_ladder_changes, jobs.send_model_proposals, jobs.send_monthly_cost):
        summary = await send(None, SETTINGS, now=NOW)
        assert summary.skipped and db["rows"] == {}


async def test_a_scan_that_changed_nothing_in_the_ladder_sends_nothing(db, monkeypatch):
    seen = []
    monkeypatch.setattr(jobs, "_read_ladder", lambda engine, since: seen.append(since) or [])
    channel = Channel()
    summary = await jobs.send_ladder_changes(None, SETTINGS, now=NOW, channel=channel)
    assert (summary.tried, channel.sent, db["rows"]) == (0, [], {})
    # Back to the night before's scan, so a notice whose send failed then is tried again.
    assert seen == [NOW - jobs.LADDER_WINDOW] and jobs.LADDER_WINDOW > timedelta(hours=24)


async def test_each_scans_ladder_notice_is_sent_once(db, monkeypatch):
    night_before = scan(dropped(), at=SCANNED - timedelta(days=1))
    notices = [night_before, scan(restored())]
    monkeypatch.setattr(jobs, "_read_ladder", lambda engine, since: notices)
    channel = Channel()
    first = await jobs.send_ladder_changes(None, SETTINGS, now=NOW, channel=channel)
    again = await jobs.send_ladder_changes(None, SETTINGS, now=NOW, channel=channel)
    assert (first.sent, again.sent, len(channel.sent)) == (2, 0, 2)
    assert list(db["rows"]) == [
        "ladder_change:2026-10-18T16:20:00+00:00", "ladder_change:2026-10-19T16:20:00+00:00",
    ]
    assert OVER in channel.sent[0] and "BACK" in channel.sent[1]


async def test_each_proposal_is_sent_once_oldest_first(db, monkeypatch):
    newest_first = [proposal(9, title="[MODEL] Proposal: tier0_free -> v/free:free"), proposal(7)]
    monkeypatch.setattr(jobs, "_read_proposals", lambda engine: newest_first)
    channel = Channel(SendResult(False, error="HTTP 502"))
    first = await jobs.send_model_proposals(None, SETTINGS, now=NOW, channel=channel)
    second = await jobs.send_model_proposals(None, SETTINGS, now=NOW, channel=channel)
    third = await jobs.send_model_proposals(None, SETTINGS, now=NOW, channel=channel)
    # The first send failed, so the next pass tries it again; the other went the first time.
    assert [(s.sent, s.failed) for s in (first, second, third)] == [(1, 1), (1, 0), (0, 0)]
    assert db["rows"] == {"model_proposal:7": "sent", "model_proposal:9": "sent"}
    assert "tier1_cheap -> v/cheap" in channel.sent[0]


@pytest.mark.parametrize(
    "now, month, start, end",
    [
        # 11:10 Sydney on 1 November (AEDT) is 00:10 UTC: October has just closed in the ledger.
        (datetime(2026, 11, 1, 0, 10, tzinfo=UTC), "2026-10",
         datetime(2026, 10, 1, tzinfo=UTC), datetime(2026, 11, 1, tzinfo=UTC)),
        (datetime(2027, 1, 1, 0, 10, tzinfo=UTC), "2026-12",
         datetime(2026, 12, 1, tzinfo=UTC), datetime(2027, 1, 1, tzinfo=UTC)),
    ],
)
async def test_the_monthly_cost_is_last_months_ledger_sent_once(
    db, monkeypatch, now, month, start, end
):
    asked = []

    def read_cost(engine, start, end):
        asked.append((start, end))
        return cost(month=f"{start:%Y-%m}")

    monkeypatch.setattr(jobs, "_read_cost", read_cost)
    channel = Channel()
    first = await jobs.send_monthly_cost(None, SETTINGS, now=now, channel=channel)
    retry = await jobs.send_monthly_cost(None, SETTINGS, now=now + timedelta(hours=1),
                                         channel=channel)
    assert (first.sent, retry.sent, len(channel.sent)) == (1, 0, 1)
    assert list(db["rows"]) == [f"monthly_cost:{month}"] and asked[0] == (start, end)


async def test_the_model_notices_reach_telegram_whole_and_carry_no_secret(monkeypatch):
    # Through the real channel, as the scheduler sends them: each passes the secret scan, and
    # the bot token is only ever in the URL. Built at run time, as test_telegram.py's is.
    token = "1000001" + ":" + "fake-bot-token-TESTONLY-0123456789"
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", token)
    seen = []

    def handler(request):
        seen.append(json.loads(request.content)["text"])
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

    channel = Telegram(SecretStr(token), "-1001234", user_agent="CyberPulse-AI/1.0",
                       transport=httpx.MockTransport(handler))
    messages = [ladder_changed(scan(dropped(), restored())), model_proposal(proposal()),
                monthly_cost(cost())]
    for message in messages:
        assert scan_text_for_secrets(message) == []
        assert (await channel.send(message)).sent
    assert seen == messages and not any(token in text for text in seen)


async def test_a_proposal_carrying_a_secret_is_withheld_not_sent(db, monkeypatch):
    # The body is the worker's own words and figures; if a key ever got into one, the secret
    # scan stops it and the claim is settled as withheld, so no later pass sends it.
    key = "sk-or-v1-" + "0123456789abcdef" * 2
    monkeypatch.setattr(jobs, "_read_proposals",
                        lambda engine: [proposal(body=f"Use the key {key} for this.")])
    seen = []
    channel = Telegram(SecretStr("1000001:x"), "-1001234", user_agent="CyberPulse-AI/1.0",
                       transport=httpx.MockTransport(lambda r: seen.append(r)))
    summary = await jobs.send_model_proposals(None, SETTINGS, now=NOW, channel=channel)
    assert summary.withheld == 1 and seen == [] and db["rows"] == {"model_proposal:7": "withheld"}


def test_every_kind_of_notice_is_one_the_table_accepts():
    # A kind the table's check does not allow fails every claim at run time, after the message
    # was written, and a skipped DB test would not say so. The newest migration to set the check
    # is the one that holds (017, for the three RIPPERDOC used to pass on).
    migrations = Path(__file__).resolve().parents[2] / "worker/db/migrations"
    sql = [p.read_text(encoding="utf-8") for p in sorted(migrations.glob("*.sql"))]
    newest = [m for m in sql if "add constraint notifications_kind_check" in m][-1]
    check = re.search(r"add constraint notifications_kind_check\s+check \(kind in \((.*?)\)\)",
                      newest, flags=re.S)
    assert check and set(re.findall(r"'([a-z_]+)'", check.group(1))) == set(get_args(Kind))
