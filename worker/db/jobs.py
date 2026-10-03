"""job_runs rows: one per ground-truth or enrichment pass, written when it ends (migration 010)."""

from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Literal

from sqlalchemy import Connection, Engine, text

Job = Literal["groundtruth", "enrichment"]


@dataclass(frozen=True)
class JobRun:
    job: Job
    started_at: datetime
    finished_at: datetime
    completed: bool  # False: the pass raised
    errors: int = 0  # failures it absorbed
    changed: bool = False


_COLUMNS = ("job", "started_at", "finished_at", "completed", "errors", "changed")


def record_job(engine: Engine, run: JobRun) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                f"insert into job_runs ({', '.join(_COLUMNS)}) "
                f"values ({', '.join(':' + c for c in _COLUMNS)})"
            ),
            asdict(run),
        )


def load_latest_job(conn: Connection, job: Job) -> JobRun | None:
    """The newest pass of `job` to finish, or None if it has never run."""
    row = (
        conn.execute(
            text(
                f"select {', '.join(_COLUMNS)} from job_runs where job = :job "
                "order by finished_at desc, id desc limit 1"
            ),
            {"job": job},
        )
        .mappings()
        .first()
    )
    return JobRun(**row) if row else None


def load_latest_completed_job(conn: Connection, job: Job) -> JobRun | None:
    """The newest pass of `job` that finished without raising, or None."""
    row = (
        conn.execute(
            text(
                f"select {', '.join(_COLUMNS)} from job_runs where job = :job and completed "
                "order by finished_at desc, id desc limit 1"
            ),
            {"job": job},
        )
        .mappings()
        .first()
    )
    return JobRun(**row) if row else None
