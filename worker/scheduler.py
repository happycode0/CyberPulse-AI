"""Lane scheduling. FAST and NORMAL run in the worker; DEEP is a Paperclip routine, so it has
no schedule here and is only reachable through `--lane deep --once`."""

import asyncio
import logging
import signal

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from worker.models import Lane
from worker.pipeline import run as pipeline_run

logger = logging.getLogger(__name__)

SCHEDULE: dict[Lane, str] = {
    Lane.FAST: "*/15 * * * *",
    Lane.NORMAL: "0 */4 * * *",
}

# A run that started late (the loop was busy, or the process was paused) is still worth
# running if it is less than one cadence overdue.
MISFIRE_GRACE_SECONDS = {Lane.FAST: 600, Lane.NORMAL: 1800}


def job_id(lane: Lane) -> str:
    return f"lane-{lane.value}"


async def _run_lane_job(lane: Lane) -> None:
    """Run one lane; a failed run is logged and the schedule carries on."""
    try:
        await pipeline_run.run_lane(lane)
    except Exception:
        logger.exception("scheduled %s run failed", lane.value)


def build_scheduler(lanes: tuple[Lane, ...] | None = None) -> AsyncIOScheduler:
    """A configured, not yet started scheduler.

    `max_instances=1` and `coalesce=True` mean a run that outlasts its interval is not
    stacked on top of itself, and missed ticks collapse into one.
    """
    scheduler = AsyncIOScheduler(timezone="UTC")
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
