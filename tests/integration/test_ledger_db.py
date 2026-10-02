"""The cost ledger against a real Postgres (migration 005, worker/db/ledger.py).

The constraints are the point: a missing cost stays null rather than becoming zero, every row
states its outcome, the tier is one the ladder has, and a sub-cent cost keeps its digits.
"""

from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from worker.db.ledger import LedgerEntry, record
from worker.db.migrate import run_migrations
from worker.version import (
    ENRICHMENT_VERSION,
    PIPELINE_VERSION,
    SCHEMA_VERSION,
    SCORING_VERSION,
)

EVENT = "evt-2026-000001"


@pytest.fixture
def conn(pg_engine):
    """A connection in a transaction that is always rolled back, with one event to point at."""
    run_migrations(pg_engine)
    with pg_engine.connect() as c:
        trans = c.begin()
        c.execute(text("delete from cost_ledger"))
        c.execute(text("delete from events"))
        c.execute(
            text(
                "insert into events (event_id, schema_version, pipeline_version, "
                "scoring_version, enrichment_version, first_seen, last_seen, title, summary) "
                "values (:id, :schema, :pipeline, :scoring, :enrichment, now(), now(), 't', 's')"
            ),
            {
                "id": EVENT,
                "schema": SCHEMA_VERSION,
                "pipeline": PIPELINE_VERSION,
                "scoring": SCORING_VERSION,
                "enrichment": ENRICHMENT_VERSION,
            },
        )
        try:
            yield c
        finally:
            trans.rollback()


def entry(**changes):
    base = {
        "provider": "openrouter",
        "stage": "classify",
        "outcome": "ok",
        "agent": "VOIGHT",
        "event_id": EVENT,
        "tier": "tier1_cheap",
        "requested_model": "openai/gpt-oss-20b",
        "model": "deepseek/deepseek-v4-flash-0731",
        "upstream": "SomeHost",
        "generation_id": "gen-123",
        "tokens_in": 210,
        "tokens_out": 18,
        "cost_usd": Decimal("0.0000273"),
        "duration_ms": 950,
    }
    base.update(changes)
    return LedgerEntry(**base)


def rows(conn):
    return conn.execute(text("select * from cost_ledger order by id")).mappings().all()


def test_an_entry_round_trips_with_its_sub_cent_cost(conn):
    record(conn, entry())
    [row] = rows(conn)
    assert row["cost_usd"] == Decimal("0.0000273")
    assert (row["agent"], row["stage"], row["event_id"], row["tier"]) == (
        "VOIGHT",
        "classify",
        EVENT,
        "tier1_cheap",
    )
    assert (row["requested_model"], row["model"], row["upstream"]) == (
        "openai/gpt-oss-20b",
        "deepseek/deepseek-v4-flash-0731",
        "SomeHost",
    )
    assert row["ts"] is not None and row["outcome"] == "ok"


def test_an_unreported_cost_stays_null(conn):
    record(conn, entry(cost_usd=None))
    assert rows(conn)[0]["cost_usd"] is None


def test_purpose_is_now_stage(conn):
    columns = {
        r[0]
        for r in conn.execute(
            text(
                "select column_name from information_schema.columns "
                "where table_name = 'cost_ledger'"
            )
        )
    }
    assert "stage" in columns and "purpose" not in columns


def test_every_row_must_state_its_outcome(conn):
    with pytest.raises(IntegrityError):
        conn.execute(text("insert into cost_ledger (provider) values ('openrouter')"))


@pytest.mark.parametrize(
    "changes",
    [{"outcome": "maybe"}, {"tier": "tier3_premium"}, {"duration_ms": -1}],
)
def test_values_outside_the_vocabulary_are_refused(conn, changes):
    with pytest.raises(IntegrityError):
        record(conn, entry(**changes))


def test_spend_outlives_the_event_it_was_for(conn):
    record(conn, entry())
    conn.execute(text("delete from events where event_id = :id"), {"id": EVENT})
    [row] = rows(conn)
    assert row["event_id"] is None and row["cost_usd"] == Decimal("0.0000273")


def test_spend_that_is_not_a_model_call_needs_no_tier(conn):
    record(conn, LedgerEntry(provider="tavily", stage="search", outcome="ok"))
    [row] = rows(conn)
    assert row["tier"] is None and row["cost_usd"] is None
