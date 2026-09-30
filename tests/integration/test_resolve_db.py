from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import text

from worker.db.events import (
    find_candidates,
    load_event_dates,
    load_live_events,
    next_event_id,
)
from worker.db.migrate import run_migrations
from worker.models import RawItem
from worker.pipeline.normalise import normalise, normalise_title
from worker.pipeline.resolve import Decision, EventCandidate, resolve
from worker.version import (
    ENRICHMENT_VERSION,
    PIPELINE_VERSION,
    SCHEMA_VERSION,
    SCORING_VERSION,
)

NOW = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)
SOURCE = "acsc-alerts"


@pytest.fixture
def conn(pg_engine):
    """A connection inside a transaction that is always rolled back, starting from an
    empty events table (other tests commit rows into the shared scratch database)."""
    run_migrations(pg_engine)
    with pg_engine.connect() as c:
        trans = c.begin()
        c.execute(text("delete from events"))
        for source_id in (SOURCE, "other-source"):
            c.execute(
                text(
                    "insert into source_registry (id, name, type, region, category, "
                    "source_class, priority, lane, enabled, url, parser, expected_frequency) "
                    "values (:id, :id, 'rss', 'AU', 'advisory', 'AUTHORITATIVE', 1, 'fast', "
                    "true, 'https://example.org/feed', 'feed', 'daily') "
                    "on conflict (id) do nothing"
                ),
                {"id": source_id},
            )
        try:
            yield c
        finally:
            trans.rollback()


def insert_event(conn, event_id, *, title="Acme VPN zero-day", first_seen=NOW,
                 last_seen=None, prominence=None, normalised_title=None):
    conn.execute(
        text(
            "insert into events (event_id, schema_version, pipeline_version, scoring_version, "
            "enrichment_version, first_seen, last_seen, title, normalised_title, summary, "
            "prominence) values (:id, :schema, :pipeline, :scoring, :enrichment, :first, "
            ":last, :title, :nt, 's', :prominence)"
        ),
        {
            "id": event_id,
            "schema": SCHEMA_VERSION,
            "pipeline": PIPELINE_VERSION,
            "scoring": SCORING_VERSION,
            "enrichment": ENRICHMENT_VERSION,
            "first": first_seen,
            "last": last_seen or first_seen,
            "title": title,
            "nt": normalised_title if normalised_title is not None else normalise_title(title),
            "prominence": prominence,
        },
    )


def insert_source(conn, event_id, item, *, source_id=None, evidence_class="NEWS", lineage_id=None):
    conn.execute(
        text(
            "insert into event_sources (event_id, source_id, url, canonical_url, guid, title, "
            "published, fetched_at, evidence_class, lineage_id, independent, url_hash, title_hash) "
            "values (:event_id, :source_id, :url, :canonical, :guid, :title, :published, "
            ":fetched, :evidence_class, :lineage_id, true, :url_hash, :title_hash)"
        ),
        {
            "event_id": event_id,
            "source_id": source_id or item.source_id,
            "url": item.url,
            "canonical": item.canonical_url,
            "guid": item.guid,
            "title": item.title,
            "published": item.published,
            "fetched": item.fetched_at,
            "evidence_class": evidence_class,
            "lineage_id": lineage_id,
            "url_hash": item.url_hash,
            "title_hash": item.title_hash,
        },
    )


def insert_cve(conn, event_id, cve_id):
    conn.execute(text("insert into cves (cve_id) values (:c) on conflict do nothing"), {"c": cve_id})
    conn.execute(
        text("insert into event_cves (event_id, cve_id) values (:e, :c)"),
        {"e": event_id, "c": cve_id},
    )


def make_item(title="Acme VPN gateway zero-day exploited in the wild", *,
              url="https://example.org/news/acme", guid=None, published=NOW,
              source_id=SOURCE):
    raw = RawItem(source_id=source_id, url=url, guid=guid, title=title, published=published,
                  fetched_at=published, payload_hash="p")
    return normalise(raw, now=NOW + timedelta(days=365))


# --- next_event_id --------------------------------------------------------------


def test_next_event_id_is_sequential_and_zero_padded(conn):
    assert next_event_id(conn, 2026) == "evt-2026-000001"
    insert_event(conn, "evt-2026-000001")
    assert next_event_id(conn, 2026) == "evt-2026-000002"


