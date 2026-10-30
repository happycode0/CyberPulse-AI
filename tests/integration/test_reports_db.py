"""worker/db/reports.py against a real Postgres: what the daily report reads, and how it is kept."""

from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import text

from worker.db.migrate import run_migrations
from worker.db.reports import (
    QUIET_DAYS,
    load_latest_report,
    load_report_input,
    report_exists,
    store_report,
)

NOW = datetime(2026, 10, 31, 20, 30, tzinfo=UTC)
TABLES = ("daily_reports", "event_cves", "events")


def clear(engine) -> None:
    with engine.begin() as conn:
        for table in TABLES:
            conn.execute(text(f"delete from {table}"))


@pytest.fixture
def db(pg_engine):
    run_migrations(pg_engine)
    clear(pg_engine)
    yield pg_engine
    clear(pg_engine)


def event(engine, n, *, first_seen, prominence=0.5, severity="medium", au=0.0, categories=(),
          sectors=(), status="active", title=None) -> str:
    event_id = f"evt-2026-{n:06d}"
    with engine.begin() as conn:
        conn.execute(
            text(
                "insert into events (event_id, schema_version, pipeline_version, scoring_version, "
                "enrichment_version, title, summary, why_it_matters, first_seen, last_seen, "
                "prominence, severity, au_relevance, categories, au_sectors, status) values "
                "(:id, '1', '1.0.0', '4', '1', :title, :summary, :why, :t, :t, :p, :sev, :au, "
                "cast(:cats as text[]), cast(:sectors as text[]), :status)"
            ),
            {"id": event_id, "title": title or f"Story {n}", "summary": f"Summary of story {n}.",
             "why": f"Why story {n} matters.", "t": first_seen, "p": prominence, "sev": severity,
             "au": au, "cats": list(categories), "sectors": list(sectors), "status": status},
        )
    return event_id


def test_the_input_is_the_days_prominent_and_australian_events(db):
    fresh = event(db, 1, first_seen=NOW - timedelta(hours=3), prominence=0.9, severity="high",
                  categories=("ransomware",), sectors=("health",), au=0.8)
    critical = event(db, 2, first_seen=NOW - timedelta(hours=5), prominence=0.2,
                     severity="critical")
    event(db, 3, first_seen=NOW - timedelta(days=3), prominence=0.95)  # outside the window
    with db.connect() as conn:
        given = load_report_input(conn, now=NOW, hours=24)
    ids = [e["event_id"] for e in given.events]
    assert ids[:2] == [fresh, critical] or set(ids) >= {fresh, critical}
    assert "evt-2026-000003" not in ids
    one = next(e for e in given.events if e["event_id"] == fresh)
    assert one["title"] == "Story 1" and one["summary"] == "Summary of story 1."
    assert one["categories"] == ["ransomware"] and one["au_sectors"] == ["health"]
    assert one["severity"] == "high" and one["au_relevance"] == 0.8 and one["kev"] is False


def test_a_quiet_topic_is_one_with_a_record_and_nothing_new_for_a_week(db):
    # DDoS: three stories a month ago, nothing since. Ransomware: one today.
    for n, days in enumerate((30, 33, 40), start=10):
        event(db, n, first_seen=NOW - timedelta(days=days), categories=("ddos",))
    event(db, 20, first_seen=NOW - timedelta(hours=2), categories=("ransomware",))
    # The Australian health sector: three AU stories two weeks ago.
    for n, days in enumerate((14, 15, 16), start=30):
        event(db, n, first_seen=NOW - timedelta(days=days), sectors=("health",), au=0.8)
    # One old phishing story is not a record.
    event(db, 40, first_seen=NOW - timedelta(days=20), categories=("phishing",))
    with db.connect() as conn:
        gaps = load_report_input(conn, now=NOW, hours=24).gaps
    by_id = {g["id"]: g for g in gaps}
    assert set(by_id) == {"category:ddos", "au_sector:health"}
    assert by_id["category:ddos"]["quiet_days"] == pytest.approx(30, abs=0.1)
    assert by_id["au_sector:health"]["quiet_days"] >= QUIET_DAYS


def test_a_report_is_kept_once_per_date_and_the_latest_is_read(db):
    with db.begin() as conn:
        assert not report_exists(conn, date(2026, 11, 1))
        store_report(conn, report_date=date(2026, 10, 31), generated_at=NOW - timedelta(days=1),
                     window_hours=24, summary="Yesterday.", items=[], gaps=[], model="m")
        store_report(conn, report_date=date(2026, 11, 1), generated_at=NOW, window_hours=24,
                     summary="Today.", items=[{"event_id": "evt-2026-000001", "why": "w"}],
                     gaps=[{"id": "category:ddos", "topic": "ddos", "kind": "category",
                            "quiet_days": 30.0}], model="m")
        assert report_exists(conn, date(2026, 11, 1))
        # A second report for the same date is not kept: the first one stands.
        assert not store_report(conn, report_date=date(2026, 11, 1), generated_at=NOW,
                                window_hours=24, summary="Again.", items=[], gaps=[], model="m")
    with db.connect() as conn:
        latest = load_latest_report(conn)
    assert latest["report_date"] == date(2026, 11, 1) and latest["summary"] == "Today."
    assert latest["items"] == [{"event_id": "evt-2026-000001", "why": "w"}]
    assert latest["gaps"][0]["quiet_days"] == 30.0 and latest["model"] == "m"


def test_no_report_yet_is_none(db):
    with db.connect() as conn:
        assert load_latest_report(conn) is None
