"""Material change against a real Postgres: what the registers and new reports write.

The register half is a set-based insert (`insert ... select ... where not exists`) whose guards
are the point: only events first seen before the change, only standing ones, and only once.
"""

from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import text

from worker.db.advisories import record_advisories
from worker.db.events import find_candidates
from worker.db.groundtruth import apply_kev
from worker.db.ingest import apply_update, insert_new_event
from worker.db.material import record_register_changes
from worker.db.migrate import run_migrations
from worker.groundtruth.kev import KevCatalogue
from worker.groundtruth.osv import Advisory, Package
from worker.models import KevEntry, RawItem, SourceConfig
from worker.pipeline.assemble import build_new_event, plan_update
from worker.pipeline.material import fix_published, kev_listing
from worker.pipeline.normalise import normalise
from worker.version import PIPELINE_VERSION, SCHEMA_VERSION, SCORING_VERSION

NOW = datetime(2026, 10, 3, 6, 0, tzinfo=UTC)
CVE = "CVE-2026-0042"
ADDED = date(2026, 10, 1)


@pytest.fixture
def conn(pg_engine):
    """A connection in a transaction that is always rolled back, starting from empty tables."""
    run_migrations(pg_engine)
    with pg_engine.connect() as c:
        trans = c.begin()
        c.execute(text("delete from events"))
        c.execute(text("delete from cves"))
        c.execute(text("insert into cves (cve_id) values (:c)"), {"c": CVE})
        for sid, region in (("s1", "US"), ("s2", "US")):
            c.execute(
                text(
                    "insert into source_registry (id, name, type, region, category, "
                    "source_class, priority, lane, enabled, url, parser, expected_frequency) "
                    "values (:id, :id, 'rss', :region, 'news', 'NEWS', 1, 'fast', true, "
                    "'https://example.org/feed', 'feed', 'daily') on conflict (id) do nothing"
                ),
                {"id": sid, "region": region},
            )
        try:
            yield c
        finally:
            trans.rollback()


def insert_event(conn, event_id, *, first_seen, status="new", merged_into=None):
    conn.execute(
        text(
            "insert into events (event_id, schema_version, pipeline_version, scoring_version, "
            "enrichment_version, first_seen, last_seen, last_material_update, status, "
            "merged_into, title, summary) values (:id, :schema, :pipeline, :scoring, '1', "
            ":first_seen, :first_seen, :first_seen, :status, :merged_into, 't', 's')"
        ),
        {
            "id": event_id,
            "schema": SCHEMA_VERSION,
            "pipeline": PIPELINE_VERSION,
            "scoring": SCORING_VERSION,
            "first_seen": first_seen,
            "status": status,
            "merged_into": merged_into,
        },
    )
    conn.execute(
        text("insert into event_cves (event_id, cve_id) values (:e, :c)"),
        {"e": event_id, "c": CVE},
    )


def state(conn, event_id):
    return conn.execute(
        text("select status, last_material_update from events where event_id = :e"),
        {"e": event_id},
    ).one()


def timeline(conn, event_id):
    return [
        (r[0], r[1])
        for r in conn.execute(
            text("select type, summary from event_timeline where event_id = :e order by id"),
            {"e": event_id},
        )
    ]


# --- what the registers report ----------------------------------------------------------------


def test_a_register_change_reaches_the_events_that_began_before_it(conn):
    before = datetime(2026, 9, 28, tzinfo=UTC)
    insert_event(conn, "evt-2026-000001", first_seen=before)
    insert_event(conn, "evt-2026-000002", first_seen=datetime(2026, 10, 2, tzinfo=UTC))
    change = kev_listing(CVE, ADDED)

    assert record_register_changes(conn, [change]) == {"evt-2026-000001"}
    assert timeline(conn, "evt-2026-000001") == [("EXPLOIT_CONFIRMED", change.summary)]
    assert tuple(state(conn, "evt-2026-000001")) == ("developing", change.at)
    # Began with the CVE already listed: nothing changed for it.
    assert timeline(conn, "evt-2026-000002") == []
    assert state(conn, "evt-2026-000002").status == "new"


def test_a_register_change_is_written_once(conn):
    insert_event(conn, "evt-2026-000001", first_seen=NOW - timedelta(days=5))
    change = fix_published(CVE, "GHSA-x", now=NOW)
    record_register_changes(conn, [change])
    assert record_register_changes(conn, [change]) == set()
    # Nor is a second advisory's fix a second patch.
    assert record_register_changes(conn, [fix_published(CVE, "GHSA-y", now=NOW)]) == set()
    assert len(timeline(conn, "evt-2026-000001")) == 1


