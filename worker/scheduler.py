"""Scheduling. FAST and NORMAL lanes, the ground-truth sync, AI enrichment, source discovery, the
model scan and gauntlet, the Telegram notifications and the watchdog run in the worker; DEEP is a
Paperclip routine, so it has no schedule here and is only reachable through `--lane deep --once`."""

import asyncio
import logging
import signal
from collections.abc import Iterable
from datetime import UTC, datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from worker import ops_api
from worker.ai.enrich import AiLayer, enrich_pending
from worker.ai.mitre import suggest_techniques
from worker.ai.scout import run_gauntlet, scan_models
from worker.cadence import FAST_INTERVAL_MINUTES, fast_minutes
from worker.db.jobs import JobRun, record_job
from worker.db.session import get_engine
from worker.discovery.run import run_gate, run_search
from worker.groundtruth.sync import sync_groundtruth
from worker.models import Lane
from worker.notify.jobs import (
    send_critical_alerts,
    send_daily_digest,
    send_developing_updates,
    send_source_activations,
)
from worker.notify.telegram import Telegram
from worker.pipeline import run as pipeline_run
from worker.publish.push import publish_token_configured
from worker.publish.run import publish_now, push_now
from worker.settings import get_settings
from worker.watchdog.run import Watchdog

logger = logging.getLogger(__name__)


def _minute_list(minutes: Iterable[int]) -> str:
    return ",".join(str(m) for m in sorted(minutes))


# The FAST lane runs every FAST_INTERVAL_MINUTES (worker/cadence.py), from the top of the hour:
# hourly since 2026-10-04, for stability (PLAN.md §2.3). The two lanes share :00 every four
# hours, which is safe: `publish_now` holds a lock (worker/publish/run.py).
FAST_MINUTES = fast_minutes()
SCHEDULE: dict[Lane, str] = {
    Lane.FAST: f"{_minute_list(FAST_MINUTES)} * * * *",
    Lane.NORMAL: "0 */4 * * *",
}