def test_next_event_id_counts_each_year_separately(conn):
    insert_event(conn, "evt-2026-000041")
    assert next_event_id(conn, 2027) == "evt-2027-000001"
    assert next_event_id(conn, 2026) == "evt-2026-000042"


def test_next_event_id_is_stable_until_the_event_is_inserted(conn):
    assert next_event_id(conn, 2026) == next_event_id(conn, 2026)


def test_next_event_id_rejects_a_year_that_would_break_the_id_format(conn):
    with pytest.raises(ValueError):
        next_event_id(conn, 12026)


# --- find_candidates ------------------------------------------------------------


def ids(events):
    return {e.event_id for e in events}


def test_finds_an_event_by_url_hash_and_resolves_it_as_duplicate(conn):
    item = make_item()
    insert_event(conn, "evt-2026-000001")
    insert_source(conn, "evt-2026-000001", item)
    candidates = find_candidates(conn, item)
    assert ids(candidates) == {"evt-2026-000001"}
    assert isinstance(candidates[0], EventCandidate)
    r = resolve(item, candidates)
    assert (r.decision, r.method) == (Decision.DUPLICATE, "url_hash")


def test_url_identity_finds_events_older_than_the_lookback(conn):
    item = make_item()
    old = NOW - timedelta(days=90)
    insert_event(conn, "evt-2026-000001", first_seen=old)
    insert_source(conn, "evt-2026-000001", item)
    assert ids(find_candidates(conn, item)) == {"evt-2026-000001"}


def test_finds_an_event_by_canonical_url_over_a_different_scheme(conn):
    item = make_item(url="https://www.example.org/news/acme")
    other = make_item(url="http://example.org/news/acme")
    insert_event(conn, "evt-2026-000001")
    insert_source(conn, "evt-2026-000001", other)
    assert ids(find_candidates(conn, item)) == {"evt-2026-000001"}


def test_finds_an_event_by_guid_within_the_same_source_only(conn):
    item = make_item(guid="42", url="https://example.org/a")
    same_feed = make_item(guid="42", url="https://example.org/b", title="Different words")
    other_feed = make_item(guid="42", url="https://example.org/c", title="More words",
                           source_id="other-source")
    insert_event(conn, "evt-2026-000001", title="Different words")
    insert_source(conn, "evt-2026-000001", same_feed)
    insert_event(conn, "evt-2026-000002", title="More words")
    insert_source(conn, "evt-2026-000002", other_feed)
    assert ids(find_candidates(conn, item)) == {"evt-2026-000001"}


def test_finds_an_event_by_the_title_hash_of_one_of_its_sources(conn):
    item = make_item("Vendor confirms Acme gateway breach", url="https://example.org/x")
    earlier = make_item("Vendor confirms Acme gateway breach", url="https://example.org/y",
                        source_id="other-source")
    insert_event(conn, "evt-2026-000001", title="Something else altogether")
    insert_source(conn, "evt-2026-000001", earlier)
    candidates = find_candidates(conn, item)
    assert ids(candidates) == {"evt-2026-000001"}
    assert resolve(item, candidates).method == "title_hash"


def test_finds_an_event_by_shared_cve(conn):
    item = make_item("Zero-day in Acme gateway CVE-2026-88772", url="https://example.org/z")
    insert_event(conn, "evt-2026-000001", title="Nothing alike in wording")
    insert_cve(conn, "evt-2026-000001", "CVE-2026-88772")
    insert_event(conn, "evt-2026-000002", title="Also nothing alike")
    assert ids(find_candidates(conn, item)) == {"evt-2026-000001"}


def test_finds_an_event_by_trigram_title_similarity(conn):
    item = make_item("Acme VPN gateway zero-day exploited in the wild", url="https://example.org/t")
    insert_event(conn, "evt-2026-000001", title="Acme VPN gateway zero-day exploited in wild")
    insert_event(conn, "evt-2026-000002", title="Quarterly phishing statistics for retailers")
    assert ids(find_candidates(conn, item)) == {"evt-2026-000001"}


