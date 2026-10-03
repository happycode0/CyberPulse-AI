"""One watchdog pass. worker/scheduler.py runs one every five minutes, and `--watchdog` runs one
now.

1. Probe the public site and Paperclip, and read the rest from the database.
2. Judge the snapshot (checks.py), then move the incidents (reconcile.py, worker/db/incidents.py).
3. Tell people. Telegram hears of each high or critical incident when it opens or reopens, and
   again when it resolves. It also hears of any incident whose breaker has tripped. The
   Incident routine in Paperclip hears of each high or critical incident when it opens or
   reopens, so TELETRAAN files it. Each notice is claimed in `notifications`, so none is sent
   twice. An incident that keeps reopening is announced MAX_REOPEN_NOTICES times, then waits
   in GET /ops/incidents.
4. Record the pass in job_runs.

When the database cannot be reached, nothing can be judged or recorded. After DB_FAILURES
passes like that in a row, Telegram is told once. That message is sent without a claim,
since there is nowhere to write one. Telegram is told again when the database is back.

The counters the probes keep between passes live in memory. A restart sets them back to
zero, which only delays a probe's finding by a few passes.
"""

import asyncio
import functools
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any

import httpx
from sqlalchemy import Engine
from sqlalchemy.exc import SQLAlchemyError

from worker.db.incidents import Incident, PassResult, record_pass
from worker.db.jobs import JobRun, record_job
from worker.db.notifications import Kind, Outcome, claim, settle
from worker.db.watchdog import gather, ping
from worker.notify.jobs import Channel
from worker.notify.messages import (
    database_back,
    database_unreachable,
    incident_breaker,
    incident_opened,
    incident_resolved,
)
from worker.notify.telegram import SendResult, Telegram
from worker.publish.push import publish_token_configured
from worker.settings import Settings
from worker.watchdog.checks import (
    NOTIFY_AT,
    PROBE_FAILURES,
    ProbeReading,
    SiteReading,
    evaluate,
)
from worker.watchdog.paperclip import IncidentRoutine, probe

logger = logging.getLogger(__name__)

EVERY_MINUTES = 5
DB_FAILURES = 3
MAX_REOPEN_NOTICES = 3
SITE_TIMEOUT_SECONDS = 20.0
# The kinds the crew is not told of: with Paperclip down, there is no crew to tell.
NOT_FOR_THE_CREW = frozenset({"paperclip-down"})


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass
class PassSummary:
    database: bool = True
    findings: int = 0
    uncovered: list[str] = field(default_factory=list)  # parts that could not be read
    opened: int = 0
    resolved: int = 0
    unresolved: int = 0
    sent: int = 0
    failed: int = 0


def _suffix(incident: Incident) -> str | None:
    """What tells one opening of an incident's notices from the next; None past the cap."""
    if incident.reopened > MAX_REOPEN_NOTICES:
        return None
    return f":r{incident.reopened}" if incident.reopened else ""


def routine_payload(incident: Incident) -> dict[str, Any]:
    """What the Incident routine is told: enough to name it. TELETRAAN reads the rest."""
    return {
        "incident": incident.id,
        "ref": f"INC-{incident.id}",
        "kind": incident.kind,
        "subject": incident.subject,
        "severity": incident.severity,
        "title": incident.title,
        "read": f"GET /ops/incidents/{incident.id}",
    }


async def _once(
    engine: Engine,
    *,
    kind: Kind,
    key: str,
    channel: str,
    now: datetime,
    send: Callable[[], Awaitable[SendResult]],
) -> Outcome | None:
    """Send unless `key` was already sent; None when it was not tried."""
    claimed = await asyncio.to_thread(claim, engine, kind=kind, key=key, now=now, channel=channel)
    if claimed is None:
        return None
    try:
        result = await send()
    except Exception as exc:  # noqa: BLE001 - a claim is always settled
        result = SendResult(False, error=type(exc).__name__)
    outcome: Outcome = "sent" if result.sent else "withheld" if result.withheld else "failed"
    await asyncio.to_thread(settle, engine, claimed, outcome, now=_utcnow(), error=result.error)
    if outcome == "sent":
        logger.info("notified %s (attempt %d)", key, claimed.attempt)
    else:
        logger.warning("%s: %s on attempt %d: %s", key, outcome, claimed.attempt, result.error)
    return outcome


