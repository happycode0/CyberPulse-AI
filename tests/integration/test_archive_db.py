"""Archiving against a real Postgres: which events leave the live set, and how one comes back."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from worker.db.archive import archive_faded
from worker.db.events import find_candidates
from worker.db.ingest import apply_update, events_needing_score, insert_new_event
from worker.db.migrate import run_migrations
from worker.models import RawItem, SourceConfig
from worker.pipeline.assemble import build_new_event, plan_update
from worker.pipeline.normalise import normalise
from worker.pipeline.score import ArchiveRule
from worker.version import PIPELINE_VERSION, SCHEMA_VERSION, SCORING_VERSION

NOW = datetime(2026, 10, 3, 6, 0, tzinfo=UTC)
RULE = ArchiveRule(prominence_below=0.05, idle_days=30)


@pytest.fixture
def conn(pg_engine):
    """A connection in a transaction that is always rolled back, starting from empty tables."""
    run_migrations(pg_engine)
    with pg_engine.connect() as c:
        trans = c.begin()
        c.execute(text("delete from events"))
        for sid in ("s1", "s2"):
            c.execute(
                text(
                    "insert into source_registry (id, name, type, region, category, "
                    "source_class, priority, lane, enabled, url, parser, expected_frequency) "
                    "values (:id, :id, 'rss', 'US', 'news', 'NEWS', 1, 'fast', true, "
                    "'https://example.org/feed', 'feed', 'daily') on conflict (id) do nothing"
                ),
                {"id": sid},
            )
        try:
            yield c
        finally:
            trans.rollback()


def insert_event(conn, event_id, *, prominence, lmu=NOW, first_seen=None, status="new",
                 merged_into=None):
    conn.execute(
        text(
            "insert into events (event_id, schema_version, pipeline_version, scoring_version, "
            "enrichment_version, first_seen, last_seen, last_material_update, status, "
            "merged_into, title, summary, prominence) values (:id, :schema, :pipeline, "
            ":scoring, '1', :first_seen, :first_seen, :lmu, :status, :merged_into, 't', 's', "
            ":prominence)"
        ),
        {
            "id": event_id,
            "schema": SCHEMA_VERSION,
            "pipeline": PIPELINE_VERSION,
            "scoring": SCORING_VERSION,
            "first_seen": first_seen or (lmu or NOW) - timedelta(days=1),
            "lmu": lmu,
            "status": status,
            "merged_into": merged_into,
            "prominence": prominence,
        },
    )


def status(conn, event_id):
    return conn.execute(
        text("select status from events where event_id = :e"), {"e": event_id}
    ).scalar_one()


def test_a_faded_or_idle_event_is_archived_and_a_live_one_is_not(conn):
    insert_event(conn, "evt-2026-000001", prominence=0.6)
    insert_event(conn, "evt-2026-000002", prominence=0.04)
    insert_event(conn, "evt-2026-000003", prominence=0.6, lmu=NOW - timedelta(days=31))
    # Exactly on the line on both counts: not yet.
    insert_event(conn, "evt-2026-000004", prominence=0.05, lmu=NOW - timedelta(days=30))
    insert_event(conn, "evt-2026-000005", prominence=0.3, status="monitoring")

    assert archive_faded(conn, RULE, now=NOW) == ["evt-2026-000002", "evt-2026-000003"]
    assert [status(conn, f"evt-2026-00000{n}") for n in range(1, 6)] == [
        "new", "archived", "archived", "new", "monitoring",
    ]
    # Archiving twice changes nothing.
    assert archive_faded(conn, RULE, now=NOW) == []


def test_an_unscored_event_is_archived_on_age_alone(conn):
    insert_event(conn, "evt-2026-000001", prominence=None)
    insert_event(conn, "evt-2026-000002", prominence=None, lmu=None,
                 first_seen=NOW - timedelta(days=40))
    assert archive_faded(conn, RULE, now=NOW) == ["evt-2026-000002"]


def test_a_merged_event_is_left_as_it_is(conn):
    insert_event(conn, "evt-2026-000001", prominence=0.6)
    insert_event(conn, "evt-2026-000002", prominence=0.01, status="archived",
                 merged_into="evt-2026-000001")
    before = conn.execute(
        text("select updated_at from events where event_id = 'evt-2026-000002'")
    ).scalar_one()
    assert archive_faded(conn, RULE, now=NOW) == []
    assert conn.execute(
        text("select updated_at from events where event_id = 'evt-2026-000002'")
    ).scalar_one() == before


def test_an_archived_event_is_not_rescored_even_when_a_report_touched_it(conn):
    insert_event(conn, "evt-2026-000001", prominence=0.01, status="archived")
    insert_event(conn, "evt-2026-000002", prominence=0.6)
    assert events_needing_score(
        conn, scoring_version=SCORING_VERSION, touched=["evt-2026-000001"], floor=0.05
    ) == ["evt-2026-000002"]


def source(sid):
    return SourceConfig(
        id=sid,
        name=sid.upper(),
        type="rss",
        region="us",
        category="news",
        source_class="feed",
        priority=1,
        lane="fast",
        enabled=True,
        url="https://example.org/feed",
        parser="rss",
        expected_frequency="daily",
    )


def item(sid, title, url, *, published):
    raw = RawItem(source_id=sid, url=url, title=title, published=published, fetched_at=NOW,
                  payload_hash="p")
    return normalise(raw, now=NOW)


def archived_story(conn):
    """An event first reported 20 days ago whose score has since faded below the floor."""
    then = NOW - timedelta(days=20)
    first = item("s1", "Acme VPN flaw CVE-2026-0042", "https://example.org/a", published=then)
    event = build_new_event(first, source("s1"), "evt-2026-000001", now=then)
    insert_new_event(conn, event, first, payload_hash="p")
    conn.execute(text("update events set prominence = 0.01"))
    assert archive_faded(conn, RULE, now=NOW) == ["evt-2026-000001"]


def report(conn, title):
    news = item("s2", title, "https://other.example/b", published=NOW - timedelta(hours=1))
    [target] = [c for c in find_candidates(conn, news) if c.event_id == "evt-2026-000001"]
    apply_update(conn, plan_update(target, news, source("s2"), now=NOW), news, payload_hash="p")


def test_a_material_report_brings_an_archived_event_back(conn):
    archived_story(conn)
    report(conn, "Acme VPN flaw CVE-2026-0042 actively exploited")
    assert status(conn, "evt-2026-000001") == "developing"
    # Back in the live set, so the run rescores it before it archives anything.
    assert "evt-2026-000001" in events_needing_score(
        conn, scoring_version=SCORING_VERSION, touched=["evt-2026-000001"], floor=0.05
    )


def test_a_repeat_report_leaves_an_archived_event_archived(conn):
    archived_story(conn)
    report(conn, "Acme VPN flaw CVE-2026-0042")
    assert status(conn, "evt-2026-000001") == "archived"
