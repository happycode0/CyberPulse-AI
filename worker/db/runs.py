"""Pipeline run rows: written by the runner, read by the publisher."""

from collections.abc import Mapping
from datetime import datetime

from sqlalchemy import Connection, Engine, text

from worker.models import Lane, RunSummary

_COLUMNS = (
    "run_id, lane, started_at, finished_at, sources_ok, sources_failed, sources_stale, "
    "items_fetched, new_events, updated_events, duplicates, archived_events, errors"
)


def start_run(conn: Connection, run_id: str, lane: Lane, started_at: datetime) -> None:
    """Insert the run row before any work starts, so a crashed run leaves a trace with
    `finished_at` unset."""
    conn.execute(
        text("insert into runs (run_id, lane, started_at) values (:run_id, :lane, :started_at)"),
        {"run_id": run_id, "lane": lane.value, "started_at": started_at},
    )


def finish_run(conn: Connection, summary: RunSummary) -> None:
    conn.execute(
        text(
            "update runs set finished_at = :finished_at, sources_ok = :sources_ok, "
            "sources_failed = :sources_failed, sources_stale = :sources_stale, "
            "items_fetched = :items_fetched, new_events = :new_events, "
            "updated_events = :updated_events, duplicates = :duplicates, "
            "archived_events = :archived_events, errors = cast(:errors as text[]) "
            "where run_id = :run_id"
        ),
        summary.model_dump(exclude={"lane", "started_at"}),
    )


def load_run(bind: Engine | Connection, run_id: str) -> RunSummary | None:
    """One run by id, or None. Accepts an engine (opens a connection) or a connection."""
    def fetch(conn: Connection) -> RunSummary | None:
        row = (
            conn.execute(
                text(f"select {_COLUMNS} from runs where run_id = :run_id"), {"run_id": run_id}
            )
            .mappings()
            .first()
        )
        return RunSummary(**row) if row else None

    if isinstance(bind, Engine):
        with bind.connect() as conn:
            return fetch(conn)
    return fetch(bind)


def load_last_completed_collection(conn: Connection) -> datetime | None:
    """When the most recent finished run finished, or None if no run has completed."""
    return conn.execute(text("select max(finished_at) from runs")).scalar_one()


def load_latest_run(conn: Connection) -> RunSummary | None:
    """The most recently started run, finished or not."""
    row = (
        conn.execute(text(f"select {_COLUMNS} from runs order by started_at desc, run_id limit 1"))
        .mappings()
        .first()
    )
    return RunSummary(**row) if row else None


def load_latest_finished_run(conn: Connection) -> RunSummary | None:
    """The run that finished last, or None. Not `load_latest_run`: the FAST and NORMAL lanes start
    together, so the newest started is often still going, and its counters are all zero."""
    row = (
        conn.execute(
            text(
                f"select {_COLUMNS} from runs where finished_at is not null "
                "order by finished_at desc, run_id limit 1"
            )
        )
        .mappings()
        .first()
    )
    return RunSummary(**row) if row else None


def load_unfinished_runs(conn: Connection, *, since: datetime) -> list[RunSummary]:
    """Runs started since `since` that have not finished, newest first. An older unfinished row
    is a run that crashed, not one still going (`start_run`)."""
    rows = conn.execute(
        text(
            f"select {_COLUMNS} from runs where finished_at is null and started_at >= :since "
            "order by started_at desc, run_id"
        ),
        {"since": since},
    ).mappings()
    return [RunSummary(**r) for r in rows]


def load_lane_activity(conn: Connection) -> Mapping[str, tuple[datetime | None, datetime | None]]:
    """Per lane: when its newest run started, and when its newest finished run finished."""
    rows = conn.execute(
        text("select lane, max(started_at), max(finished_at) from runs group by lane")
    ).all()
    return {lane: (started, finished) for lane, started, finished in rows}
