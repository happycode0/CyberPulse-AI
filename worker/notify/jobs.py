"""The notification passes the scheduler runs (worker/scheduler.py).

- **The daily digest**, at 07:00 Sydney time, tried again at 08:00 and 09:00 if it failed.
- **Critical alerts for Australia**, every 15 minutes: a new event that is critical or carries a
  KEV-listed CVE, and that matters to Australia.
- **Developing updates**, in the same pass: a material change to such an event after its first
  hour, or one that newly exposes a critical or high event in Australia.

Each message is claimed in `notifications` before it is sent, so none is sent twice. With no
Telegram bot configured, a pass claims nothing and returns, so the first pass after the token is
added sends normally.
"""

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

from sqlalchemy import Engine

from worker.db.digest import load_digest
from worker.db.notifications import (
    AlertEvent,
    Kind,
    Outcome,
    UpdateEntry,
    claim,
    load_alert_events,
    load_update_entries,
    settle,
)
from worker.notify.messages import SYDNEY, critical_au_alert, daily_digest, developing_update
from worker.notify.telegram import SendResult, Telegram
from worker.settings import Settings

logger = logging.getLogger(__name__)

DIGEST_HOURS = 24
# How new an event must be to alert. Longer than the ground-truth cadence (6 hours), so a CVE
# that KEV lists a few hours after the event arrived still raises its alert.
ALERT_WINDOW = timedelta(hours=12)
# The rest wait for the next pass, 15 minutes later: a backlog never floods the chat.
MAX_ALERTS_PER_PASS = 5


class Channel(Protocol):
    async def send(self, message: str) -> SendResult: ...


@dataclass
class NotifySummary:
    sent: int = 0
    failed: int = 0
    withheld: int = 0
    skipped: str | None = None  # why nothing was tried

    @property
    def tried(self) -> int:
        return self.sent + self.failed + self.withheld


def _utcnow() -> datetime:
    return datetime.now(UTC)


async def _deliver(
    engine: Engine,
    channel: Channel,
    *,
    kind: Kind,
    key: str,
    message: str,
    now: datetime,
    event_id: str | None = None,
) -> Outcome | None:
    """Send `message` unless `key` was already sent; None when it was not tried."""
    claimed = await asyncio.to_thread(
        claim, engine, kind=kind, key=key, now=now, event_id=event_id
    )
    if claimed is None:
        return None
    try:
        result = await channel.send(message)
    except Exception as exc:  # noqa: BLE001 - a claim is always settled
        result = SendResult(False, error=type(exc).__name__)
    outcome: Outcome = "sent" if result.sent else "withheld" if result.withheld else "failed"
    await asyncio.to_thread(settle, engine, claimed, outcome, now=_utcnow(), error=result.error)
    if outcome == "sent":
        logger.info("notified %s (attempt %d)", key, claimed.attempt)
    else:
        logger.warning("%s: %s on attempt %d: %s", key, outcome, claimed.attempt, result.error)
    return outcome


def _count(summary: NotifySummary, outcome: Outcome | None) -> None:
    if outcome == "sent":
        summary.sent += 1
    elif outcome == "failed":
        summary.failed += 1
    elif outcome == "withheld":
        summary.withheld += 1


def _read_digest(engine: Engine, now: datetime) -> dict:
    with engine.connect() as conn:
        return load_digest(conn, now=now, hours=DIGEST_HOURS)


def _read_alerts(engine: Engine, since: datetime) -> list[AlertEvent]:
    with engine.connect() as conn:
        return load_alert_events(conn, since=since)


def _read_updates(engine: Engine, since: datetime) -> list[UpdateEntry]:
    with engine.connect() as conn:
        return load_update_entries(conn, since=since)


async def send_daily_digest(
    engine: Engine, settings: Settings, *, now: datetime, channel: Channel | None = None
) -> NotifySummary:
    """The digest of the 24 hours to `now`, once per Sydney date."""
    channel = channel or Telegram.from_settings(settings)
    if channel is None:
        return NotifySummary(skipped="no Telegram bot is configured")
    digest = await asyncio.to_thread(_read_digest, engine, now)
    summary = NotifySummary()
    outcome = await _deliver(
        engine,
        channel,
        kind="daily_digest",
        key=f"daily_digest:{now.astimezone(SYDNEY).date().isoformat()}",
        message=daily_digest(digest, site_url=settings.site_url),
        now=now,
    )
    _count(summary, outcome)
    return summary


async def send_critical_alerts(
    engine: Engine, settings: Settings, *, now: datetime, channel: Channel | None = None
) -> NotifySummary:
    """An alert for each qualifying event not yet alerted, at most `MAX_ALERTS_PER_PASS`."""
    channel = channel or Telegram.from_settings(settings)
    if channel is None:
        return NotifySummary(skipped="no Telegram bot is configured")
    events = await asyncio.to_thread(_read_alerts, engine, now - ALERT_WINDOW)
    summary = NotifySummary()
    for event in events:
        if summary.tried >= MAX_ALERTS_PER_PASS:
            break
        outcome = await _deliver(
            engine,
            channel,
            kind="critical_au_alert",
            key=f"critical_au_alert:{event.event_id}",
            message=critical_au_alert(event, site_url=settings.site_url),
            now=now,
            event_id=event.event_id,
        )
        _count(summary, outcome)
    return summary


async def send_developing_updates(
    engine: Engine, settings: Settings, *, now: datetime, channel: Channel | None = None
) -> NotifySummary:
    """A message for each qualifying change not yet sent, at most `MAX_ALERTS_PER_PASS`."""
    channel = channel or Telegram.from_settings(settings)
    if channel is None:
        return NotifySummary(skipped="no Telegram bot is configured")
    entries = await asyncio.to_thread(_read_updates, engine, now - ALERT_WINDOW)
    summary = NotifySummary()
    for entry in entries:
        if summary.tried >= MAX_ALERTS_PER_PASS:
            break
        outcome = await _deliver(
            engine,
            channel,
            kind="developing_update",
            key=f"developing_update:{entry.entry_id}",
            message=developing_update(entry, site_url=settings.site_url),
            now=now,
            event_id=entry.event_id,
        )
        _count(summary, outcome)
    return summary