def test_a_listing_the_event_already_reported_is_not_news_again(conn):
    insert_event(conn, "evt-2026-000001", first_seen=datetime(2026, 9, 28, tzinfo=UTC))
    conn.execute(
        text(
            "insert into event_timeline (event_id, ts, type, summary) values "
            "('evt-2026-000001', :t, 'EXPLOIT_CONFIRMED', "
            "'CISA: CISA Adds One Known Exploited Vulnerability to Catalog')"
        ),
        {"t": datetime(2026, 10, 1, 18, tzinfo=UTC)},
    )
    assert record_register_changes(conn, [kev_listing(CVE, ADDED)]) == set()


def test_a_merged_event_is_left_alone_an_archived_one_comes_back_and_a_later_clock_stands(conn):
    old = NOW - timedelta(days=5)
    insert_event(conn, "evt-2026-000001", first_seen=old, status="monitoring")
    insert_event(
        conn, "evt-2026-000002", first_seen=old, status="archived", merged_into="evt-2026-000001"
    )
    insert_event(conn, "evt-2026-000003", first_seen=old, status="archived")
    conn.execute(
        text("update events set last_material_update = :t where event_id = 'evt-2026-000001'"),
        {"t": NOW},
    )
    change = kev_listing(CVE, ADDED)
    assert record_register_changes(conn, [change]) == {"evt-2026-000001", "evt-2026-000003"}
    # A standing event keeps its status, and the clock never moves back.
    assert tuple(state(conn, "evt-2026-000001")) == ("monitoring", NOW)
    assert tuple(state(conn, "evt-2026-000003")) == ("developing", change.at)
    assert timeline(conn, "evt-2026-000002") == []


def test_kev_reports_only_the_listings_that_are_new(conn):
    catalogue = KevCatalogue(
        version="2026.10.03",
        released=NOW,
        declared_count=1,
        entries={CVE: KevEntry(listed=True, date_added=ADDED, due_date=date(2026, 10, 22))},
        skipped=0,
    )
    assert apply_kev(conn, catalogue).newly_listed == ((CVE, ADDED),)
    revised = KevCatalogue(
        version="2026.10.04",
        released=NOW,
        declared_count=1,
        entries={CVE: KevEntry(listed=True, date_added=ADDED, due_date=date(2026, 10, 29))},
        skipped=0,
    )
    # A revised due date is an update, but the CVE was already listed.
    result = apply_kev(conn, revised)
    assert result.updated == 1 and result.newly_listed == ()


def advisory(*fixed):
    return Advisory(
        id="GHSA-x",
        source="ghsa",
        summary="Acme flaw",
        severity="high",
        reviewed=True,
        packages=(Package("npm", "acme", tuple(fixed)),),
        published=NOW - timedelta(days=3),
        modified=NOW,
        aliases=(CVE,),
    )


def test_an_advisory_gaining_a_fixed_version_is_reported_and_a_first_reading_is_not(conn):
    assert record_advisories(conn, CVE, [advisory()], now=NOW).fixed == ()
    assert record_advisories(conn, CVE, [advisory("1.2.3")], now=NOW).fixed == ("GHSA-x",)
    # Another fixed version on an advisory that already had one is not news.
    assert record_advisories(conn, CVE, [advisory("1.2.3", "1.1.9")], now=NOW).fixed == ()


# --- what a new report says --------------------------------------------------------------------


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


def item(sid, title, url):
    raw = RawItem(
        source_id=sid,
        url=url,
        title=title,
        published=NOW - timedelta(hours=1),
        fetched_at=NOW,
        payload_hash="p",
    )
    return normalise(raw, now=NOW)


def test_a_report_that_confirms_exploitation_makes_a_new_event_developing(conn):
    first = item("s1", f"Acme VPN flaw {CVE}", "https://example.org/a")
    event = build_new_event(first, source("s1"), "evt-2026-000001", now=NOW - timedelta(days=1))
    insert_new_event(conn, event, first, payload_hash="p")

    news = item("s2", f"Acme VPN flaw {CVE} actively exploited", "https://other.example/b")
    [target] = [c for c in find_candidates(conn, news) if c.event_id == "evt-2026-000001"]
    apply_update(conn, plan_update(target, news, source("s2"), now=NOW), news, payload_hash="p")

    assert tuple(state(conn, "evt-2026-000001")) == ("developing", news.published)
    assert ("EXPLOIT_CONFIRMED", f"S2: {news.title}") in timeline(conn, "evt-2026-000001")
