"""The watchdog's signatures (PLAN.md §11): what counts as a fault, judged from a snapshot of the
system (worker/db/watchdog.py gathers it) and nothing else.

A finding is a kind of fault and what it is wrong with, its subject: a lane, a source id, a job,
or "" for the system as a whole. There is one open incident per (kind, subject)
(worker/watchdog/reconcile.py). A part of the snapshot that could not be gathered is None, and
the kinds it would have shown are not covered that pass: their open incidents are left as they
are, not cleared because nothing was seen.

Titles and evidence are our own words and figures. A feed's last error is the only text from
outside, and the site already publishes it (source-health.json).

The thresholds were set against what VM 200 showed on 2026-10-03. A fast run brings about 90
items and a normal run about 320, nearly all duplicates. Fresh events are what volume is
judged on, because a new source's back catalogue arrives as thousands of old events at once.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from statistics import median
from typing import Any, Literal

from worker.db.digest import truncate, usd
from worker.db.jobs import Job, JobRun

Kind = Literal[
    "no-collection",
    "zero-volume",
    "feed-failing",
    "parser-drift",
    "stale-feed",
    "volume-collapse",
    "duplicate-explosion",
    "job-failing",
    "publish-failure",
    "schema-drift",
    "push-failure",
    "site-stale",
    "cost-anomaly",
    "paperclip-down",
]
Severity = Literal["low", "medium", "high", "critical"]
SEVERITIES: tuple[Severity, ...] = ("low", "medium", "high", "critical")
# Told to Telegram and to the Incident routine; the rest wait in GET /ops/incidents.
NOTIFY_AT: frozenset[str] = frozenset({"high", "critical"})
# Kinds whose subject is a source id.
SOURCE_KINDS: frozenset[str] = frozenset({"feed-failing", "parser-drift", "stale-feed"})

# A lane with no finished run for this long has stopped: two runs missed, and a little.
COLLECTION_LIMIT: dict[str, timedelta] = {
    "fast": timedelta(minutes=35),
    "normal": timedelta(hours=4, minutes=35),
}
COLLECTION_SEVERITY: dict[str, Severity] = {"fast": "critical", "normal": "high"}
# Runs in a row that fetched nothing at all, and the least time they must span between them.
ZERO_RUNS: dict[str, int] = {"fast": 3, "normal": 2}
ZERO_SPAN = timedelta(minutes=30)

# Checks in a row. Failing matches the five that degrade a source (worker/pipeline/health.py).
FAILING_AFTER = 5
EMPTY_AFTER = 3
STALE_AFTER = 3
# How many of a source's latest meaningful checks the snapshot holds.
SOURCE_HISTORY = 10
_FAILED = frozenset({"error", "timeout"})

# Fresh events in 24 hours below this share of the usual day, once there are enough usual days
# and they are not too few to judge.
COLLAPSE_SHARE = 0.25
COLLAPSE_MIN_DAYS = 3
COLLAPSE_MIN_MEDIAN = 8
# Fresh events in 6 hours above this many times a usual quarter-day, and never below the floor.
EXPLOSION_MULTIPLE = 5
EXPLOSION_FLOOR = 100
EXPLOSION_MIN_DAYS = 2

# A job with no completed pass for this long is failing.
JOB_LIMIT: dict[Job, timedelta] = {
    "groundtruth": timedelta(hours=13),
    "enrichment": timedelta(minutes=75),
    "discovery": timedelta(hours=50),
    "source-gate": timedelta(hours=9),
    "model-scan": timedelta(hours=50),
    "model-gauntlet": timedelta(days=15),
}
JOB_SEVERITY: dict[Job, Severity] = {"groundtruth": "high", "enrichment": "high"}
# The publish and push after each run: failing, and nothing completed for this long.
PUBLISH_LIMIT = timedelta(minutes=30)
PUSH_LIMIT = timedelta(minutes=45)
# Pages rebuilds from the data branch every 15 minutes, when GitHub runs the schedule on time.
SITE_LIMIT = timedelta(hours=3)
# Probes that fail this many times in a row count; fewer is a blip.
PROBE_FAILURES = 3

# AI spend in 24 hours above this many times a usual day, and never below the floor.
COST_MULTIPLE = 3
COST_FLOOR = Decimal("1.00")
BUDGET_SHARE = Decimal("0.9")

_MIN = timedelta(minutes=1)


@dataclass(frozen=True)
class Finding:
    kind: Kind
    subject: str
    severity: Severity
    title: str
    evidence: dict[str, Any] = field(default_factory=dict)

    @property
    def key(self) -> tuple[str, str]:
        return self.kind, self.subject


@dataclass(frozen=True)
class LaneRun:
    finished_at: datetime
    items_fetched: int


@dataclass(frozen=True)
class SourceState:
    source_id: str
    name: str
    lane: str
    priority: int
    host: str
    lifecycle: str | None
    statuses: tuple[str, ...]  # its latest meaningful checks, oldest first
    had_items: bool  # one of those checks fetched items
    last_error: str | None
    last_checked: datetime | None


@dataclass(frozen=True)
class Volume:
    fresh_24h: int
    fresh_6h: int
    usual: tuple[int, ...]  # fresh events in each earlier 24 hours, newest first


@dataclass(frozen=True)
class Cost:
    last_24h: Decimal
    usual: tuple[Decimal, ...]  # spend in each earlier 24 hours, newest first
    month_to_date: Decimal
    budget: Decimal


@dataclass(frozen=True)
class Jobs:
    latest: Mapping[str, JobRun]
    completed: Mapping[str, JobRun]


@dataclass(frozen=True)
class SiteReading:
    generated_at: datetime | None  # None: it could not be read
    error: str | None = None
    failures: int = 0  # reads in a row that failed


@dataclass(frozen=True)
class ProbeReading:
    failures: int  # checks in a row that got no answer
    error: str | None = None


@dataclass(frozen=True)
class Snapshot:
    now: datetime
    lanes: Mapping[str, Sequence[LaneRun]] | None = None  # newest first
    sources: Sequence[SourceState] | None = None
    volume: Volume | None = None
    jobs: Jobs | None = None
    cost: Cost | None = None
    site: SiteReading | None = None
    paperclip: ProbeReading | None = None


# Which kinds each part of the snapshot shows.
COVERS: dict[str, tuple[Kind, ...]] = {
    "lanes": ("no-collection", "zero-volume"),
    "sources": ("feed-failing", "parser-drift", "stale-feed"),
    "volume": ("volume-collapse", "duplicate-explosion"),
    "jobs": ("job-failing", "publish-failure", "schema-drift", "push-failure"),
    "cost": ("cost-anomaly",),
    "site": ("site-stale",),
    "paperclip": ("paperclip-down",),
}

# What each kind means and who fixes it, for TELETRAAN and WHEELJACK (GET /ops/incidents).
KIND_GUIDE: dict[Kind, str] = {
    "no-collection": (
        "A lane has stopped finishing runs. The worker or its scheduler is stuck or down. "
        "The board restarts the worker; a code fault behind it goes to WHEELJACK."
    ),
    "zero-volume": (
        "A lane's runs finish but fetch nothing from any source: the network, DNS or the "
        "collector itself. The board checks the VM's network first."
    ),
    "feed-failing": (
        "One feed keeps failing to answer. If its same-host siblings fail too, the publisher "
        "is down and there is nothing to fix. If only this feed fails, its URL may have moved: "
        "WHEELJACK proposes the new URL in config/sources, with the publisher's page as proof."
    ),
    "parser-drift": (
        "A feed answers but its parser finds no items, so the page or feed changed shape. "
        "WHEELJACK fixes the parser and adds a fixture of the new shape."
    ),
    "stale-feed": (
        "A feed answers with items, but the newest is older than its expected frequency. "
        "Usually the publisher is quiet; no fix unless it lasts."
    ),
    "volume-collapse": (
        "Far fewer fresh events than a usual day: sources failing quietly, or dedupe merging "
        "too much. SERAPH's source health and the merge log are the place to start."
    ),
    "duplicate-explosion": (
        "Far more fresh events than usual: dedupe or the normaliser has stopped matching "
        "reports of the same thing. WHEELJACK fixes the matching, with a test of the case."
    ),
    "job-failing": (
        "A scheduled job has not completed a pass within its limit. The worker's logs say why; "
        "the board decides, and a code fault goes to WHEELJACK."
    ),
    "publish-failure": (
        "Building the site's data keeps failing. The worker's logs say why; a code fault "
        "goes to WHEELJACK."
    ),
    "schema-drift": (
        "The site's data no longer passes its JSON schemas or the secret scan, so nothing is "
        "published (it fails closed). WHEELJACK fixes the builder or the schema, never the scan."
    ),
    "push-failure": (
        "Pushing the site's data to the data branch keeps failing: the publish token or "
        "GitHub. The board checks the token."
    ),
    "site-stale": (
        "The public site's data is old or cannot be read, though the worker publishes: the "
        "Pages build or GitHub. The board checks the Pages workflow."
    ),
    "cost-anomaly": (
        "AI spend is far above a usual day, or near the month's budget. The board checks the "
        "ledger (GET /ops/cost) and the agents' activity."
    ),
    "paperclip-down": (
        "Paperclip's server is not answering. Only a person can see to it; the crew cannot be told."
    ),
}


def span(delta: timedelta) -> str:
    """'47 minutes', '5 hours', '3 days'."""
    minutes = int(delta.total_seconds() // 60)
    if minutes < 120:
        return f"{minutes} minute{'' if minutes == 1 else 's'}"
    hours = minutes // 60
    if hours < 48:
        return f"{hours} hours"
    return f"{hours // 24} days"


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _trailing(statuses: Sequence[str], wanted: frozenset[str]) -> int:
    count = 0
    for status in reversed(statuses):
        if status not in wanted:
            break
        count += 1
    return count


# ─── Collection ───────────────────────────────────────────────────────────────────────────────────


def lane_findings(lanes: Mapping[str, Sequence[LaneRun]], now: datetime) -> list[Finding]:
    out = []
    for lane, runs in lanes.items():
        limit = COLLECTION_LIMIT.get(lane)
        if limit is None or not runs:
            continue  # unscheduled, or it has never run
        since = now - runs[0].finished_at
        if since > limit:
            out.append(
                Finding(
                    "no-collection",
                    lane,
                    COLLECTION_SEVERITY[lane],
                    f"The {lane} lane has not finished a run for {span(since)}",
                    {"last_finished": _iso(runs[0].finished_at), "limit_minutes": limit // _MIN},
                )
            )
            continue
        needed = ZERO_RUNS[lane]
        last = runs[:needed]
        if (
            len(last) == needed
            and all(r.items_fetched == 0 for r in last)
            and last[0].finished_at - last[-1].finished_at >= ZERO_SPAN
        ):
            out.append(
                Finding(
                    "zero-volume",
                    lane,
                    "high",
                    f"The {lane} lane's last {needed} runs fetched nothing",
                    {"runs": [_iso(r.finished_at) for r in last]},
                )
            )
    return out


# ─── Sources ──────────────────────────────────────────────────────────────────────────────────────


def source_findings(sources: Sequence[SourceState]) -> list[Finding]:
    by_host: dict[str, list[SourceState]] = {}
    for s in sources:
        by_host.setdefault(s.host, []).append(s)
    out = []
    for s in sources:
        base = {
            "source": s.source_id,
            "name": s.name,
            "lane": s.lane,
            "priority": s.priority,
            "lifecycle": s.lifecycle,
            "last_checked": _iso(s.last_checked),
            "last_error": truncate(s.last_error) if s.last_error else None,
            "recent_statuses": list(s.statuses),
        }
        failed = _trailing(s.statuses, _FAILED)
        if failed >= FAILING_AFTER:
            siblings = [
                {"source": o.source_id, "latest": o.statuses[-1] if o.statuses else None}
                for o in by_host.get(s.host, ())
                if o.source_id != s.source_id
            ]
            sick = sum(1 for o in siblings if o["latest"] in _FAILED)
            out.append(
                Finding(
                    "feed-failing",
                    s.source_id,
                    "high" if s.priority == 1 else "medium",
                    f"{s.name} has failed its last {failed} checks",
                    {
                        **base,
                        "failures_in_a_row": failed,
                        "host": s.host,
                        "same_host": siblings,
                        "same_host_failing": sick,
                    },
                )
            )
            continue
        empty = _trailing(s.statuses, frozenset({"empty"}))
        if empty >= EMPTY_AFTER:
            out.append(
                Finding(
                    "parser-drift",
                    s.source_id,
                    "high",
                    f"{s.name} answers, but its last {empty} checks found no items",
                    {**base, "empty_in_a_row": empty, "had_items_before": s.had_items},
                )
            )
            continue
        stale = _trailing(s.statuses, frozenset({"stale"}))
        if stale >= STALE_AFTER:
            out.append(
                Finding(
                    "stale-feed",
                    s.source_id,
                    "low",
                    f"{s.name}'s newest item has been older than expected for {stale} checks",
                    {**base, "stale_in_a_row": stale},
                )
            )
    return out


# ─── Volume ───────────────────────────────────────────────────────────────────────────────────────


def volume_findings(v: Volume) -> list[Finding]:
    out = []
    usual = median(v.usual) if v.usual else None
    evidence = {"fresh_24h": v.fresh_24h, "fresh_6h": v.fresh_6h, "usual_days": list(v.usual)}
    if (
        usual is not None
        and len(v.usual) >= COLLAPSE_MIN_DAYS
        and usual >= COLLAPSE_MIN_MEDIAN
        and v.fresh_24h < COLLAPSE_SHARE * usual
    ):
        out.append(
            Finding(
                "volume-collapse",
                "",
                "high",
                f"{v.fresh_24h} fresh events in 24 hours, against a usual {usual:g}",
                {**evidence, "usual_median": usual},
            )
        )
    if usual is not None and len(v.usual) >= EXPLOSION_MIN_DAYS:
        limit = max(EXPLOSION_FLOOR, EXPLOSION_MULTIPLE * usual / 4)
        if v.fresh_6h > limit:
            out.append(
                Finding(
                    "duplicate-explosion",
                    "",
                    "high",
                    f"{v.fresh_6h} fresh events in 6 hours, against a usual {usual:g} a day",
                    {**evidence, "usual_median": usual, "limit_6h": limit},
                )
            )
    return out


# ─── Jobs, publish and push ───────────────────────────────────────────────────────────────────────


def _job_evidence(latest: JobRun, done: JobRun | None) -> dict[str, Any]:
    return {
        "latest_finished": _iso(latest.finished_at),
        "latest_completed": latest.completed,
        "latest_note": latest.note,
        "latest_errors": latest.errors,
        "last_completed": _iso(done.finished_at) if done else None,
    }


def job_findings(jobs: Jobs, now: datetime) -> list[Finding]:
    out = []
    for job, limit in JOB_LIMIT.items():
        latest = jobs.latest.get(job)
        if latest is None:
            continue  # it has never run here
        done = jobs.completed.get(job)
        if done is not None and now - done.finished_at <= limit:
            continue
        title = (
            f"The {job} job has not completed a pass for {span(now - done.finished_at)}"
            if done
            else f"The {job} job has never completed a pass"
        )
        out.append(
            Finding(
                "job-failing",
                job,
                JOB_SEVERITY.get(job, "medium"),
                title,
                {**_job_evidence(latest, done), "limit_minutes": limit // _MIN},
            )
        )
    for job, limit in (("publish", PUBLISH_LIMIT), ("push", PUSH_LIMIT)):
        latest = jobs.latest.get(job)
        if latest is None or latest.completed:
            continue
        done = jobs.completed.get(job)
        if done is not None and now - done.finished_at <= limit:
            continue
        evidence = {**_job_evidence(latest, done), "limit_minutes": limit // _MIN}
        if job == "publish" and latest.note == "ValidationFailure":
            out.append(
                Finding(
                    "schema-drift",
                    "",
                    "high",
                    "The site's data fails its schemas or the secret scan, so nothing publishes",
                    evidence,
                )
            )
            continue
        since = f"for {span(now - done.finished_at)}" if done else "since it was first tried"
        verb = "Building" if job == "publish" else "Pushing"
        out.append(
            Finding(
                "publish-failure" if job == "publish" else "push-failure",
                "",
                "high",
                f"{verb} the site's data has failed {since}",
                evidence,
            )
        )
    return out


# ─── The site, the spend and Paperclip ────────────────────────────────────────────────────────────


def site_findings(site: SiteReading, now: datetime) -> list[Finding]:
    if site.generated_at is None:
        if site.failures < PROBE_FAILURES:
            return []
        return [
            Finding(
                "site-stale",
                "",
                "high",
                f"The public site's data could not be read {site.failures} times in a row",
                {"error": site.error, "failures_in_a_row": site.failures},
            )
        ]
    age = now - site.generated_at
    if age <= SITE_LIMIT:
        return []
    return [
        Finding(
            "site-stale",
            "",
            "high",
            f"The public site's data is {span(age)} old",
            {"generated_at": _iso(site.generated_at), "limit_minutes": SITE_LIMIT // _MIN},
        )
    ]


def cost_findings(c: Cost) -> list[Finding]:
    out = []
    usual = median(c.usual) if c.usual else None
    limit = max(COST_FLOOR, COST_MULTIPLE * usual) if usual is not None else COST_FLOOR
    if c.last_24h > limit:
        against = f", against a usual ${usual:.2f}" if usual is not None else ""
        out.append(
            Finding(
                "cost-anomaly",
                "rate",
                "high",
                f"AI spend in 24 hours is ${c.last_24h:.2f}{against}",
                {
                    "last_24h_usd": usd(c.last_24h),
                    "usual_days_usd": [usd(d) for d in c.usual],
                    "limit_usd": usd(limit),
                },
            )
        )
    if c.budget > 0 and c.month_to_date >= BUDGET_SHARE * c.budget:
        out.append(
            Finding(
                "cost-anomaly",
                "budget",
                "high",
                f"AI spend this month is ${c.month_to_date:.2f} of the ${c.budget:.2f} budget",
                {"month_to_date_usd": usd(c.month_to_date), "budget_usd": usd(c.budget)},
            )
        )
    return out


def paperclip_findings(p: ProbeReading) -> list[Finding]:
    if p.failures < PROBE_FAILURES:
        return []
    return [
        Finding(
            "paperclip-down",
            "",
            "high",
            f"Paperclip has not answered {p.failures} checks in a row",
            {"error": p.error, "failures_in_a_row": p.failures},
        )
    ]


def evaluate(s: Snapshot) -> tuple[list[Finding], frozenset[str]]:
    """Every fault the snapshot shows, and the kinds it could show at all."""
    findings: list[Finding] = []
    covered: set[str] = set()
    parts = (
        ("lanes", s.lanes, lambda p: lane_findings(p, s.now)),
        ("sources", s.sources, source_findings),
        ("volume", s.volume, volume_findings),
        ("jobs", s.jobs, lambda p: job_findings(p, s.now)),
        ("cost", s.cost, cost_findings),
        ("site", s.site, lambda p: site_findings(p, s.now)),
        ("paperclip", s.paperclip, paperclip_findings),
    )
    for name, part, check in parts:
        if part is None:
            continue
        covered.update(COVERS[name])
        findings += check(part)
    return findings, frozenset(covered)
