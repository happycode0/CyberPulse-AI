"""What the routine wakes read beyond the follow-up queue (worker/notify/routines.py)."""

from datetime import datetime
from typing import NamedTuple

from sqlalchemy import Engine, text


class WaitingSites(NamedTuple):
    """Found sites still waiting for a feed whose home page the gate has read and found none in:
    all of them, and the ones read since `since`. A site not read yet is not counted: the gate
    may still find its feed itself."""

    tried: int
    new: int


def waiting_sites(engine: Engine, *, since: datetime) -> WaitingSites:
    with engine.connect() as conn:
        row = conn.execute(
            text(
                "select count(*) filter (where last_probe_at is not null) as tried, "
                "count(*) filter (where last_probe_at > :since) as new "
                "from source_candidates where state = 'discovered' and feed_url is null"
            ),
            {"since": since},
        ).one()
    return WaitingSites(row.tried, row.new)
