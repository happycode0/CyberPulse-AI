"""Command line: `python -m worker [--migrate] [--lane LANE [--once]] [--groundtruth] [--publish]`.

With no arguments the scheduler runs FAST, NORMAL and the ground-truth sync until interrupted.
Explicit actions run in a fixed order (migrate, collect, ground truth, publish) and then exit,
unless `--lane` is given without `--once`, which schedules that one lane instead.

The order is not arbitrary: the ground-truth sync writes the CVSS bands and KEV listings that
`urgency` is computed from, so running it before the publish is what gets a freshly looked-up
severity into the same set of files rather than the next one.
"""

import argparse
import asyncio
import logging
import sys
from collections.abc import Sequence

from worker.db.migrate import run_migrations
from worker.db.session import get_engine
from worker.groundtruth.sync import DEFAULT_CVSS_BATCH, sync_groundtruth
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
    p.add_argument(
        "--groundtruth",
        action="store_true",
        help="read the KEV, EPSS and CVSS registers once and record what they say",
    )
    p.add_argument(
        "--cvss-batch",
        type=int,
        default=DEFAULT_CVSS_BATCH,
        metavar="N",
        help=(
            "how many CVEs --groundtruth resolves CVSS for in this pass "
            f"(default {DEFAULT_CVSS_BATCH}; 0 reads only the bulk registers)"
        ),
    )
    p.add_argument("--publish", action="store_true", help="build and write the public JSON files")
    return p


def _groundtruth(cvss_batch: int) -> int:
    """Run one sync. Only a failure the sync itself could not absorb is non-zero.

    Register errors land in `summary.errors` and are logged as warnings rather than failing the
    command, because "CISA was unreachable for ten minutes" is not a reason for a container to exit
    non-zero and be restarted into trying again immediately.
    """
    summary = asyncio.run(sync_groundtruth(cvss_batch=cvss_batch))
    for error in summary.errors:
        logger.warning("ground truth: %s", error)
    logger.info(
        "ground truth: kev=%s epss=%s cvss=%s severity_changed=%d rescored=%d",
        summary.kev,
        summary.epss,
        summary.cvss,
        summary.severity_changed,
        summary.rescored,
    )
    return EXIT_OK


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

    if args.groundtruth:
        code = _groundtruth(args.cvss_batch)
        if code != EXIT_OK:
            return code

    if args.publish:
        code = _publish()
        if code != EXIT_OK:
            return code

    schedule = (lane is not None and not args.once) or not (
        args.migrate or args.publish or args.groundtruth or lane
    )
    if schedule:
        asyncio.run(serve((lane,) if lane is not None else None))
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
