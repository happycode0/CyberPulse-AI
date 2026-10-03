"""The AU engine against a real Postgres (migration 008, worker/db/au.py, the rescore's AU step).

A fake connection would let the parts that matter be wrong: which text the facts are read from
(never the model's summary), the region join that fixes `au_directly_reported` for a merged
event, the `is distinct from` that leaves an unchanged event alone, and the 008 backfill.
"""

from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from worker.db.au import load_au_facts, save_au
from worker.db.events import load_events
from worker.db.migrate import MIGRATIONS_DIR, run_migrations
from worker.pipeline.assemble import AU_SOURCE_REASON
from worker.pipeline.au import AuSource, ModelReading
from worker.pipeline.run import _assess_au
from worker.pipeline.score import DEFAULT_SCORING_PATH, ScoringConfig
from worker.version import PIPELINE_VERSION, SCHEMA_VERSION, SCORING_VERSION

NOW = datetime(2026, 10, 3, 3, 0, tzinfo=UTC)
EVENT = "evt-2026-000001"
EVENT_2 = "evt-2026-000002"
CONFIG = ScoringConfig.load(DEFAULT_SCORING_PATH)


@pytest.fixture
def conn(pg_engine):
    """A connection in a transaction that is always rolled back, with three sources registered."""
    run_migrations(pg_engine)
    with pg_engine.connect() as c:
        trans = c.begin()
        c.execute(text("delete from events"))
        c.execute(text("delete from source_registry"))
        for sid, name, region in (
            ("itnews_security", "ITNews Security", "au"),
            ("acsc_alerts", "ACSC Alerts", "au"),
            ("wire", "Wire", "global"),
        ):
            c.execute(
                text(
                    "insert into source_registry (id, name, type, region, category, "
                    "source_class, priority, lane, enabled, url, parser, expected_frequency) "
                    "values (:id, :name, 'rss', :region, 'news', 'NEWS', 1, 'fast', true, "
                    "'https://example.test/feed', 'rss', 'hourly')"
                ),
                {"id": sid, "name": name, "region": region},
            )
        try:
            yield c
        finally:
            trans.rollback()


def insert_event(conn, event_id, *, title, source_summary=None, summary="Our own words."):
    conn.execute(
        text(
            "insert into events (event_id, schema_version, pipeline_version, scoring_version, "
            "enrichment_version, first_seen, last_seen, status, title, summary, source_summary, "
            "prominence) values (:id, :schema, :pipeline, :scoring, '1', :now, :now, 'new', "
            ":title, :summary, :source_summary, 0.5)"
        ),
        {
            "id": event_id,
            "schema": SCHEMA_VERSION,
            "pipeline": PIPELINE_VERSION,
            "scoring": SCORING_VERSION,
            "now": NOW,
            "title": title,
            "summary": summary,
            "source_summary": source_summary,
        },
    )


def add_source(conn, event_id, source_id, title, n=0):
    conn.execute(
        text(
            "insert into event_sources (event_id, source_id, url, title, published, "
            "evidence_class, url_hash) values (:e, :s, :url, :title, :now, 'NEWS', :hash)"
        ),
        {
            "e": event_id,
            "s": source_id,
            "url": f"https://example.test/{source_id}/{n}",
            "title": title,
            "now": NOW,
            "hash": f"{event_id}-{source_id}-{n}",
        },
    )


def set_model_reading(conn, event_id, relevance, reasons=(), sectors=()):
    conn.execute(
        text(
            "update events set au_model_relevance = :r, au_model_reasons = cast(:reasons as "
            "text[]), au_model_sectors = cast(:sectors as text[]) where event_id = :e"
        ),
        {"e": event_id, "r": relevance, "reasons": list(reasons), "sectors": list(sectors)},
    )


def row(conn, event_id):
    return (
        conn.execute(text("select * from events where event_id = :e"), {"e": event_id})
        .mappings()
        .one()
    )


def assess(conn, *event_ids):
    return _assess_au(conn, load_events(conn, list(event_ids)), CONFIG)


# ─── The facts ────────────────────────────────────────────────────────────────────────────────────


