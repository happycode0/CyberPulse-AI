"""What the crew's jobs did, counted from the rows they leave (data/crew.json).

Since Paperclip was retired on 2026-10-31 every job runs in the worker, under its old agent's
callsign. SERAPH's is code: the collection, source checks, ground truth and publishing, counted
from the runs and job rows (`Activity`). MORPHEUS's daily report, DECKARD's follow-ups and
TELETRAAN's incident diagnosis are AI calls, counted from the cost ledger by stage
(`JobActivity`). The pipeline's own enrichment and MITRE calls are `PipelineAi`, and the owner
gets the month's ledger on Telegram.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy import Connection, text


@dataclass(frozen=True)
class Activity:
    tasks: int  # since the start of the month
    items: int | None
    last_active: datetime | None  # ever, not only this month
    failing: bool = False  # the newest attempt did not complete


@dataclass(frozen=True)
class JobActivity:
    """An AI job's calls this month, from the cost ledger, and the newest call ever: when, and
    on which model and provider. The defaults are a job that has never run."""

    calls: int = 0
    cost_usd: Decimal = Decimal(0)
    last_at: datetime | None = None
    model: str | None = None
    provider: str | None = None


@dataclass(frozen=True)
class PipelineAi:
    """The worker's own enrichment calls this month: triage, briefs, severity and MITRE
    suggestions (ledger stages `enrich.*`). The crew's jobs are `JobActivity`."""

    calls: int
    cost_usd: Decimal
    # The newest call ever, and who served it: a month without calls still says when AI last ran.
    last_call_at: datetime | None = None
    provider: str | None = None


def _one(conn: Connection, sql: str, since: datetime) -> dict:
    return dict(conn.execute(text(sql), {"since": since}).mappings().one())


def load_crew_activity(conn: Connection, *, since: datetime) -> dict[str, Activity]:
    """Activity per callsign (lower case): SERAPH's.

    Its tasks are the sources checked, the finished collection runs, and the completed
    ground-truth and publish passes, added together; its items are what the runs fetched. It
    is failing while the newest ground-truth pass did not complete.
    """
    seraph = _one(
        conn,
        "select (select count(*) from source_health where checked_at >= :since) as checks, "
        "       (select count(*) from runs where started_at >= :since "
        "        and finished_at is not null) as runs, "
        "       (select coalesce(sum(items_fetched), 0) from runs where started_at >= :since "
        "        and finished_at is not null) as items, "
        "       (select count(*) from job_runs where job in ('groundtruth', 'publish') "
        "        and completed and started_at >= :since) as passes, "
        "       (select not completed from job_runs where job = 'groundtruth' "
        "        order by finished_at desc, id desc limit 1) as failing, "
        "       greatest((select max(checked_at) from source_health), "
        "                (select max(finished_at) from runs), "
        "                (select max(finished_at) from job_runs "
        "                 where job in ('groundtruth', 'publish') and completed)) as last_active",
        since,
    )
    return {
        "seraph": Activity(
            seraph["checks"] + seraph["runs"] + seraph["passes"],
            int(seraph["items"]),
            seraph["last_active"],
            bool(seraph["failing"]),
        ),
    }


def load_pipeline_ai(conn: Connection, *, since: datetime) -> PipelineAi:
    row = _one(
        conn,
        "select count(*) filter (where ts >= :since) as calls, "
        "coalesce(sum(cost_usd) filter (where ts >= :since), 0) as cost, "
        "max(ts) as last_call_at, "
        "(array_agg(provider order by ts desc))[1] as provider "
        "from cost_ledger where coalesce(agent, 'worker') = 'worker' and stage like 'enrich.%'",
        since,
    )
    return PipelineAi(
        row["calls"], Decimal(row["cost"]), row["last_call_at"], row["provider"]
    )


def load_job_activity(
    conn: Connection, *, since: datetime, stages: Mapping[str, str]
) -> dict[str, JobActivity]:
    """`JobActivity` per callsign for `stages` (callsign -> ledger stage): the rows whose stage is
    that one or under it (`report.daily`, `report.daily.retry`), the worker's own."""
    out = {}
    for callsign, stage in stages.items():
        row = dict(
            conn.execute(
                text(
                    "select count(*) filter (where ts >= :since) as calls, "
                    "coalesce(sum(cost_usd) filter (where ts >= :since), 0) as cost, "
                    "max(ts) as last_at, "
                    "(array_agg(model order by ts desc))[1] as model, "
                    "(array_agg(provider order by ts desc))[1] as provider "
                    "from cost_ledger where coalesce(agent, 'worker') = 'worker' "
                    "and (stage = :stage or stage like :under)"
                ),
                {"since": since, "stage": stage, "under": f"{stage}.%"},
            ).mappings().one()
        )
        out[callsign] = JobActivity(
            row["calls"], Decimal(row["cost"]), row["last_at"], row["model"], row["provider"]
        )
    return out
