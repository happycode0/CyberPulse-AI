"""What the deterministic crew did, counted from the rows their jobs leave (data/crew.json).

Only agents whose work the worker itself does are counted here. The AI agents work in the
Paperclip control panel, which the worker never reads, so nothing is claimed for them.
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
    """Activity per callsign (lower case) for LIBRARIAN, PROWL, SERAPH and ROGUE."""
    librarian = _one(
        conn,
        "select (select count(*) from job_runs where job = 'groundtruth' and completed "
        "        and started_at >= :since) as tasks, "
        "       (select max(finished_at) from job_runs where job = 'groundtruth' and completed) "
        "        as last_active, "
        "       (select not completed from job_runs where job = 'groundtruth' "
        "        order by finished_at desc, id desc limit 1) as failing",
        since,
    )
    prowl = _one(
        conn,
        "select count(*) filter (where finished_at is not null) as tasks, "
        "       coalesce(sum(items_fetched) filter (where finished_at is not null), 0) as items, "
        "       (select max(finished_at) from runs) as last_active "
        "from runs where started_at >= :since",
        since,
    )
    seraph = _one(
        conn,
        "select count(*) as tasks, count(distinct source_id) as items, "
        "       (select max(checked_at) from source_health) as last_active "
        "from source_health where checked_at >= :since",
        since,
    )
    rogue = _one(
        conn,
        "select count(*) as tasks, (select max(ts) from cost_ledger) as last_active "
        "from cost_ledger where ts >= :since",
        since,
    )
    return {
        "librarian": Activity(
            librarian["tasks"], None, librarian["last_active"], bool(librarian["failing"])
        ),
        "prowl": Activity(prowl["tasks"], int(prowl["items"]), prowl["last_active"]),
        "seraph": Activity(seraph["tasks"], seraph["items"], seraph["last_active"]),
        "rogue": Activity(rogue["tasks"], None, rogue["last_active"]),
    }


def load_pipeline_ai(conn: Connection, *, since: datetime) -> PipelineAi:
    row = _one(
        conn,
        "select count(*) as calls, coalesce(sum(cost_usd), 0) as cost "
        "from cost_ledger where ts >= :since and coalesce(agent, 'worker') = 'worker'",
        since,
    )
    return PipelineAi(row["calls"], Decimal(row["cost"]))
