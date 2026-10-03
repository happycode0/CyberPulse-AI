"""Command line: `python -m worker [--check-models] [--check-budget] [--check-duplicates]
[--check-lineage] [--migrate] [--lane LANE [--once]] [--groundtruth] [--enrich] [--publish]`.

With no arguments the scheduler runs FAST, NORMAL, the ground-truth sync and AI enrichment until
interrupted. Explicit actions run in a fixed order (check models, check budget, check duplicates,
check lineage, migrate, collect, ground truth, enrich, publish) and then exit, unless `--lane` is
given without `--once`, which schedules that one lane instead.

The order is not arbitrary: the ground-truth sync writes the CVSS bands and KEV listings that
`urgency` is computed from, so running it before the publish is what gets a freshly looked-up
severity into the same set of files rather than the next one. Enrichment comes after it so a
severity judgment is only asked for where the registers still have no official score.
"""

import argparse
import asyncio
import logging
import sys
from collections import defaultdict
from collections.abc import Sequence
from datetime import UTC, datetime

import httpx

from worker.ai.budget import BudgetUnreadable, assess, fetch_key_status
from worker.ai.enrich import DEFAULT_BATCH, AiLayer, enrich_pending
from worker.ai.ladder import (
    OUTPUT_CEILING_USD_PER_MTOK,
    LadderRejected,
    LadderUnusable,
    verify_ladder,
)
from worker.ai.mitre import DEFAULT_MITRE_BATCH, suggest_techniques
from worker.db.lineage import load_reports
from worker.db.merge import load_live_ids, load_story_records, load_token_weights
from worker.db.migrate import run_migrations
from worker.db.session import get_engine
from worker.groundtruth.sync import DEFAULT_ADVISORY_BATCH, DEFAULT_CVSS_BATCH, sync_groundtruth
from worker.models import Lane
from worker.pipeline.correlate import after_merges, plan_merges, possible_duplicates
from worker.pipeline.lineage import Publishers, assign
from worker.pipeline.run import (
    CONSOLIDATE_LOOKBACK,
    DEFAULT_REGISTRY_PATH,
    WORD_WEIGHT_LOOKBACK,
    run_lane,
)
from worker.publish.build import LIVE_MIN_PROMINENCE
from worker.publish.run import publish_now
from worker.publish.validate import ValidationFailure
from worker.scheduler import SCHEDULE, serve
from worker.settings import get_settings
from worker.sources.registry import load_publishers, load_registry

logger = logging.getLogger("worker")

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2

