"""`load_corroboration` (worker/db/sources.py), the database half of a source's reputation."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from worker.db.migrate import run_migrations
from worker.db.sources import load_corroboration
from worker.pipeline.reputation import CORROBORATION_WINDOW, Corroboration
from worker.version import (
    ENRICHMENT_VERSION,
    PIPELINE_VERSION,
    SCHEMA_VERSION,
    SCORING_VERSION,
)

NOW = datetime(2026, 10, 4, 12, 0, tzinfo=UTC)
SINCE = NOW - CORROBORATION_WINDOW
SOURCES = ("abc", "abc-politics", "thn", "wire")


@pytest.fixture
def conn(pg_engine):
    """A connection inside a transaction that is always rolled back."""
    run_migrations(pg_engine)
    with pg_engine.connect() as c:
        trans = c.begin()
        c.execute(text("delete from events"))
        for source_id in SOURCES:
            c.execute(
                text(
                    "insert into source_registry (id, name, type, region, category, "
                    "source_class, priority, lane, enabled, url, parser, expected_frequency) "
                    "values (:id, :id, 'rss', 'AU', 'news', 'NEWS', 2, 'normal', true, "
                    "'https://example.org/' || :id, 'rss', 'daily') on conflict (id) do nothing"
                ),
                {"id": source_id},
            )
        c.execute(
            text(
                "insert into source_lineage (lineage_id, origin_source_id) "
                "values ('abc', 'abc'), ('wire', 'wire') on conflict do nothing"
            )
        )
        try:
            yield c
        finally:
            trans.rollback()


def insert_event(conn, event_id, *, first_seen=NOW, merged_into=None):
    conn.execute(
        text(
            "insert into events (event_id, schema_version, pipeline_version, scoring_version, "
            "enrichment_version, first_seen, last_seen, status, title, summary, merged_into) "
            "values (:id, :schema, :pipeline, :scoring, :enrichment, :first, :first, :status, "
            "'A story', 'What happened.', :merged)"
        ),
        {
            "id": event_id, "schema": SCHEMA_VERSION, "pipeline": PIPELINE_VERSION,
            "scoring": SCORING_VERSION, "enrichment": ENRICHMENT_VERSION, "first": first_seen,
            "status": "archived" if merged_into else "new", "merged": merged_into,
        },
    )


def report(conn, event_id, source_id, *, lineage=None, independent=True):
    n = conn.execute(text("select count(*) from event_sources")).scalar_one()
    conn.execute(
        text(
            "insert into event_sources (event_id, source_id, url, evidence_class, lineage_id, "
            "independent, url_hash) values (:e, :s, :url, 'NEWS', :lineage, :ind, :hash)"
        ),
        {"e": event_id, "s": source_id, "url": f"https://example.test/{n}",
         "lineage": lineage, "ind": independent, "hash": f"h{n}"},
    )


def test_a_story_another_source_also_reported_corroborates_both(conn):
    insert_event(conn, "evt-2026-000001")
    report(conn, "evt-2026-000001", "abc")
    report(conn, "evt-2026-000001", "thn")
    insert_event(conn, "evt-2026-000002")
    report(conn, "evt-2026-000002", "abc")
    assert load_corroboration(conn, since=SINCE) == {
        "abc": Corroboration(events=2, corroborated=1),
        "thn": Corroboration(events=1, corroborated=1),
    }


def test_a_source_never_corroborates_itself(conn):
    # Its second feed shares its lineage, and a copy of its headline is not independent.
    insert_event(conn, "evt-2026-000001")
    report(conn, "evt-2026-000001", "abc", lineage="abc")
    report(conn, "evt-2026-000001", "abc-politics", lineage="abc")
    report(conn, "evt-2026-000001", "thn", lineage="abc", independent=False)
    # Two reports of its own on one event are one event.
    insert_event(conn, "evt-2026-000002")
    report(conn, "evt-2026-000002", "abc", lineage="abc")
    report(conn, "evt-2026-000002", "abc")
    report(conn, "evt-2026-000002", "wire", lineage="wire", independent=False)
    got = load_corroboration(conn, since=SINCE)
    assert got["abc"] == Corroboration(events=2, corroborated=0)
    assert got["abc-politics"] == Corroboration(events=1, corroborated=0)


def test_a_syndicated_copy_is_corroborated_by_another_lineage(conn):
    insert_event(conn, "evt-2026-000001")
    report(conn, "evt-2026-000001", "wire", lineage="wire")
    report(conn, "evt-2026-000001", "thn", lineage="wire", independent=False)
    report(conn, "evt-2026-000001", "abc", lineage="abc")
    got = load_corroboration(conn, since=SINCE)
    assert got["thn"] == Corroboration(events=1, corroborated=1)
    assert got["wire"] == got["abc"] == Corroboration(events=1, corroborated=1)


def test_only_the_window_counts_and_a_merged_event_is_not_counted_twice(conn):
    insert_event(conn, "evt-2026-000001", first_seen=SINCE - timedelta(minutes=1))
    report(conn, "evt-2026-000001", "abc")
    insert_event(conn, "evt-2026-000002", first_seen=SINCE)
    report(conn, "evt-2026-000002", "abc")
    insert_event(conn, "evt-2026-000003", merged_into="evt-2026-000002")
    report(conn, "evt-2026-000003", "thn")
    assert load_corroboration(conn, since=SINCE) == {
        "abc": Corroboration(events=1, corroborated=0),
    }
