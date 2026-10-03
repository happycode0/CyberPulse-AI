"""worker/notify/messages.py and worker/notify/jobs.py, with no database and no bot."""

from datetime import UTC, datetime, timedelta

import pytest

from worker.db.notifications import AlertEvent, Claim, UpdateEntry
from worker.notify import jobs
from worker.notify.messages import (
    critical_au_alert,
    daily_digest,
    developing_update,
    event_url,
    source_activated,
)
from worker.notify.telegram import SendResult
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
