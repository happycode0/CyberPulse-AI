"""What the deterministic crew did, counted from the rows its jobs leave (data/crew.json).

Only an agent whose work the worker itself does is counted here: SERAPH, the Collector, whose
wake answers for the collection, source checks, ground truth and publishing. The AI agents work
in the Paperclip control panel, which the worker never reads, so nothing is claimed for them.
RIPPERDOC was counted from the cost ledger until it was retired on 2026-10-20; the worker's own
model calls are `PipelineAi`, and the owner gets the month's ledger on Telegram.
"""

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
class PipelineAi:
    """The worker's own model calls this month: enrichment and MITRE suggestions."""

    calls: int
    cost_usd: Decimal
    # The newest call ever, and who served it: a month without calls still says when AI last ran.
    last_call_at: datetime | None = None
    provider: str | None = None


@dataclass(frozen=True)
class Seen:
    """What the worker last received from an AI agent: its last piece of work the worker can see,
    and how many this month."""

    last_at: datetime | None
    this_month: int


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
        "from cost_ledger where coalesce(agent, 'worker') = 'worker'",
        since,
    )
    return PipelineAi(
        row["calls"], Decimal(row["cost"]), row["last_call_at"], row["provider"]
    )


def load_crew_seen(conn: Connection, *, since: datetime) -> dict[str, Seen]:
    """The crew's work that reaches the worker, per callsign (lower case): TACHIKOMA's source
    proposals, DECKARD's follow-up reports and TELETRAAN's verdicts on incident fixes. The other
    agents write only into Paperclip, which the worker never reads."""
    row = _one(
        conn,
        "select (select max(proposed_at) from source_candidates where found_by = 'tachikoma') "
        "       as tachikoma_at, "
        "       (select count(*) from source_candidates where found_by = 'tachikoma' "
        "        and proposed_at >= :since) as tachikoma_n, "
        "       (select max(updated_at) from followup_tasks where status = 'done' "
        "        and payload ? 'outcome') as deckard_at, "
        "       (select count(*) from followup_tasks where status = 'done' "
        "        and payload ? 'outcome' and updated_at >= :since) as deckard_n, "
        "       (select max(ts) from incident_verdicts) as teletraan_at, "
        "       (select count(*) from incident_verdicts where ts >= :since) as teletraan_n",
        since,
    )
    return {
        name: Seen(row[f"{name}_at"], row[f"{name}_n"])
        for name in ("tachikoma", "deckard", "teletraan")
    }
