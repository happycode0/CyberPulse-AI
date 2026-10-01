"""Command line: `python -m worker [--migrate] [--lane LANE [--once]] [--publish]`.

With no arguments the scheduler runs FAST and NORMAL until interrupted. Explicit actions run
in a fixed order (migrate, collect, publish) and then exit, unless `--lane` is given without
`--once`, which schedules that one lane instead.
"""

import argparse
import asyncio
import logging
import sys
from collections.abc import Sequence

from worker.db.migrate import run_migrations
from worker.db.session import get_engine
from worker.models import Lane
from worker.pipeline.run import run_lane
from worker.publish.run import publish_now
from worker.publish.validate import ValidationFailure
from worker.scheduler import SCHEDULE, serve

logger = logging.getLogger("worker")

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="worker", description="CyberPulse-AI worker")
    p.add_argument("--migrate", action="store_true", help="apply pending database migrations")
    p.add_argument("--lane", choices=[lane.value for lane in Lane], help="collection lane")
    p.add_argument(
        "--once", action="store_true", help="run --lane a single time and exit (default: schedule it)"
    )
    p.add_argument("--publish", action="store_true", help="build and write the public JSON files")
    return p


def _publish() -> int:
    try:
        written = asyncio.run(publish_now())
    except ValidationFailure as exc:
        logger.error("publish blocked, nothing written: %s", exc)
        return EXIT_FAILED
    logger.info("published %d files", len(written))
    return EXIT_OK


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    parser = build_parser()
    args = parser.parse_args(argv)
    lane = Lane(args.lane) if args.lane else None

    if args.once and lane is None:
        parser.error("--once needs --lane")
    if lane is not None and not args.once and lane not in SCHEDULE:
        parser.error(f"the {lane.value} lane has no schedule; use --lane {lane.value} --once")

    if args.migrate:
        applied = run_migrations(get_engine())
        logger.info("migrations applied: %s", ", ".join(applied) or "none")

    if lane is not None and args.once:
        summary = asyncio.run(run_lane(lane, once=True))
        logger.info("run %s finished: %s", summary.run_id, summary.model_dump_json())

    if args.publish:
        code = _publish()
        if code != EXIT_OK:
            return code

    schedule = (lane is not None and not args.once) or not (args.migrate or args.publish or lane)
    if schedule:
        asyncio.run(serve((lane,) if lane is not None else None))
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