# A run that started late (the loop was busy, or the process was paused) is still worth running
# while it can finish well before the next one: two thirds of the cadence for FAST, half an hour
# for NORMAL.
MISFIRE_GRACE_SECONDS = {Lane.FAST: FAST_INTERVAL_MINUTES * 60 * 2 // 3, Lane.NORMAL: 1800}

# The registers answer on their own clock, which is much slower than any feed: EPSS republishes once
# a day, KEV on CISA's working days. Four passes a day is enough to pick either up within hours while
# leaving the CVSS backfill four batches a day to work through — and the sync writes only what moved,
# so the three passes a day that find an unchanged EPSS file cost a download and no rows.
#
# :25 rather than :00 keeps it off the hour the FAST and NORMAL lanes share, and clear of the
# enrichment passes and the alerts. Nothing breaks if they overlap — `publish_now` holds a lock and
# `rescore` is idempotent — but a sync competing with a collection for the same connection pool
# makes both slower for no reason.
GROUNDTRUTH_SCHEDULE = "25 */6 * * *"
GROUNDTRUTH_JOB_ID = "groundtruth-sync"

# Six hours between passes, so an hour late is still well within the cadence. A missed pass costs
# nothing permanent — the registers are read in full every time, not as a delta — but KEV is the
# strongest exploitation signal the site has and there is no reason to skip a reading of it.
GROUNDTRUTH_MISFIRE_GRACE_SECONDS = 3600

# Twice an hour, whatever the FAST cadence. :05 enriches what the :00 run collected, once it is
# stored. :35 is the backlog pass: events arrive at the same rate however often we collect, and
# each pass takes a small batch, so one pass an hour would halve what gets enriched. A small batch
# keeps the spend per pass small and lets the budget mode change between passes. A pass with
# nothing pending costs a budget reading and no calls.
ENRICH_MINUTES = (5, 35)
ENRICH_SCHEDULE = f"{_minute_list(ENRICH_MINUTES)} * * * *"
ENRICH_JOB_ID = "ai-enrichment"
ENRICH_MISFIRE_GRACE_SECONDS = 900

# The daily digest at 07:10 Sydney time: ten minutes past the hour, so it reports the 07:00 run
# and the enrichment pass after it. 08:10 and 09:10 send it only if 07:10 failed or was missed:
# each Sydney date's digest is sent once (worker/notify/jobs.py).
DIGEST_SCHEDULE = "10 7,8,9 * * *"
DIGEST_TIMEZONE = "Australia/Sydney"
DIGEST_JOB_ID = "daily-digest"
DIGEST_MISFIRE_GRACE_SECONDS = 1800

# Minutes an alert pass waits after a FAST run, which is over in seconds, and after an enrichment
# pass, which can take several minutes.
ALERT_AFTER_COLLECTION = 3
ALERT_AFTER_ENRICHMENT = 13


def alert_minutes(fast: Iterable[int], enrich: Iterable[int]) -> tuple[int, ...]:
    """The alert pass follows every FAST run, so a critical event collected at :00 is in the
    chat by :03. It also follows every enrichment pass, which can raise an event's Australian
    relevance. Hourly, that is :03, :18 (after :05) and :48 (after the :35 backlog pass)."""
    after_runs = {(m + ALERT_AFTER_COLLECTION) % 60 for m in fast}
    after_passes = {(m + ALERT_AFTER_ENRICHMENT) % 60 for m in enrich}
    return tuple(sorted(after_runs | after_passes))


ALERT_SCHEDULE = f"{_minute_list(alert_minutes(FAST_MINUTES, ENRICH_MINUTES))} * * * *"
ALERT_JOB_ID = "critical-alerts"
ALERT_MISFIRE_GRACE_SECONDS = 600

# The nightly discovery search at 03:10 Sydney time, off the hourly run's minute. Tavily's credits
# are counted over the last 24 hours (worker/discovery/run.py), so a pass that runs late still
# keeps within the day's allowance.
DISCOVERY_SCHEDULE = "10 3 * * *"
DISCOVERY_JOB_ID = "source-discovery"
DISCOVERY_MISFIRE_GRACE_SECONDS = 3600

# SERAPH's gate every 4 hours, ten minutes before each NORMAL run, which then collects anything it
# activated. A probe is one fetch per candidate per pass, so 6 healthy probes in a row
# (config/discovery.yaml) take a day.
GATE_SCHEDULE = "50 3-23/4 * * *"
GATE_JOB_ID = "source-gate"
GATE_MISFIRE_GRACE_SECONDS = 1800

# RIPPERDOC's model scan at 03:20 Sydney time, after the discovery search and clear of every
# other job's minute. A model the guard now refuses leaves its chain from the next enrichment
# pass, so a day is the longest a price rise goes unnoticed (worker/ai/scout.py).
MODEL_SCAN_SCHEDULE = "20 3 * * *"
MODEL_SCAN_JOB_ID = "model-scan"
MODEL_SCAN_MISFIRE_GRACE_SECONDS = 3600

# The gauntlet on Sunday at 03:40 Sydney time, after that day's scan. A day name, not 0:
# APScheduler counts the days of the week from Monday.
GAUNTLET_SCHEDULE = "40 3 * * sun"
GAUNTLET_JOB_ID = "model-gauntlet"
GAUNTLET_MISFIRE_GRACE_SECONDS = 3600

# The watchdog every five minutes from :02 (worker/watchdog/run.py). Every other job starts on a
# minute that is a multiple of five, or three past one (:00, :03, :05, :18 and so on), so the
# watchdog never starts with one. At :02 it reads the :00 run once it has finished, not while it
# is in progress.
WATCHDOG_SCHEDULE = "2-57/5 * * * *"
WATCHDOG_JOB_ID = "watchdog"
WATCHDOG_MISFIRE_GRACE_SECONDS = 240

# Kept for the life of the process: the governor in it must remember a 402 from one pass to the
# next (worker/ai/enrich.py), and the model scan hands it the ladder as it checked it.
_ai_layer: AiLayer | None = None
# Kept for the same reason: its probes count failures from one pass to the next.
_watchdog: Watchdog | None = None


def _layer() -> AiLayer:
    global _ai_layer
    if _ai_layer is None:
        _ai_layer = AiLayer()
    return _ai_layer


def _the_watchdog() -> Watchdog:
    global _watchdog
    if _watchdog is None:
        _watchdog = Watchdog(get_settings())
    return _watchdog


def job_id(lane: Lane) -> str:
    return f"lane-{lane.value}"


def _now() -> datetime:
    return datetime.now(UTC)


async def _record(run: JobRun) -> None:
    """Leave a job_runs row for the ops API's wakes. Never fatal: a pass that worked is not
    undone by its row failing to write, and the next pass writes another."""
    try:
        await asyncio.to_thread(record_job, get_engine(), run)
    except Exception:
        logger.exception("could not record the %s pass", run.job)


async def _publish(after: str, *, stands: str) -> None:
    """Rebuild data/*.json, then push it to the `data` branch the site is built from.

    Neither step failing is fatal, and a push is not tried after a failed publish: it would push
    the previous files again. A failed push leaves `data/` current on this host and the site a
    publish behind; the next publish pushes again. `stands` says what the failure leaves intact.
    Each step that is tried leaves a job_runs row, which is how the watchdog tells when they
    keep failing.
    """
    started = _now()
    try:
        written = await publish_now()
    except Exception as exc:
        logger.exception("publish after %s failed; %s", after, stands)
        await _record(JobRun("publish", started, _now(), False, note=type(exc).__name__))
        return
    logger.info("published %d files after %s", len(written), after)
    await _record(JobRun("publish", started, _now(), True, changed=bool(written)))

    started = _now()
    try:
        pushed = await push_now()
    except Exception as exc:
        logger.exception("push after %s failed; the next publish pushes again", after)
        await _record(JobRun("push", started, _now(), False, note=type(exc).__name__))
        return
    if pushed is None:
        return  # no publish token on this host; serve() said so at start
    await _record(JobRun("push", started, _now(), True, changed=pushed.pushed))
    if pushed.pushed:
        logger.info("pushed %s to the data branch", pushed.commit_sha)
    else:
        logger.info("data branch already current; nothing pushed")


async def _run_lane_job(lane: Lane) -> None:
    """Run one lane and republish from it; either step failing leaves the schedule running.

    Publishing belongs here rather than only behind `--publish`. PLAN.md §11's failure model says
    that with Paperclip down the worker "keeps collecting and publishing", which it can only do if
    a scheduled run is what triggers a publish — otherwise the database advances with every run
    while data/*.json keeps describing whichever collection was last published by hand, and the
    site reports a stale snapshot as current.
    """
    try:
        await pipeline_run.run_lane(lane)
    except Exception:
        # Not followed by a publish. run_lane already absorbs per-source failures and returns a
        # summary counting them, so reaching here means the run itself could not proceed — the
        # database is unreachable or the registry is unreadable. Publishing would either fail for
        # the same reason or rewrite the previous files unchanged; neither is worth the second
        # traceback in the log.
        logger.exception("scheduled %s run failed", lane.value)
        return

    # Deliberately not fatal. Collection is the irreplaceable half: a missed publish is corrected
    # by the next publish, one FAST interval later at most, whereas a lane that stops running
    # loses items that have already fallen off the end of their feed and cannot be re-fetched.
    await _publish(f"the {lane.value} lane", stands="collection is unaffected")


async def _groundtruth_job() -> None:
    """Read the registers and republish if anything they said has changed.

    Failing here is never fatal to the schedule. `sync_groundtruth` already absorbs a register being
    down — that is the normal case it is designed for — so reaching the handler means something
    structural, and the next pass in six hours gets another go. Nothing is lost in the meantime: the
    registers are read in full each time rather than as a delta, so a skipped pass is a delay and
    not a gap.
    """
    started = _now()
    try:
        summary = await sync_groundtruth()
    except Exception:
        logger.exception("scheduled ground-truth sync failed")
        await _record(JobRun("groundtruth", started, _now(), completed=False))
        return
    await _record(
        JobRun(
            "groundtruth",
            started,
            _now(),
            completed=True,
            errors=len(summary.errors),
            changed=summary.changed_anything,
        )
    )

    if not summary.changed_anything:
        logger.info("ground-truth sync changed nothing; not republishing")
        return
    await _publish("the ground-truth sync", stands="the sync itself stands")


async def _enrich_job() -> None:
    """Enrich a batch of pending events, suggest MITRE techniques for a few enriched ones, and
    republish if either changed anything.

    Never fatal to the schedule. With no key, a ladder that fails the guard or no money, the pass
    calls nothing and returns, and the events stay `pending_enrichment` on the site. The two
    passes fail apart: suggestions still run after an enrichment pass that raised.
    """
    layer = _layer()
    started = _now()
    changed, completed, errors = False, True, 0
    try:
        enriched = await enrich_pending(layer=layer)
        changed |= enriched.changed_anything
        errors += enriched.failed + len(enriched.errors)
    except Exception:
        logger.exception("scheduled enrichment failed")
        completed = False
    try:
        suggested = await suggest_techniques(layer=layer)
        changed |= suggested.changed_anything
        errors += suggested.failed
    except Exception:
        logger.exception("scheduled MITRE suggestions failed")
        completed = False
    await _record(JobRun("enrichment", started, _now(), completed, errors, changed))

    if not changed:
        return
    await _publish("enrichment", stands="the enrichment itself stands")


async def _digest_job() -> None:
    """Never fatal to the schedule; 08:10 and 09:10 try again."""
    try:
        await send_daily_digest(get_engine(), get_settings(), now=_now())
    except Exception:
        logger.exception("daily digest failed")


async def _alert_job() -> None:
    """Never fatal to the schedule; an alert not sent now is tried by the next pass."""
    try:
        await send_critical_alerts(get_engine(), get_settings(), now=_now())
    except Exception:
        logger.exception("critical alert pass failed")
    try:
        await send_developing_updates(get_engine(), get_settings(), now=_now())
    except Exception:
        logger.exception("developing update pass failed")


async def _discovery_job() -> None:
    """Never fatal to the schedule; tomorrow night searches again."""
    started = _now()
    try:
        summary = await run_search()
    except Exception:
        logger.exception("scheduled discovery search failed")
        await _record(JobRun("discovery", started, _now(), completed=False))
        return
    if summary.skipped:
        logger.info("discovery search skipped: %s", summary.skipped)
    await _record(
        JobRun(
            "discovery", started, _now(), completed=True, errors=len(summary.errors),
            changed=summary.new_hosts > 0,
        )
    )


async def _gate_job() -> None:
    """Never fatal to the schedule; the next pass probes again. Nothing is published: an activated
    source's first items arrive with the next NORMAL run, which publishes."""
    started = _now()
    try:
        summary = await run_gate()
    except Exception:
        logger.exception("scheduled source-gate pass failed")
        await _record(JobRun("source-gate", started, _now(), completed=False))
    else:
        await _record(
            JobRun(
                "source-gate", started, _now(), completed=True, errors=len(summary.errors),
                changed=summary.changed_anything,
            )
        )
    try:
        await send_source_activations(get_engine(), get_settings(), now=_now())
    except Exception:
        logger.exception("new-source notification pass failed")


async def _model_scan_job() -> None:
    """Never fatal to the schedule; tomorrow's scan reads the list again. The ladder it checked
    goes to the AI layer: a pruned one is used as it is, and one that did not pass makes the next
    enrichment pass check before it calls. One that could not be checked changes nothing."""
    started = _now()
    try:
        summary = await scan_models()
    except Exception:
        logger.exception("scheduled model scan failed")
        await _record(JobRun("model-scan", started, _now(), completed=False))
        return
    if summary.verified is not None:
        _layer().adopt(summary.verified)
    elif summary.ladder_failed:
        _layer().adopt(None)
    await _record(
        JobRun(
            "model-scan",
            started,
            _now(),
            completed=True,
            errors=len(summary.errors),
            changed=summary.changed_anything,
        )
    )


async def _gauntlet_job() -> None:
    """Never fatal to the schedule; next Sunday runs again. Nothing is published: a proposal is
    for RIPPERDOC to raise, and nothing changes until config/models.yaml does."""
    started = _now()
    try:
        summary = await run_gauntlet()
    except Exception:
        logger.exception("scheduled model gauntlet failed")
        await _record(JobRun("model-gauntlet", started, _now(), completed=False))
        return
    if summary.skipped:
        logger.info("model gauntlet skipped: %s", summary.skipped)
    await _record(
        JobRun(
            "model-gauntlet",
            started,
            _now(),
            completed=True,
            errors=sum(m.errors for m in summary.measured if not m.reused),
            changed=summary.changed_anything,
        )
    )


async def _watchdog_job() -> None:
    """Never fatal to the schedule; the next pass is five minutes away. A pass records itself
    (worker/watchdog/run.py); one that raised is recorded here."""
    started = _now()
    try:
        await _the_watchdog().run_pass(get_engine(), started)
    except Exception as exc:
        logger.exception("watchdog pass failed")
        await _record(JobRun("watchdog", started, _now(), False, note=type(exc).__name__))


def build_scheduler(lanes: tuple[Lane, ...] | None = None) -> AsyncIOScheduler:
    """A configured, not yet started scheduler.

    `max_instances=1` and `coalesce=True` mean a run that outlasts its interval is not
    stacked on top of itself, and missed ticks collapse into one.

    The ground-truth sync, enrichment, discovery, the model scan and gauntlet, notifications and
    the watchdog are added only for the default schedule. `lanes` comes from `--lane`, which means "schedule this one thing", and silently bringing a register sync or
    model calls along with it would make the narrow form impossible to ask for.
    """
    scheduler = AsyncIOScheduler(timezone="UTC")
    if lanes is None:
        scheduler.add_job(
            _groundtruth_job,
            CronTrigger.from_crontab(GROUNDTRUTH_SCHEDULE, timezone="UTC"),
            id=GROUNDTRUTH_JOB_ID,
            name="ground-truth sync",
            max_instances=1,
            coalesce=True,
            misfire_grace_time=GROUNDTRUTH_MISFIRE_GRACE_SECONDS,
            replace_existing=True,
        )
        scheduler.add_job(
            _enrich_job,
            CronTrigger.from_crontab(ENRICH_SCHEDULE, timezone="UTC"),
            id=ENRICH_JOB_ID,
            name="AI enrichment",
            max_instances=1,
            coalesce=True,
            misfire_grace_time=ENRICH_MISFIRE_GRACE_SECONDS,
            replace_existing=True,
        )
        scheduler.add_job(
            _digest_job,
            CronTrigger.from_crontab(DIGEST_SCHEDULE, timezone=DIGEST_TIMEZONE),
            id=DIGEST_JOB_ID,
            name="daily digest",
            max_instances=1,
            coalesce=True,
            misfire_grace_time=DIGEST_MISFIRE_GRACE_SECONDS,
            replace_existing=True,
        )
        scheduler.add_job(
            _alert_job,
            CronTrigger.from_crontab(ALERT_SCHEDULE, timezone="UTC"),
            id=ALERT_JOB_ID,
            name="critical alerts",
            max_instances=1,
            coalesce=True,
            misfire_grace_time=ALERT_MISFIRE_GRACE_SECONDS,
            replace_existing=True,
        )
        scheduler.add_job(
            _discovery_job,
            CronTrigger.from_crontab(DISCOVERY_SCHEDULE, timezone=DIGEST_TIMEZONE),
            id=DISCOVERY_JOB_ID,
            name="discovery search",
            max_instances=1,
            coalesce=True,
            misfire_grace_time=DISCOVERY_MISFIRE_GRACE_SECONDS,
            replace_existing=True,
        )
        scheduler.add_job(
            _gate_job,
            CronTrigger.from_crontab(GATE_SCHEDULE, timezone="UTC"),
            id=GATE_JOB_ID,
            name="source gate",
            max_instances=1,
            coalesce=True,
            misfire_grace_time=GATE_MISFIRE_GRACE_SECONDS,
            replace_existing=True,
        )
        scheduler.add_job(
            _model_scan_job,
            CronTrigger.from_crontab(MODEL_SCAN_SCHEDULE, timezone=DIGEST_TIMEZONE),
            id=MODEL_SCAN_JOB_ID,
            name="model scan",
            max_instances=1,
            coalesce=True,
            misfire_grace_time=MODEL_SCAN_MISFIRE_GRACE_SECONDS,
            replace_existing=True,
        )
        scheduler.add_job(
            _gauntlet_job,
            CronTrigger.from_crontab(GAUNTLET_SCHEDULE, timezone=DIGEST_TIMEZONE),
            id=GAUNTLET_JOB_ID,
            name="model gauntlet",
            max_instances=1,
            coalesce=True,
            misfire_grace_time=GAUNTLET_MISFIRE_GRACE_SECONDS,
            replace_existing=True,
        )
        scheduler.add_job(
            _watchdog_job,
            CronTrigger.from_crontab(WATCHDOG_SCHEDULE, timezone="UTC"),
            id=WATCHDOG_JOB_ID,
            name="watchdog",
            max_instances=1,
            coalesce=True,
            misfire_grace_time=WATCHDOG_MISFIRE_GRACE_SECONDS,
            replace_existing=True,
        )
    for lane in lanes if lanes is not None else tuple(SCHEDULE):
        scheduler.add_job(
            _run_lane_job,
            CronTrigger.from_crontab(SCHEDULE[lane], timezone="UTC"),
            args=[lane],
            id=job_id(lane),
            name=f"{lane.value} lane",
            max_instances=1,
            coalesce=True,
            misfire_grace_time=MISFIRE_GRACE_SECONDS[lane],
            replace_existing=True,
        )
    return scheduler


async def serve(lanes: tuple[Lane, ...] | None = None) -> None:
    """Run the scheduler until SIGINT / SIGTERM.

    The ops API (worker/ops_api.py) starts with the default schedule only: the agents it answers
    describe the whole worker, and a `--lane` process is a narrow one beside it.
    """
    scheduler = build_scheduler(lanes)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    scheduler.start()
    logger.info("scheduler started: %s", ", ".join(j.id for j in scheduler.get_jobs()))
    if publish_token_configured():
        logger.info("each publish is pushed to the data branch")
    else:
        logger.info("no publish token: data/ is written here and not pushed")
    if lanes is None:
        if Telegram.from_settings(get_settings()) is None:
            logger.info("no Telegram bot: the digest and alerts are not sent")
        else:
            logger.info("Telegram notifications are on")
        settings = get_settings()
        if settings.paperclip_incident_webhook_url and settings.paperclip_incident_webhook_secret:
            logger.info("incidents go to the Incident routine in Paperclip")
        else:
            logger.info("no Incident routine webhook: incidents are not sent to Paperclip")
    ops = ops_api.start(get_settings(), get_engine()) if lanes is None else None
    try:
        await stop.wait()
    finally:
        scheduler.shutdown(wait=False)
        if ops is not None:
            await asyncio.to_thread(ops_api.stop, ops)
        logger.info("scheduler stopped")
