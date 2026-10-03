"""worker/db/crew.py against a real Postgres: the counters behind data/crew.json."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import text

from worker.db.crew import load_crew_activity, load_pipeline_ai
from worker.db.jobs import JobRun, record_job
from worker.db.migrate import run_migrations

NOW = datetime(2026, 10, 3, 8, 0, tzinfo=UTC)
MONTH = datetime(2026, 10, 1, tzinfo=UTC)
TABLES = (
    "job_runs", "cost_ledger", "events", "source_health", "source_fetch_state", "runs",
    "source_lineage", "source_registry",
)


def clear(engine) -> None:
    with engine.begin() as conn:
        for table in TABLES:
            conn.execute(text(f"delete from {table}"))


@pytest.fixture
def db(pg_engine):
    run_migrations(pg_engine)
    clear(pg_engine)
    with pg_engine.begin() as conn:
        conn.execute(
            text(
                "insert into source_registry (id, name, type, region, category, source_class, "
                "priority, lane, enabled, url, parser, expected_frequency) values "
                "('crew-a', 'a', 'rss', 'AU', 'advisory', 'AUTHORITATIVE', 1, 'fast', true, "
                "'https://example.org/a', 'feed', 'daily'), "
                "('crew-b', 'b', 'rss', 'AU', 'advisory', 'AUTHORITATIVE', 1, 'fast', true, "
                "'https://example.org/b', 'feed', 'daily')"
            )
        )
    yield pg_engine
    clear(pg_engine)


def test_an_empty_database_counts_nothing(db):
    with db.connect() as conn:
        activity = load_crew_activity(conn, since=MONTH)
        ai = load_pipeline_ai(conn, since=MONTH)
    assert {k: (a.tasks, a.last_active, a.failing) for k, a in activity.items()} == {
        "librarian": (0, None, False),
        "prowl": (0, None, False),
        "seraph": (0, None, False),
        "rogue": (0, None, False),
    }
    assert (ai.calls, ai.cost_usd) == (0, Decimal(0))


def test_each_agent_is_counted_from_its_own_rows(db):
    last_month = MONTH - timedelta(days=2)
    record_job(db, JobRun("groundtruth", last_month, last_month, completed=True))
    record_job(db, JobRun("groundtruth", NOW - timedelta(hours=7), NOW - timedelta(hours=6),
                          completed=True))
    record_job(db, JobRun("groundtruth", NOW - timedelta(hours=1), NOW, completed=False))
    with db.begin() as conn:
        conn.execute(
            text(
                "insert into runs (run_id, lane, started_at, finished_at, items_fetched) values "
                "('r-old', 'fast', :old, :old, 500), "
                "('r-1', 'fast', :a, :a, 40), ('r-2', 'normal', :b, :b, 60), "
                "('r-3', 'fast', :c, null, 0)"
            ),
            {"old": last_month, "a": NOW - timedelta(hours=2), "b": NOW - timedelta(hours=1),
             "c": NOW},
        )
        conn.execute(
            text(
                "insert into source_health (source_id, checked_at, status) values "
                "('crew-a', :a, 'ok'), ('crew-a', :b, 'ok'), ('crew-b', :b, 'error'), "
                "('crew-b', :old, 'ok')"
            ),
            {"a": NOW - timedelta(hours=2), "b": NOW - timedelta(minutes=5), "old": last_month},
        )
        conn.execute(
            text(
                "insert into cost_ledger (ts, provider, model, stage, agent, cost_usd, outcome) "
                "values (:a, 'openrouter', 'm', 'enrich.brief', 'worker', 0.01, 'ok'), "
                "(:b, 'openrouter', 'm', 'enrich.brief', null, null, 'invalid_output'), "
                "(:b, 'openrouter', 'm', 'gauntlet', 'ripperdoc', 0.5, 'ok'), "
                "(:old, 'openrouter', 'm', 'enrich.brief', 'worker', 9, 'ok')"
            ),
            {"a": NOW - timedelta(hours=3), "b": NOW - timedelta(hours=1), "old": last_month},
        )

    with db.connect() as conn:
        activity = load_crew_activity(conn, since=MONTH)
        ai = load_pipeline_ai(conn, since=MONTH)

    librarian = activity["librarian"]
    assert (librarian.tasks, librarian.last_active, librarian.failing) == (
        1, NOW - timedelta(hours=6), True
    )
    prowl = activity["prowl"]
    assert (prowl.tasks, prowl.items, prowl.last_active) == (2, 100, NOW - timedelta(hours=1))
    seraph = activity["seraph"]
    assert (seraph.tasks, seraph.items, seraph.last_active) == (
        3, 2, NOW - timedelta(minutes=5)
    )
    assert (activity["rogue"].tasks, activity["rogue"].last_active) == (
        3, NOW - timedelta(hours=1)
    )
    # Only the worker's own calls: RIPPERDOC's gauntlet is not the pipeline's spend.
    assert (ai.calls, ai.cost_usd) == (2, Decimal("0.01"))
