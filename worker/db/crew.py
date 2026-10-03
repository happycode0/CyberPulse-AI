"""What the deterministic crew did, counted from the rows their jobs leave (data/crew.json).

Only agents whose work the worker itself does are counted here: SERAPH, the Collector, whose
wake answers for the collection, source checks, ground truth and publishing, and RIPPERDOC's
cost ledger (ROGUE's, before the 8-agent crew). The AI agents work in the Paperclip control
panel, which the worker never reads, so nothing is claimed for them.
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


def _one(conn: Connection, sql: str, since: datetime) -> dict:
    return dict(conn.execute(text(sql), {"since": since}).mappings().one())


def load_crew_activity(conn: Connection, *, since: datetime) -> dict[str, Activity]:
    """Activity per callsign (lower case) for SERAPH and RIPPERDOC.

    SERAPH's tasks are the sources checked, the finished collection runs, and the completed
    ground-truth and publish passes, added together; its items are what the runs fetched. It
    is failing while the newest ground-truth pass did not complete. RIPPERDOC's tasks are the
    ledger's rows: every AI call the worker priced.
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
    ripperdoc = _one(
        conn,
        "select count(*) as tasks, (select max(ts) from cost_ledger) as last_active "
        "from cost_ledger where ts >= :since",
        since,
    )
    return {
        "seraph": Activity(
            seraph["checks"] + seraph["runs"] + seraph["passes"],
            int(seraph["items"]),
            seraph["last_active"],
            bool(seraph["failing"]),
        ),
        "ripperdoc": Activity(ripperdoc["tasks"], None, ripperdoc["last_active"]),
    }


def load_pipeline_ai(conn: Connection, *, since: datetime) -> PipelineAi:
    row = _one(
        conn,
        "select count(*) as calls, coalesce(sum(cost_usd), 0) as cost "
        "from cost_ledger where ts >= :since and coalesce(agent, 'worker') = 'worker'",
        since,
    )
    return PipelineAi(row["calls"], Decimal(row["cost"]))
