"""Scheduling. FAST and NORMAL lanes, the ground-truth sync, AI enrichment, source discovery, the
model scan and gauntlet, the Telegram notifications and the watchdog run in the worker; DEEP is a
Paperclip routine, so it has no schedule here and is only reachable through `--lane deep --once`."""

import asyncio
import logging
import signal
from datetime import UTC, datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from worker import ops_api
from worker.ai.enrich import AiLayer, enrich_pending
from worker.ai.mitre import suggest_techniques
from worker.ai.scout import run_gauntlet, scan_models
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
# The schedule itself lives in worker/schedule.py, which the publisher reads too; the names
# are re-exported here for worker/main.py and the tests.
from worker.schedule import (  # noqa: F401
    ALERT_AFTER_COLLECTION,
    ALERT_AFTER_ENRICHMENT,
    ALERT_JOB_ID,
    ALERT_MISFIRE_GRACE_SECONDS,
    ALERT_SCHEDULE,
    DIGEST_JOB_ID,
    DIGEST_MISFIRE_GRACE_SECONDS,
    DIGEST_SCHEDULE,
    DIGEST_TIMEZONE,
    DISCOVERY_JOB_ID,
    DISCOVERY_MISFIRE_GRACE_SECONDS,
    DISCOVERY_SCHEDULE,
    ENRICH_JOB_ID,
    ENRICH_MINUTES,
    ENRICH_MISFIRE_GRACE_SECONDS,
    ENRICH_SCHEDULE,
    FAST_MINUTES,
    GATE_JOB_ID,
    GATE_MISFIRE_GRACE_SECONDS,
    GATE_SCHEDULE,
    GAUNTLET_JOB_ID,
    GAUNTLET_MISFIRE_GRACE_SECONDS,
    GAUNTLET_SCHEDULE,
    GROUNDTRUTH_JOB_ID,
    GROUNDTRUTH_MISFIRE_GRACE_SECONDS,
    GROUNDTRUTH_SCHEDULE,
    JOBS,
    LANE_JOBS,
    MISFIRE_GRACE_SECONDS,
    MODEL_SCAN_JOB_ID,
    MODEL_SCAN_MISFIRE_GRACE_SECONDS,
    MODEL_SCAN_SCHEDULE,
    SCHEDULE,
    WATCHDOG_JOB_ID,
    WATCHDOG_MISFIRE_GRACE_SECONDS,
    WATCHDOG_SCHEDULE,
    alert_minutes,
    job_id,
)
from worker.settings import get_settings
from worker.watchdog.run import Watchdog

logger = logging.getLogger(__name__)


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
    """A configured, not yet started scheduler, with the jobs worker/schedule.py lists.

    `max_instances=1` and `coalesce=True` mean a run that outlasts its interval is not
    stacked on top of itself, and missed ticks collapse into one.

    The ground-truth sync, enrichment, discovery, the model scan and gauntlet, notifications and
    the watchdog are added only for the default schedule. `lanes` comes from `--lane`, which
    means "schedule this one thing", and silently bringing a register sync or model calls along
    with it would make the narrow form impossible to ask for.
    """
    # Looked up here, not at import, so a test that replaces a handler is the one scheduled.
    handlers = {
        GROUNDTRUTH_JOB_ID: _groundtruth_job,
        ENRICH_JOB_ID: _enrich_job,
        DIGEST_JOB_ID: _digest_job,
        ALERT_JOB_ID: _alert_job,
        DISCOVERY_JOB_ID: _discovery_job,
        GATE_JOB_ID: _gate_job,
        MODEL_SCAN_JOB_ID: _model_scan_job,
        GAUNTLET_JOB_ID: _gauntlet_job,
        WATCHDOG_JOB_ID: _watchdog_job,
    }
    scheduler = AsyncIOScheduler(timezone="UTC")
    if lanes is None:
        for job in JOBS:
            scheduler.add_job(
                handlers[job.id],
                CronTrigger.from_crontab(job.cron, timezone=job.timezone),
                id=job.id,
                name=job.label,
                max_instances=1,
                coalesce=True,
                misfire_grace_time=job.misfire_grace_seconds,
                replace_existing=True,
            )
    for lane in lanes if lanes is not None else tuple(SCHEDULE):
        job = LANE_JOBS[lane]
        scheduler.add_job(
            _run_lane_job,
            CronTrigger.from_crontab(job.cron, timezone=job.timezone),
            args=[lane],
            id=job.id,
            name=job.label,
            max_instances=1,
            coalesce=True,
            misfire_grace_time=job.misfire_grace_seconds,
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
