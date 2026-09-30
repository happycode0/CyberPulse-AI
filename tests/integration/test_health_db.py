from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from worker.db.migrate import run_migrations
from worker.db.sources import (
    load_health_history,
    load_lifecycle_states,
    record_health,
    set_lifecycle_state,
)
from worker.models import SourceHealth
from worker.pipeline.health import HealthStatus, LifecycleState

NOW = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)
SOURCE = "acsc-alerts"


@pytest.fixture
def conn(pg_engine):
    """A connection inside a transaction that is always rolled back."""
    run_migrations(pg_engine)
    with pg_engine.connect() as c:
        trans = c.begin()
        c.execute(text("delete from source_health"))
        c.execute(
            text(
                "insert into source_registry (id, name, type, region, category, "
                "source_class, priority, lane, enabled, url, parser, expected_frequency) "
                "values (:id, :id, 'rss', 'AU', 'advisory', 'AUTHORITATIVE', 1, 'fast', "
                "true, 'https://example.org/feed', 'rss', 'daily') on conflict do nothing"
            ),
            {"id": SOURCE},
        )
        try:
            yield c
        finally:
            trans.rollback()


def make_health(status=HealthStatus.OK, *, at=NOW, age=0.5, items=12, duration=240, error=None):
    return SourceHealth(
        source_id=SOURCE, checked_at=at, status=status, error=error,
        newest_item_age_days=age, items_fetched=items, duration_ms=duration,
    )


def test_record_health_round_trips_every_field(conn):
    h = make_health(HealthStatus.STALE, age=120.0, error="newest item 120 days old")
    record_health(conn, h)
    assert load_health_history(conn, SOURCE) == [h]


def test_unknown_age_round_trips_as_none(conn):
    record_health(conn, make_health(age=None))
    assert load_health_history(conn, SOURCE)[0].newest_item_age_days is None


def test_history_is_chronological_and_limited_to_the_most_recent(conn):
    for i in range(6):
        record_health(conn, make_health(at=NOW - timedelta(minutes=15 * (5 - i)), items=i))
    history = load_health_history(conn, SOURCE, limit=4)
    assert [h.items_fetched for h in history] == [2, 3, 4, 5]


def test_history_is_per_source(conn):
    conn.execute(
        text(
            "insert into source_registry (id, name, type, region, category, source_class, "
            "priority, lane, enabled, url, parser, expected_frequency) values "
            "('other', 'other', 'rss', 'AU', 'advisory', 'NEWS', 1, 'fast', true, "
            "'https://example.org/o', 'rss', 'daily')"
        )
    )
    record_health(conn, make_health())
    assert load_health_history(conn, "other") == []


def test_lifecycle_state_is_persisted(conn):
    set_lifecycle_state(conn, SOURCE, LifecycleState.DEGRADED)
    value = conn.execute(
        text("select lifecycle_state from source_registry where id = :id"), {"id": SOURCE}
    ).scalar_one()
    assert value == "degraded"


def test_load_lifecycle_states_reads_back_only_rows_with_a_state(conn):
    conn.execute(
        text(
            "insert into source_registry (id, name, type, region, category, source_class, "
            "priority, lane, enabled, url, parser, expected_frequency) values "
            "('unset', 'unset', 'rss', 'AU', 'advisory', 'NEWS', 1, 'fast', true, "
            "'https://example.org/u', 'rss', 'daily')"
        )
    )
    set_lifecycle_state(conn, SOURCE, LifecycleState.TESTING)
    states = load_lifecycle_states(conn)
    assert states[SOURCE] is LifecycleState.TESTING
    assert "unset" not in states


def test_unknown_lifecycle_state_is_rejected_by_the_database(conn):
    with pytest.raises(Exception):
        conn.execute(
            text("update source_registry set lifecycle_state = 'bogus' where id = :id"),
            {"id": SOURCE},
        )