# How many of the groups consolidation would merge `--check-duplicates` lists, and how many
# events `--check-lineage` does.
SHOWN_GROUPS = 40


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="worker", description="CyberPulse-AI worker")
    p.add_argument(
        "--check-models",
        action="store_true",
        help="check config/models.yaml against OpenRouter's live prices; non-zero if any model fails",
    )
    p.add_argument(
        "--check-budget",
        action="store_true",
        help="read the OpenRouter key's spend and show the mode it allows; spends nothing",
    )
    p.add_argument(
        "--check-duplicates",
        action="store_true",
        help="report the duplicate rate on the site and what consolidation would merge; reads only",
    )
    p.add_argument(
        "--check-lineage",
        action="store_true",
        help="count recent events' confirmations by outlet and by source lineage; reads only",
    )
    p.add_argument("--migrate", action="store_true", help="apply pending database migrations")
    p.add_argument("--lane", choices=[lane.value for lane in Lane], help="collection lane")
    p.add_argument(
        "--once",
        action="store_true",
        help="run --lane a single time and exit (default: schedule it)",
    )
    p.add_argument(
        "--groundtruth",
        action="store_true",
        help="read the KEV, EPSS, CVSS, OSV and MITRE registers once and record what they say",
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
    p.add_argument(
        "--advisory-batch",
        type=int,
        default=DEFAULT_ADVISORY_BATCH,
        metavar="N",
        help=(
            "how many CVEs --groundtruth looks up in OSV in this pass "
            f"(default {DEFAULT_ADVISORY_BATCH}; 0 skips the lookups)"
        ),
    )
    p.add_argument(
        "--enrich",
        action="store_true",
        help="run one AI enrichment pass over pending events, within the budget mode",
    )
    p.add_argument(
        "--enrich-batch",
        type=int,
        default=DEFAULT_BATCH,
        metavar="N",
        help=f"how many events --enrich takes in this pass (default {DEFAULT_BATCH})",
    )
    p.add_argument(
        "--mitre-batch",
        type=int,
        default=DEFAULT_MITRE_BATCH,
        metavar="N",
        help=(
            "how many enriched events --enrich suggests MITRE techniques for "
            f"(default {DEFAULT_MITRE_BATCH}; 0 skips them)"
        ),
    )
    p.add_argument("--publish", action="store_true", help="build and write the public JSON files")
    return p


def _check_models(*, required: bool) -> int:
    """Run the price-ceiling guard (worker/ai/ladder.py) and log what it found.

    `required` separates asking from starting. `--check-models` fails the command on a breach. The
    scheduler logs the same errors and starts anyway, because what a failed check switches off is
    the AI layer, and collection and publishing do not depend on it.
    """
    try:
        verified = asyncio.run(verify_ladder())
    except LadderUnusable as exc:
        problems = exc.breaches if isinstance(exc, LadderRejected) else [exc]
        for problem in problems:
            logger.error("model ladder: %s", problem)
        logger.error("the AI layer stays off until the model ladder passes")
        return EXIT_FAILED if required else EXIT_OK
    logger.info(
        "model ladder passed: %d models, each with a capable route at or under $%s/M output",
        len(verified.ladder.slugs()),
        OUTPUT_CEILING_USD_PER_MTOK,
    )
    return EXIT_OK


def _check_budget() -> int:
    """Read the key's spend from OpenRouter and log the mode it allows (worker/ai/budget.py).

    Costs nothing: `GET /api/v1/key` is a read. Non-zero only when there is no reading, because
    that is the case in which the worker would refuse paid calls for want of one.
    """
    settings = get_settings()
    key = settings.openrouter_api_key
    if key is None:
        logger.error("budget: OPENROUTER_API_KEY is not set, so the AI layer stays off")
        return EXIT_FAILED

    async def read():
        async with httpx.AsyncClient() as http:
            return await fetch_key_status(http, key, settings.user_agent)

    try:
        status = asyncio.run(read())
    except BudgetUnreadable as exc:
        logger.error("budget: %s; paid AI calls stay off until a reading succeeds", exc)
        return EXIT_FAILED
    reading = assess(status, settings.ai_monthly_budget_usd)
    limit = (
        f"${status.limit:.2f} ({status.limit_reset or 'never resets'}), "
        f"${status.limit_remaining:.2f} left"
        if status.limit is not None and status.limit_remaining is not None
        else "none"
    )
    logger.info(
        "budget: mode %s, %s; spent $%.4f today and $%.4f this month; key limit %s; "
        "free-model requests left today: %s",
        reading.mode.value,
        reading.reason,
        status.usage_daily,
        status.usage_monthly,
        limit,
        "unknown" if status.free_requests_remaining is None else status.free_requests_remaining,
    )
    return EXIT_OK


def _check_duplicates() -> int:
    """Measure the duplicate rate in published output (PLAN.md's KPI for the resolver), now and
    as it would be once consolidation merged what it would merge, and list those merges.

    Reads only: the connection's transaction is rolled back, and nothing is merged.
    """
    now = datetime.now(UTC)
    with get_engine().connect() as conn:
        records = load_story_records(conn, since=now - CONSOLIDATE_LOOKBACK)
        weights = load_token_weights(conn, since=now - WORD_WEIGHT_LOOKBACK)
        live = set(load_live_ids(conn, min_prominence=LIVE_MIN_PROMINENCE))
        conn.rollback()

    groups = plan_merges(records, weights)
    before = possible_duplicates(records, live, weights)
    losers = {e for g in groups for e in g.losers}
    live_after = (live - losers) | {g.winner for g in groups if live & {g.winner, *g.losers}}
    after = possible_duplicates(after_merges(records, groups), live_after, weights)
    titles = {r.event_id: r.title for r in records}

    logger.info(
        "duplicates: %d events on the site seen in the last %d days; %d (%.1f%%) are in a "
        "possible-duplicate pair",
        before.live,
        CONSOLIDATE_LOOKBACK.days,
        before.in_pairs,
        100 * before.rate,
    )
    logger.info(
        "duplicates: consolidation would merge %d events into %d (largest group %d); "
        "afterwards %d of %d (%.1f%%) would be in a pair",
        len(losers),
        len(groups),
        max((1 + len(g.losers) for g in groups), default=0),
        after.in_pairs,
        after.live,
        100 * after.rate,
    )
    for g in sorted(groups, key=lambda g: (-len(g.losers), g.winner))[:SHOWN_GROUPS]:
        logger.info(
            "  %s <- %s [%s] %s%s",
            g.winner,
            ", ".join(g.losers),
            ", ".join(g.methods),
            titles[g.winner][:90],
            f" (retitled: {g.title[:60]})" if g.title else "",
        )
        for e in g.losers:
            logger.info("      %s %s", e, titles[e][:90])
    for a, b, why, score in after.pairs[:SHOWN_GROUPS]:
        logger.info(
            "  left apart: %s / %s (%s %.2f): %s | %s",
            a,
            b,
            why,
            score,
            titles[a][:60],
            titles[b][:60],
        )
    return EXIT_OK


def _check_lineage() -> int:
    """Count each recent event's confirmations as one per outlet and as one per lineage
    (worker/pipeline/lineage.py), and list the events where the two differ: a publisher's
    several feeds, an outlet relaying an agency, a syndicated copy.

    Reads only: it says what the run's lineage pass makes of the events, and changes nothing.
    """
    now = datetime.now(UTC)
    with get_engine().connect() as conn:
        reports = load_reports(conn, since=now - CONSOLIDATE_LOOKBACK)
        conn.rollback()
    publishers = Publishers.from_registry(
        load_registry(DEFAULT_REGISTRY_PATH), load_publishers(DEFAULT_REGISTRY_PATH)
    )

    by_outlet = by_lineage = 0
    differ = []
    for event_id, rs in reports.items():
        assigned = assign(rs, publishers)
        outlets: dict[str, set[str]] = defaultdict(set)
        for r in rs:
            outlets[assigned[r.id].lineage].add(r.source_id)
        n = len({r.source_id for r in rs})
        by_outlet += n
        by_lineage += len(outlets)
        if n > len(outlets):
            differ.append((n - len(outlets), event_id, n, outlets, min(rs, key=lambda r: r.at)))

    logger.info(
        "lineage: %d events seen in the last %d days; %d confirmations counted by outlet, %d by "
        "lineage; %d events have fewer lineages than outlets",
        len(reports),
        CONSOLIDATE_LOOKBACK.days,
        by_outlet,
        by_lineage,
        len(differ),
    )
    for _, event_id, n, outlets, first in sorted(differ, key=lambda d: (-d[0], d[1]))[
        :SHOWN_GROUPS
    ]:
        shared = sorted((line, s) for line, s in outlets.items() if s != {line})
        logger.info(
            "  %s: %d outlets, %d lineages: %s | %s",
            event_id,
            n,
            len(outlets),
            "; ".join(f"{line} <- {', '.join(sorted(s))}" for line, s in shared),
            (first.title or "")[:70],
        )
    return EXIT_OK


def _groundtruth(cvss_batch: int, advisory_batch: int) -> int:
    """Run one sync. Only a failure the sync itself could not absorb is non-zero.

    Register errors land in `summary.errors` and are logged as warnings rather than failing the
    command, because "CISA was unreachable for ten minutes" is not a reason for a container to exit
    non-zero and be restarted into trying again immediately.
    """
    summary = asyncio.run(sync_groundtruth(cvss_batch=cvss_batch, advisory_batch=advisory_batch))
    for error in summary.errors:
        logger.warning("ground truth: %s", error)
    logger.info(
        "ground truth: kev=%s epss=%s cvss=%s advisories=%s mitre=%s severity_changed=%d "
        "rescored=%d",
        summary.kev,
        summary.epss,
        summary.cvss,
        summary.advisories,
        ", ".join(summary.mitre_loaded) or "unchanged",
        summary.severity_changed,
        summary.rescored,
    )
    return EXIT_OK


def _enrich(batch: int, mitre_batch: int) -> int:
    """Run one pass, then one pass of MITRE suggestions on the same ladder and budget reading.
    Like the ground-truth sync, only a failure a pass could not absorb is non-zero: a task that
    failed or waited for money is a normal outcome, not an error."""

    async def both():
        layer = AiLayer()
        summary = await enrich_pending(layer=layer, batch=batch)
        await suggest_techniques(layer=layer, batch=mitre_batch)
        return summary

    summary = asyncio.run(both())
    for error in summary.errors:
        logger.warning("enrichment: %s", error)
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

    if args.check_models:
        code = _check_models(required=True)
        if code != EXIT_OK:
            return code

    if args.check_budget:
        code = _check_budget()
        if code != EXIT_OK:
            return code

    if args.check_duplicates:
        code = _check_duplicates()
        if code != EXIT_OK:
            return code

    if args.check_lineage:
        code = _check_lineage()
        if code != EXIT_OK:
            return code

    if args.migrate:
        applied = run_migrations(get_engine())
        logger.info("migrations applied: %s", ", ".join(applied) or "none")

    if lane is not None and args.once:
        summary = asyncio.run(run_lane(lane, once=True))
        logger.info("run %s finished: %s", summary.run_id, summary.model_dump_json())

    if args.groundtruth:
        code = _groundtruth(args.cvss_batch, args.advisory_batch)
        if code != EXIT_OK:
            return code

    if args.enrich:
        code = _enrich(args.enrich_batch, args.mitre_batch)
        if code != EXIT_OK:
            return code

    if args.publish:
        code = _publish()
        if code != EXIT_OK:
            return code

    schedule = (lane is not None and not args.once) or not (
        args.check_models
        or args.check_budget
        or args.check_duplicates
        or args.check_lineage
        or args.migrate
        or args.publish
        or args.groundtruth
        or args.enrich
        or lane
    )
    if schedule:
        _check_models(required=False)
        asyncio.run(serve((lane,) if lane is not None else None))
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
