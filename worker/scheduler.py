"""Scheduling. FAST and NORMAL lanes plus the ground-truth sync run in the worker; DEEP is a
Paperclip routine, so it has no schedule here and is only reachable through `--lane deep --once`."""

import asyncio
import logging
import signal

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from worker.groundtruth.sync import sync_groundtruth
from worker.models import Lane
from worker.pipeline import run as pipeline_run
from worker.publish.run import publish_now

logger = logging.getLogger(__name__)

SCHEDULE: dict[Lane, str] = {
    Lane.FAST: "*/15 * * * *",
    Lane.NORMAL: "0 */4 * * *",
}

# A run that started late (the loop was busy, or the process was paused) is still worth
# running if it is less than one cadence overdue.
MISFIRE_GRACE_SECONDS = {Lane.FAST: 600, Lane.NORMAL: 1800}

# The registers answer on their own clock, which is much slower than any feed: EPSS republishes once
# a day, KEV on CISA's working days. Four passes a day is enough to pick either up within hours while
# leaving the CVSS backfill four batches a day to work through — and the sync writes only what moved,
# so the three passes a day that find an unchanged EPSS file cost a download and no rows.
#
# :25 rather than :00 keeps it off the hour the FAST and NORMAL lanes share. Nothing breaks if they
# overlap — `publish_now` holds a lock and `rescore` is idempotent — but a sync competing with a
# collection for the same connection pool makes both slower for no reason.
GROUNDTRUTH_SCHEDULE = "25 */6 * * *"
GROUNDTRUTH_JOB_ID = "groundtruth-sync"

# Six hours between passes, so an hour late is still well within the cadence. A missed pass costs
# nothing permanent — the registers are read in full every time, not as a delta — but KEV is the
# strongest exploitation signal the site has and there is no reason to skip a reading of it.
GROUNDTRUTH_MISFIRE_GRACE_SECONDS = 3600


def job_id(lane: Lane) -> str:
    return f"lane-{lane.value}"


async def _run_lane_job(lane: Lane) -> None:
    """Run one lane and republish from it; either step failing leaves the schedule running.

    Publishing belongs here rather than only behind `--publish`. PLAN.md §11's failure model says
    that with Paperclip down the worker "keeps collecting and publishing", which it can only do if
    a scheduled run is what triggers a publish — otherwise the database advances every 15 minutes
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

    try:
        written = await publish_now()
    except Exception:
        # Deliberately not fatal. Collection is the irreplaceable half: a missed publish is
        # corrected by the next run 15 minutes later, whereas a lane that stops running loses
        # items that have already fallen off the end of their feed and cannot be re-fetched.
        logger.exception("publish after the %s lane failed; collection is unaffected", lane.value)
    else:
        logger.info("published %d files after the %s lane", len(written), lane.value)


async def _groundtruth_job() -> None:
    """Read the registers and republish if anything they said has changed.

    Failing here is never fatal to the schedule. `sync_groundtruth` already absorbs a register being
    down — that is the normal case it is designed for — so reaching the handler means something
    structural, and the next pass in six hours gets another go. Nothing is lost in the meantime: the
    registers are read in full each time rather than as a delta, so a skipped pass is a delay and
    not a gap.
    """
    try:
        summary = await sync_groundtruth()
    except Exception:
        logger.exception("scheduled ground-truth sync failed")
        return

    if not summary.changed_anything:
        logger.info("ground-truth sync changed nothing; not republishing")
        return
    try:
        written = await publish_now()
    except Exception:
        logger.exception("publish after the ground-truth sync failed; the sync itself stands")
    else:
        logger.info("published %d files after the ground-truth sync", len(written))


def build_scheduler(lanes: tuple[Lane, ...] | None = None) -> AsyncIOScheduler:
    """A configured, not yet started scheduler.

    `max_instances=1` and `coalesce=True` mean a run that outlasts its interval is not
    stacked on top of itself, and missed ticks collapse into one.

    The ground-truth sync is added only for the default schedule. `lanes` comes from `--lane`, which
    means "schedule this one thing", and silently bringing a register sync along with it would make
    the narrow form impossible to ask for.
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
    """Run the scheduler until SIGINT / SIGTERM."""
    scheduler = build_scheduler(lanes)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    scheduler.start()
    logger.info("scheduler started: %s", ", ".join(j.id for j in scheduler.get_jobs()))
    try:
        await stop.wait()
    finally:
        scheduler.shutdown(wait=False)
        logger.info("scheduler stopped")
