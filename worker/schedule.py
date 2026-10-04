"""What the worker runs, and when: every scheduled job's id, cron line, timezone and what it does.

worker/scheduler.py adds these jobs and nothing else, and worker/publish/build.py publishes them
in system-status.json (`public_schedule`), so the site's schedule is the one that runs. Pure: it
imports no job, so the publisher can read it without the scheduler.

`kind` says what does the work: `code` is rules and registers alone, `ai` calls a model, and
`mixed` is code that may call one. Nothing here is mixed today: enrichment and the gauntlet call
models, and the model scan reads OpenRouter's model list and calls none.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from worker.cadence import FAST_INTERVAL_MINUTES, fast_minutes
from worker.models import Lane


class JobKind(StrEnum):
    CODE = "code"
    AI = "ai"
    MIXED = "mixed"


@dataclass(frozen=True)
class Job:
    id: str
    label: str
    cron: str
    timezone: str
    kind: JobKind
    # One plain sentence for the site, which says what the job does and not how it is wired.
    summary: str
    misfire_grace_seconds: int

    def public(self) -> dict[str, Any]:
        """system-status.json's `schedule` entry."""
        return {
            "job": self.id,
            "label": self.label,
            "cron": self.cron,
            "timezone": self.timezone,
            "kind": self.kind.value,
            "summary": self.summary,
        }


def _minute_list(minutes: Iterable[int]) -> str:
    return ",".join(str(m) for m in sorted(minutes))


UTC_ZONE = "UTC"

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


def job_id(lane: Lane) -> str:
    return f"lane-{lane.value}"


# The jobs only the default schedule runs, in the order the scheduler adds them; `--lane` runs
# its lanes alone (worker/scheduler.py `build_scheduler`).
JOBS: tuple[Job, ...] = (
    Job(
        GROUNDTRUTH_JOB_ID, "Ground-truth sync", GROUNDTRUTH_SCHEDULE, UTC_ZONE, JobKind.CODE,
        "Reads the public registers (CISA KEV, EPSS, CVE scores, OSV advisories and MITRE "
        "ATT&CK) and rescores and republishes any event whose facts changed.",
        GROUNDTRUTH_MISFIRE_GRACE_SECONDS,
    ),
    Job(
        ENRICH_JOB_ID, "AI enrichment", ENRICH_SCHEDULE, UTC_ZONE, JobKind.AI,
        "Asks a model to triage, brief and estimate the severity of a small batch of new "
        "events, and to suggest MITRE techniques, within the month's budget; an event it has "
        "not reached is published without them.",
        ENRICH_MISFIRE_GRACE_SECONDS,
    ),
    Job(
        DIGEST_JOB_ID, "Daily digest", DIGEST_SCHEDULE, DIGEST_TIMEZONE, JobKind.CODE,
        "Sends the morning digest to Telegram at 07:10 Sydney time; 08:10 and 09:10 send it "
        "only if 07:10 did not.",
        DIGEST_MISFIRE_GRACE_SECONDS,
    ),
    Job(
        ALERT_JOB_ID, "Critical alerts", ALERT_SCHEDULE, UTC_ZONE, JobKind.CODE,
        "After each collection and enrichment pass, sends Telegram an alert for a new critical "
        "or actively exploited event that matters to Australia, and updates on developing ones.",
        ALERT_MISFIRE_GRACE_SECONDS,
    ),
    Job(
        DISCOVERY_JOB_ID, "Discovery search", DISCOVERY_SCHEDULE, DIGEST_TIMEZONE, JobKind.CODE,
        "Searches the web for feeds worth adding and queues each new one as a candidate; it "
        "adds no source itself.",
        DISCOVERY_MISFIRE_GRACE_SECONDS,
    ),
    Job(
        GATE_JOB_ID, "Source gate", GATE_SCHEDULE, UTC_ZONE, JobKind.CODE,
        "Probes each candidate feed; one that passes enough healthy probes in a row becomes a "
        "source, and the next normal run collects it.",
        GATE_MISFIRE_GRACE_SECONDS,
    ),
    Job(
        MODEL_SCAN_JOB_ID, "Model scan", MODEL_SCAN_SCHEDULE, DIGEST_TIMEZONE, JobKind.CODE,
        "Reads OpenRouter's public model list and checks the enrichment models against the "
        "price and capability rules, dropping any that fail; it calls no model.",
        MODEL_SCAN_MISFIRE_GRACE_SECONDS,
    ),
    Job(
        GAUNTLET_JOB_ID, "Model gauntlet", GAUNTLET_SCHEDULE, DIGEST_TIMEZONE, JobKind.AI,
        "Runs the current enrichment models and up to two challengers on a labelled test set; "
        "a better challenger is proposed for review, never switched to.",
        GAUNTLET_MISFIRE_GRACE_SECONDS,
    ),
    Job(
        WATCHDOG_JOB_ID, "Watchdog", WATCHDOG_SCHEDULE, UTC_ZONE, JobKind.CODE,
        "Checks that the site, the collections and the other jobs are keeping up, and opens an "
        "incident, and tells the crew, when one is not.",
        WATCHDOG_MISFIRE_GRACE_SECONDS,
    ),
)

_LANE_SUMMARIES = {
    Lane.FAST: "Collects the fast-lane sources (government advisories, the KEV list and the "
    "fastest news feeds), merges and scores what is new by rule, and republishes the site's data.",
    Lane.NORMAL: "Collects the normal-lane sources, merges and scores what is new by rule, and "
    "republishes the site's data.",
}

# DEEP has no schedule: it is a Paperclip routine (worker/scheduler.py).
LANE_JOBS: dict[Lane, Job] = {
    lane: Job(
        job_id(lane), f"{lane.value.capitalize()} lane", SCHEDULE[lane], UTC_ZONE, JobKind.CODE,
        _LANE_SUMMARIES[lane], MISFIRE_GRACE_SECONDS[lane],
    )
    for lane in SCHEDULE
}


def public_schedule() -> list[dict[str, Any]]:
    """The default schedule as system-status.json publishes it: the lanes first, then the rest
    in the order they are added."""
    return [job.public() for job in (*LANE_JOBS.values(), *JOBS)]
