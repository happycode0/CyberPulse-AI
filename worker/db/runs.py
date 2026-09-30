"""Pipeline run reads for the publisher. Run rows are written by the pipeline runner."""

from datetime import datetime

from sqlalchemy import Connection, text

from worker.models import RunSummary


def load_last_completed_collection(conn: Connection) -> datetime | None:
    """When the most recent finished run finished, or None if no run has completed."""
    return conn.execute(text("select max(finished_at) from runs")).scalar_one()


def load_latest_run(conn: Connection) -> RunSummary | None:
    """The most recently started run, finished or not."""
    row = (
        conn.execute(
            text(
                "select run_id, lane, started_at, finished_at, sources_ok, sources_failed, "
                "sources_stale, items_fetched, new_events, updated_events, duplicates, "
                "archived_events, errors from runs order by started_at desc, run_id limit 1"
            )
        )
        .mappings()
        .first()
    )
    return RunSummary(**row) if row else None
