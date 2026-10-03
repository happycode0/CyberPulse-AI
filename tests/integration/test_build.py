import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from worker.db.migrate import run_migrations
from worker.publish.build import build_all
from worker.publish.validate import ValidationFailure
from worker.version import (
    ENRICHMENT_VERSION,
    PIPELINE_VERSION,
    SCHEMA_VERSION,
    SCORING_VERSION,
)

NOW = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)
SOURCE = "acsc-alerts"
OR_KEY = "sk-or-v1-deadbeefdeadbeefdeadbeefdeadbeef"


@pytest.fixture
def conn(pg_engine):
    """A connection inside a transaction that is always rolled back, starting from empty
    events / runs / source_health (other tests commit rows into the shared scratch DB)."""
    run_migrations(pg_engine)
    with pg_engine.connect() as c:
        trans = c.begin()
        for table in ("events", "runs", "source_health"):
            c.execute(text(f"delete from {table}"))
        c.execute(
            text(
                "insert into source_registry (id, name, type, region, category, "
                "source_class, priority, lane, enabled, url, parser, expected_frequency) "
                "values (:id, 'ACSC Alerts', 'rss', 'AU', 'advisory', 'AUTHORITATIVE', 1, "
                "'fast', true, 'https://example.org/feed', 'feed', 'daily') "
                "on conflict (id) do nothing"
            ),
            {"id": SOURCE},
        )
        try:
            yield c
        finally:
            trans.rollback()


def insert_event(conn, event_id, *, summary="CVE-2026-88772 exploited; patch available.",
                 prominence=0.9, first_seen=NOW, severity="high"):
    conn.execute(
        text(
            "insert into events (event_id, schema_version, pipeline_version, scoring_version, "
            "enrichment_version, first_seen, last_seen, title, normalised_title, summary, "
            "severity, prominence) values (:id, :schema, :pipeline, :scoring, :enrichment, "
            ":first, :first, 'Acme VPN zero-day', 'acme vpn zero-day', :summary, :severity, "
            ":prominence)"
        ),
        {
            "id": event_id,
            "schema": SCHEMA_VERSION,
            "pipeline": PIPELINE_VERSION,
            "scoring": SCORING_VERSION,
            "enrichment": ENRICHMENT_VERSION,
            "first": first_seen,
            "summary": summary,
            "severity": severity,
            "prominence": prominence,
        },
    )


def insert_event_with_summary(conn, summary):
    insert_event(conn, "evt-2026-000001", summary=summary)


def insert_run(conn, *, finished_at=NOW - timedelta(minutes=3)):
    conn.execute(
        text(
            "insert into runs (run_id, lane, started_at, finished_at, sources_ok, "
            "sources_failed, errors) values ('run-1', 'fast', :s, :f, 9, 1, '{boom}')"
        ),
        {"s": NOW - timedelta(minutes=5), "f": finished_at},
    )


def read(directory, name):
    return json.loads((directory / name).read_text())


def test_build_emits_expected_files(tmp_path, conn):
    insert_event(conn, "evt-2026-000001")
    names = {p.name for p in build_all(conn, tmp_path, now=NOW)}
    assert {"live.json", "index.json", "source-health.json", "system-status.json"} <= names
    assert (tmp_path / "history" / "2026-09-30.json").is_file()


def test_live_json_lists_events_by_prominence_above_the_threshold(tmp_path, conn):
    insert_event(conn, "evt-2026-000001", prominence=0.4)
    insert_event(conn, "evt-2026-000002", prominence=0.9)
    insert_event(conn, "evt-2026-000003", prominence=0.05)
    insert_event(conn, "evt-2026-000004", prominence=None)
    build_all(conn, tmp_path, now=NOW)
    ids = [e["event_id"] for e in read(tmp_path, "live.json")["events"]]
    assert ids == ["evt-2026-000002", "evt-2026-000001"]


def test_history_and_index_follow_first_seen_dates(tmp_path, conn):
    insert_event(conn, "evt-2026-000001", first_seen=NOW)
    insert_event(conn, "evt-2026-000002", first_seen=NOW - timedelta(days=1))
    build_all(conn, tmp_path, now=NOW)
    index = read(tmp_path, "index.json")
    assert [d["date"] for d in index["days"]] == ["2026-09-30", "2026-09-29"]
    assert [e["event_id"] for e in read(tmp_path, "history/2026-09-29.json")["events"]] == [
        "evt-2026-000002"
    ]


def test_no_raw_article_body_is_published(tmp_path, conn):
    insert_event(conn, "evt-2026-000001")
    build_all(conn, tmp_path, now=NOW)
    assert all("raw_body" not in e for e in read(tmp_path, "live.json")["events"])


def test_a_kev_listed_cve_is_published_as_a_claim_citing_the_catalogue(tmp_path, conn):
    insert_event(conn, "evt-2026-000001")
    conn.execute(
        text(
            "insert into cves (cve_id, kev_listed, kev_date_added) "
            "values ('CVE-2026-88772', true, '2026-09-29')"
        )
    )
    conn.execute(text("insert into event_cves values ('evt-2026-000001', 'CVE-2026-88772')"))
    build_all(conn, tmp_path, now=NOW)
    (event,) = read(tmp_path, "live.json")["events"]
    assert [(c["text"], c["evidence"]) for c in event["claims"]] == [
        ("CISA lists CVE-2026-88772 as exploited in the wild, added 2026-09-29.", ["cisa_kev"])
    ]


def test_system_status_uses_last_completed_collection_not_live(tmp_path, conn):
    insert_run(conn)
    build_all(conn, tmp_path, now=NOW)
    s = read(tmp_path, "system-status.json")
    assert s["last_completed_collection"] == "2026-09-30T11:57:00Z"
    assert "live" not in json.dumps(s).lower()
    assert read(tmp_path, "live.json")["last_completed_collection"] == "2026-09-30T11:57:00Z"


def test_source_health_lists_registry_sources(tmp_path, conn):
    build_all(conn, tmp_path, now=NOW)
    ids = {s["source_id"] for s in read(tmp_path, "source-health.json")["sources"]}
    assert SOURCE in ids


def test_build_writes_nothing_when_a_secret_is_found(tmp_path, conn, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", OR_KEY)
    insert_event_with_summary(conn, f"contains {OR_KEY}")
    with pytest.raises(ValidationFailure, match="secret"):
        build_all(conn, tmp_path, now=NOW)
    assert list(tmp_path.iterdir()) == []  # fail closed: nothing written


def test_build_is_atomic_on_schema_failure(tmp_path, conn):
    (tmp_path / "live.json").write_text('{"previous": true}')
    # Transactional DDL: dropping the CHECK is rolled back with the fixture's transaction.
    conn.execute(text("alter table events drop constraint events_severity_check"))
    insert_event(conn, "evt-2026-000001", severity="spicy")
    with pytest.raises(ValidationFailure):
        build_all(conn, tmp_path, now=NOW)  # conn contains an invalid event
    assert json.loads((tmp_path / "live.json").read_text()) == {"previous": True}
    assert [p.name for p in tmp_path.iterdir()] == ["live.json"]