def test_facts_are_the_sources_words_and_who_reported_it_never_the_models_summary(conn):
    insert_event(
        conn,
        EVENT,
        title="SIM-swap wave hits telco customers",
        source_summary="Customers lost access to their numbers.",
        summary="A model wrote that Sydney was affected.",
    )
    add_source(conn, EVENT, "wire", "SIM-swap wave hits telco customers")
    add_source(conn, EVENT, "itnews_security", "Optus customers hit by SIM swaps", n=1)
    set_model_reading(conn, EVENT, 0.2, ["Optus customers were targeted"], ["telecommunications"])
    facts = load_au_facts(conn, [EVENT])[EVENT]
    assert facts.texts == (
        "SIM-swap wave hits telco customers",
        "Customers lost access to their numbers.",
        "Optus customers hit by SIM swaps",
    )
    assert facts.sources == (
        AuSource("wire", "Wire", "global"),
        AuSource("itnews_security", "ITNews Security", "au"),
    )
    assert facts.model == ModelReading(
        0.2, ("Optus customers were targeted",), ("telecommunications",)
    )
    assert load_au_facts(conn, []) == {}


# ─── The rescore's AU step ────────────────────────────────────────────────────────────────────────


def test_the_rescore_publishes_the_facts_and_keeps_the_models_reading_apart(conn):
    insert_event(conn, EVENT, title="SIM-swap wave", summary="A model wrote Sydney.")
    add_source(conn, EVENT, "wire", "SIM-swap wave")
    add_source(conn, EVENT, "itnews_security", "Optus customers hit by SIM swaps", n=1)
    set_model_reading(conn, EVENT, 0.2, ["Optus customers were targeted"], ["telecommunications"])
    (event,) = assess(conn, EVENT)
    stored = row(conn, EVENT)
    assert stored["au_relevance"] == CONFIG.au.floors.organisation == event.au.relevance
    assert stored["au_directly_reported"] is True
    assert stored["au_reasons"] == [
        "names an Australian organisation (Optus)",
        AU_SOURCE_REASON,
        "Optus customers were targeted",
    ]
    assert stored["au_sectors"] == ["telecommunications"]
    assert stored["au_soci_asset_classes"] == ["communications"]
    assert stored["au_model_relevance"] == 0.2  # the model's reading is never overwritten


def test_a_merged_australian_report_marks_the_event_reported_in_au(conn):
    # Ingest set `au_directly_reported` from the first source only; an Australian authority's
    # report merged in later now counts.
    insert_event(conn, EVENT, title="Critical Fortinet flaw")
    add_source(conn, EVENT, "wire", "Critical Fortinet flaw")
    assert row(conn, EVENT)["au_directly_reported"] is False
    add_source(conn, EVENT, "acsc_alerts", "Critical vulnerability in Fortinet FortiOS", n=1)
    assess(conn, EVENT)
    stored = row(conn, EVENT)
    assert stored["au_directly_reported"] is True
    assert stored["au_reasons"] == ["published by an Australian government authority (ACSC Alerts)"]
    assert stored["au_relevance"] == CONFIG.au.floors.authority


def test_an_unrated_event_stays_null_and_an_unchanged_one_is_not_written_again(conn):
    insert_event(conn, EVENT, title="Optus outage")
    insert_event(conn, EVENT_2, title="Chrome zero-day patched")
    add_source(conn, EVENT, "wire", "Optus outage")
    add_source(conn, EVENT_2, "wire", "Chrome zero-day patched")
    assessed = {e.event_id: e.au for e in assess(conn, EVENT, EVENT_2)}
    assert row(conn, EVENT)["au_relevance"] == CONFIG.au.floors.organisation
    assert row(conn, EVENT_2)["au_relevance"] is None
    assert save_au(conn, assessed) == 0


# ─── Migration 008 ────────────────────────────────────────────────────────────────────────────────


def test_the_backfill_moves_a_rated_events_reading_to_the_model_columns(conn):
    insert_event(conn, EVENT, title="Victorian hospitals hit")
    insert_event(conn, EVENT_2, title="Unrated")
    conn.execute(
        text(
            "update events set au_relevance = 0.8, au_reasons = cast(:reasons as text[]), "
            "au_sectors = '{health}' where event_id = :e"
        ),
        {"e": EVENT, "reasons": [AU_SOURCE_REASON, "Victorian hospitals were hit"]},
    )
    sql = (MIGRATIONS_DIR / "008_au_model_reading.sql").read_text()
    backfill = sql[sql.index("update events") :]
    conn.exec_driver_sql(backfill)
    rated, unrated = row(conn, EVENT), row(conn, EVENT_2)
    assert rated["au_model_relevance"] == 0.8
    assert rated["au_model_reasons"] == ["Victorian hospitals were hit"]
    assert rated["au_model_sectors"] == ["health"]
    assert unrated["au_model_relevance"] is None and unrated["au_model_reasons"] == []


def test_a_model_relevance_outside_zero_to_one_is_refused(conn):
    insert_event(conn, EVENT, title="x")
    with pytest.raises(IntegrityError, match="check"), conn.begin_nested():
        set_model_reading(conn, EVENT, 1.5)