def test_fuzzy_signals_ignore_events_idle_for_more_than_thirty_days(conn):
    item = make_item(url="https://example.org/t")
    stale = NOW - timedelta(days=31)
    insert_event(conn, "evt-2026-000001", title=item.title, first_seen=stale)
    insert_event(conn, "evt-2026-000002", title=item.title, first_seen=stale,
                 last_seen=NOW - timedelta(days=29))
    assert ids(find_candidates(conn, item)) == {"evt-2026-000002"}


def test_unrelated_events_are_not_candidates(conn):
    insert_event(conn, "evt-2026-000001", title="Quarterly phishing statistics for retailers")
    assert find_candidates(conn, make_item()) == []


def test_candidates_are_fully_hydrated(conn):
    item = make_item(guid="g-1")
    insert_event(conn, "evt-2026-000001", title=item.title)
    insert_source(conn, "evt-2026-000001", item, evidence_class="AUTHORITATIVE")
    insert_cve(conn, "evt-2026-000001", "CVE-2026-88772")
    conn.execute(text(
        "insert into cve_scores (cve_id, kind, score, vector, source, observed_at) values "
        "('CVE-2026-88772', 'cvss', 5.0, null, 'nvd', now() - interval '1 day'), "
        "('CVE-2026-88772', 'cvss', 9.8, 'AV:N', 'nvd', now())"
    ))
    conn.execute(text(
        "insert into claims (event_id, text, confidence) values ('evt-2026-000001', 'c', 0.7)"
    ))
    conn.execute(text(
        "insert into event_timeline (event_id, ts, type, summary) "
        "values ('evt-2026-000001', now(), 'NEW_FACT', 'first')"
    ))
    [event] = find_candidates(conn, item)
    assert event.title == item.title
    assert [(s.source_id, s.evidence_class.value) for s in event.sources] == [
        (SOURCE, "AUTHORITATIVE")
    ]
    assert event.cves[0].id == "CVE-2026-88772"
    assert event.cves[0].cvss.score == 9.8  # latest observation wins
    assert event.cves[0].epss.status == "unknown" and event.cves[0].epss.score is None
    assert [c.text for c in event.claims] == ["c"]
    assert [t.summary for t in event.timeline] == ["first"]
    assert event.risk.prominence is None
    assert event.source_guids == frozenset({(SOURCE, "g-1")})


# --- live events and dates ------------------------------------------------------


def test_load_live_events_filters_orders_and_limits_by_prominence(conn):
    insert_event(conn, "evt-2026-000001", prominence=0.30)
    insert_event(conn, "evt-2026-000002", prominence=0.90)
    insert_event(conn, "evt-2026-000003", prominence=0.05)
    insert_event(conn, "evt-2026-000004", prominence=None)
    insert_event(conn, "evt-2026-000005", prominence=0.60)
    live = load_live_events(conn, min_prominence=0.05, limit=10)
    assert [e.event_id for e in live] == ["evt-2026-000002", "evt-2026-000005", "evt-2026-000001"]
    assert not isinstance(live[0], EventCandidate)
    assert live[0].risk.prominence == 0.90
    top = load_live_events(conn, min_prominence=0.05, limit=2)
    assert [e.event_id for e in top] == ["evt-2026-000002", "evt-2026-000005"]


def test_load_live_events_output_publishes_cleanly(conn):
    item = make_item()
    insert_event(conn, "evt-2026-000001", prominence=0.5)
    insert_source(conn, "evt-2026-000001", item)
    [event] = load_live_events(conn, min_prominence=0.05, limit=10)
    assert event.model_dump_public()["sources"][0]["source_id"] == SOURCE


def test_load_event_dates_returns_distinct_utc_dates_newest_first(conn):
    insert_event(conn, "evt-2026-000001", first_seen=datetime(2026, 9, 28, 23, 59, tzinfo=UTC))
    insert_event(conn, "evt-2026-000002", first_seen=datetime(2026, 9, 28, 1, 0, tzinfo=UTC))
    insert_event(conn, "evt-2026-000003", first_seen=datetime(2026, 9, 30, 0, 0, tzinfo=UTC))
    assert load_event_dates(conn) == [date(2026, 9, 30), date(2026, 9, 28)]


def test_load_event_dates_is_empty_without_events(conn):
    assert load_event_dates(conn) == []
