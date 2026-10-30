"""The daily report on Telegram (worker/notify/messages.py `daily_report`,
worker/notify/jobs.py `send_daily_report`). No database: the read and the claims are fakes."""

from datetime import UTC, date, datetime

import pytest

from worker.db.notifications import Claim
from worker.notify import jobs as jobs_mod
from worker.notify.jobs import send_daily_report
from worker.notify.messages import daily_report
from worker.notify.telegram import SendResult
from worker.settings import Settings

NOW = datetime(2026, 10, 31, 20, 31, tzinfo=UTC)  # 07:31 on Sunday 1 November in Sydney
REPORT = {
    "report_date": date(2026, 11, 1),
    "generated_at": NOW,
    "window_hours": 24,
    "summary": "A quiet day for Australia apart from one retail breach.",
    "items": [{"event_id": "evt-2026-000002", "why": "Australian customers' data was taken."}],
    "gaps": [{"id": "au_sector:health", "topic": "health", "kind": "au_sector",
              "quiet_days": 9.2},
             {"id": "category:ddos", "topic": "ddos", "kind": "category", "quiet_days": 30.0}],
    "model": "deepseek/deepseek-v4-flash",
}
TITLES = {"evt-2026-000002": ("high", "Retailer breach exposes Australian customers")}


def test_the_report_reads_as_the_site_shows_it():
    text = daily_report(REPORT, TITLES, site_url="https://example.org/site")
    assert text.startswith("CyberPulse-AI · daily intelligence report · Sun 1 Nov 2026\n")
    assert "A quiet day for Australia apart from one retail breach." in text
    assert "1. HIGH · Retailer breach exposes Australian customers" in text
    assert "   Australian customers' data was taken." in text
    assert "https://example.org/site/event.html?id=evt-2026-000002" in text
    assert "- Australian health sector: nothing new for 9 days" in text
    assert "- ddos: nothing new for 30 days" in text
    assert "Written by AI (deepseek/deepseek-v4-flash)" in text


def test_an_item_whose_event_is_gone_is_left_out():
    text = daily_report(REPORT, {}, site_url="https://example.org/site")
    assert "evt-2026-000002" not in text and "TODAY" not in text


class FakeTelegram:
    def __init__(self):
        self.sent = []

    async def send(self, message):
        self.sent.append(message)
        return SendResult(True)


@pytest.fixture
def notify(monkeypatch):
    state = {"report": REPORT, "claimed": set()}

    def claim(engine, *, kind, key, now, event_id=None):
        if key in state["claimed"]:
            return None
        state["claimed"].add(key)
        return Claim(len(state["claimed"]), 1)

    monkeypatch.setattr(jobs_mod, "_read_report", lambda engine: (state["report"], TITLES))
    monkeypatch.setattr(jobs_mod, "claim", claim)
    monkeypatch.setattr(jobs_mod, "settle", lambda *a, **k: None)
    return state


def settings() -> Settings:
    return Settings(
        _env_file=None, database_url="postgresql://x", site_url="https://example.org/site"
    )


async def test_todays_report_is_sent_once(notify):
    telegram = FakeTelegram()
    first = await send_daily_report(None, settings(), now=NOW, channel=telegram)
    again = await send_daily_report(None, settings(), now=NOW, channel=telegram)
    assert (first.sent, again.sent) == (1, 0) and len(telegram.sent) == 1
    assert notify["claimed"] == {"daily_report:2026-11-01"}


async def test_yesterdays_report_is_not_sent_today(notify):
    notify["report"] = {**REPORT, "report_date": date(2026, 10, 31)}
    done = await send_daily_report(None, settings(), now=NOW, channel=FakeTelegram())
    assert done.tried == 0 and done.skipped == "no report for 2026-11-01"