class Watchdog:
    def __init__(
        self,
        settings: Settings,
        *,
        telegram: Callable[[Settings], Channel | None] = Telegram.from_settings,
        routine: Callable[[Settings], IncidentRoutine | None] = IncidentRoutine.from_settings,
        token_configured: Callable[[], bool] = publish_token_configured,
        site_transport: httpx.AsyncBaseTransport | None = None,
        paperclip_transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._settings = settings
        self._telegram = telegram
        self._routine = routine
        self._token_configured = token_configured
        self._site_transport = site_transport
        self._paperclip_transport = paperclip_transport
        self.site_failures = 0
        self.paperclip_failures = 0
        self.db_failures = 0
        self.db_down_since: datetime | None = None
        self.db_alerted = False

    # --- Probes ---

    async def _read_site(self, now: datetime) -> SiteReading | None:
        """The public site's data, as a visitor gets it; None when this host does not push, or
        while reads have failed fewer than PROBE_FAILURES times in a row."""
        if not self._token_configured():
            return None
        url = f"{self._settings.site_url.rstrip('/')}/data/system-status.json"
        error: str
        try:
            async with httpx.AsyncClient(
                timeout=SITE_TIMEOUT_SECONDS,
                transport=self._site_transport,
                headers={"User-Agent": self._settings.user_agent, "Cache-Control": "no-cache"},
                follow_redirects=True,
            ) as http:
                # A query string of its own, so no cache between here and Pages answers for it.
                response = await http.get(url, params={"watchdog": int(now.timestamp())})
            if response.status_code != 200:
                error = f"HTTP {response.status_code}"
            else:
                generated = datetime.fromisoformat(response.json()["generated_at"])
                if generated.tzinfo is None:
                    raise ValueError("no time zone")
                self.site_failures = 0
                return SiteReading(generated)
        except httpx.HTTPError as exc:
            error = type(exc).__name__
        except (ValueError, KeyError, TypeError):
            error = "system-status.json cannot be read"
        self.site_failures += 1
        if self.site_failures < PROBE_FAILURES:
            return None
        return SiteReading(None, error, self.site_failures)

    async def _probe_paperclip(self) -> ProbeReading | None:
        """Whether Paperclip answers; None when no probe is set, or while probes have failed
        fewer than PROBE_FAILURES times in a row, so a blip neither opens nor clears anything."""
        url = self._settings.watchdog_paperclip_url.strip()
        if not url:
            return None
        error = await probe(
            url, user_agent=self._settings.user_agent, transport=self._paperclip_transport
        )
        self.paperclip_failures = 0 if error is None else self.paperclip_failures + 1
        if 0 < self.paperclip_failures < PROBE_FAILURES:
            return None
        return ProbeReading(self.paperclip_failures, error)

    # --- The database ---

    async def _database_down(self, now: datetime, telegram: Channel | None) -> None:
        self.db_failures += 1
        self.db_down_since = self.db_down_since or now
        if self.db_failures < DB_FAILURES or self.db_alerted or telegram is None:
            return
        try:
            result = await telegram.send(database_unreachable(self.db_down_since))
        except Exception as exc:  # noqa: BLE001 - the next pass tries again
            result = SendResult(False, error=type(exc).__name__)
        self.db_alerted = result.sent
        if not result.sent:
            logger.warning("database-down notice not sent: %s", result.error)

    async def _database_back(self, engine: Engine, now: datetime, telegram: Channel | None) -> None:
        since, alerted = self.db_down_since, self.db_alerted
        self.db_failures, self.db_down_since, self.db_alerted = 0, None, False
        if since is None or not alerted or telegram is None:
            return
        await _once(
            engine,
            kind="system_failure",
            key=f"system_failure:db:{since.isoformat()}",
            channel="telegram",
            now=now,
            send=functools.partial(telegram.send, database_back(since, now)),
        )

    # --- Telling ---

    async def _notify(
        self,
        engine: Engine,
        result: PassResult,
        now: datetime,
        telegram: Channel | None,
        routine: IncidentRoutine | None,
        summary: PassSummary,
    ) -> None:
        def count(outcome: Outcome | None) -> None:
            if outcome == "sent":
                summary.sent += 1
            elif outcome is not None:
                summary.failed += 1

        for incident in result.unresolved:
            suffix = _suffix(incident)
            crew = routine is not None and incident.kind not in NOT_FOR_THE_CREW
            if incident.severity in NOTIFY_AT and suffix is not None:
                if telegram is not None:
                    message = incident_opened(incident, crew=crew and not incident.needs_human)
                    count(
                        await _once(
                            engine,
                            kind="incident",
                            key=f"incident:{incident.id}{suffix}",
                            channel="telegram",
                            now=now,
                            send=functools.partial(telegram.send, message),
                        )
                    )
                if crew and routine is not None and not incident.needs_human:
                    count(
                        await _once(
                            engine,
                            kind="incident",
                            key=f"paperclip:incident:{incident.id}{suffix}",
                            channel="paperclip",
                            now=now,
                            send=functools.partial(routine.fire, routine_payload(incident)),
                        )
                    )
            if incident.needs_human and telegram is not None:
                count(
                    await _once(
                        engine,
                        kind="incident",
                        key=f"incident-breaker:{incident.id}",
                        channel="telegram",
                        now=now,
                        send=functools.partial(telegram.send, incident_breaker(incident)),
                    )
                )
        for incident in result.resolved:
            suffix = _suffix(incident)
            if incident.severity in NOTIFY_AT and suffix is not None and telegram is not None:
                count(
                    await _once(
                        engine,
                        kind="incident",
                        key=f"incident-resolved:{incident.id}{suffix}",
                        channel="telegram",
                        now=now,
                        send=functools.partial(telegram.send, incident_resolved(incident)),
                    )
                )

    # --- The pass ---

    async def run_pass(self, engine: Engine, now: datetime | None = None) -> PassSummary:
        started = now or _utcnow()
        summary = PassSummary()
        telegram = self._telegram(self._settings)
        site, paperclip = await asyncio.gather(self._read_site(started), self._probe_paperclip())
        if not await asyncio.to_thread(ping, engine):
            summary.database = False
            await self._database_down(started, telegram)
            logger.warning("watchdog: the database cannot be reached (%d)", self.db_failures)
            return summary
        await self._database_back(engine, started, telegram)

        snapshot = await asyncio.to_thread(
            gather, engine, started, budget=self._settings.ai_monthly_budget_usd
        )
        snapshot = replace(snapshot, site=site, paperclip=paperclip)
        summary.uncovered = [
            name
            for name in ("lanes", "sources", "volume", "jobs", "cost")
            if getattr(snapshot, name) is None
        ]
        findings, covered = evaluate(snapshot)
        summary.findings = len(findings)
        result = await asyncio.to_thread(record_pass, engine, findings, covered, started)
        summary.opened, summary.resolved = len(result.opened), len(result.resolved)
        summary.unresolved = len(result.unresolved)
        await self._notify(
            engine, result, started, telegram, self._routine(self._settings), summary
        )
        for incident in result.opened:
            logger.warning(
                "incident INC-%d opened%s: %s %s (%s)",
                incident.id,
                " again" if incident.reopened else "",
                incident.kind,
                incident.subject or "-",
                incident.severity,
            )
        for incident in result.resolved:
            logger.info(
                "incident INC-%d resolved: %s %s",
                incident.id,
                incident.kind,
                incident.subject or "-",
            )

        run = JobRun(
            "watchdog",
            started,
            max(started, _utcnow()),
            completed=True,
            errors=len(summary.uncovered) + summary.failed,
            changed=bool(result.opened or result.resolved),
        )
        try:
            await asyncio.to_thread(record_job, engine, run)
        except SQLAlchemyError as exc:
            logger.warning("watchdog pass not recorded: %s", type(exc).__name__)
        return summary
