"""worker/db/notifications.py against a real Postgres: claiming a message once, and the alert rule."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from worker.db.migrate import run_migrations
from worker.db.notifications import MAX_ATTEMPTS, claim, load_alert_events, settle
from worker.version import PIPELINE_VERSION, SCHEMA_VERSION, SCORING_VERSION

NOW = datetime(2026, 10, 3, 8, 0, tzinfo=UTC)
TABLES = ("notifications", "event_cves", "cves", "events")


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


def status(db, key):
    with db.connect() as conn:
        return conn.execute(
            text("select status, attempts, sent_at is not null, error from notifications "
                 "where dedupe_key = :k"),
            {"k": key},
        ).one()


def test_a_sent_message_is_never_claimed_again(db):
    first = claim(db, kind="daily_digest", key="daily_digest:2026-10-03", now=NOW)
    assert first is not None and first.attempt == 1
    assert claim(db, kind="daily_digest", key="daily_digest:2026-10-03", now=NOW) is None
    settle(db, first, "sent", now=NOW)
    assert tuple(status(db, "daily_digest:2026-10-03")) == ("sent", 1, True, None)
    assert claim(db, kind="daily_digest", key="daily_digest:2026-10-03", now=NOW) is None


def test_a_failed_send_is_claimed_again_until_the_attempts_run_out(db):
    key = "critical_au_alert:evt-2026-000001"
    for attempt in range(1, MAX_ATTEMPTS + 1):
        claimed = claim(db, kind="critical_au_alert", key=key, now=NOW,
                        event_id="evt-2026-000001")
        assert claimed is not None and claimed.attempt == attempt
        settle(db, claimed, "failed", now=NOW, error="HTTP 502")
    assert claim(db, kind="critical_au_alert", key=key, now=NOW) is None
    assert tuple(status(db, key)) == ("failed", MAX_ATTEMPTS, False, "HTTP 502")


def test_a_withheld_message_stays_withheld(db):
    claimed = claim(db, kind="daily_digest", key="daily_digest:2026-10-04", now=NOW)
    settle(db, claimed, "withheld", now=NOW, error="withheld by the secret scan")
    assert claim(db, kind="daily_digest", key="daily_digest:2026-10-04", now=NOW) is None


def test_an_unknown_kind_is_refused_by_the_table(db):
    with pytest.raises(Exception, match="check"):
        claim(db, kind="gossip", key="gossip:1", now=NOW)


def add_event(db, event_id, *, severity="critical", au=0.8, direct=False, status="new",
              first_seen=NOW - timedelta(hours=1), merged_into=None, kev=None):
    with db.begin() as conn:
        conn.execute(
            text(
                "insert into events (event_id, schema_version, pipeline_version, "
                "scoring_version, enrichment_version, first_seen, last_seen, "
                "last_material_update, status, severity, merged_into, title, summary, "
                "prominence, au_relevance, au_directly_reported) values (:id, :schema, "
                ":pipeline, :scoring, '1', :seen, :seen, :seen, :status, :severity, :merged, "
                "'t', 's', 0.5, :au, :direct)"
            ),
            {"id": event_id, "schema": SCHEMA_VERSION, "pipeline": PIPELINE_VERSION,
             "scoring": SCORING_VERSION, "seen": first_seen, "status": status,
             "severity": severity, "merged": merged_into, "au": au, "direct": direct},
        )
        if kev is not None:
            conn.execute(
                text("insert into cves (cve_id, kev_listed) values (:c, :listed) "
                     "on conflict do nothing"),
                {"c": kev[0], "listed": kev[1]},
            )
            conn.execute(
                text("insert into event_cves (event_id, cve_id) values (:e, :c)"),
                {"e": event_id, "c": kev[0]},
            )


def test_the_alert_rule(db):
    add_event(db, "evt-2026-000001")  # critical, AU 0.8: yes
    add_event(db, "evt-2026-000002", severity="high", au=None, direct=True,
              kev=("CVE-2026-0001", True))  # KEV, reported in AU: yes
    add_event(db, "evt-2026-000003", severity="high", kev=("CVE-2026-0002", False))  # no KEV
    add_event(db, "evt-2026-000004", au=0.5)  # not Australian enough
    add_event(db, "evt-2026-000005", status="archived")  # back catalogue
    add_event(db, "evt-2026-000006", first_seen=NOW - timedelta(hours=20))  # too old
    add_event(db, "evt-2026-000007", status="archived",
              merged_into="evt-2026-000001")  # merged away, which archives it
    with db.connect() as conn:
        events = load_alert_events(conn, since=NOW - timedelta(hours=12))
    assert [(e.event_id, e.kev_cves) for e in events] == [
        ("evt-2026-000001", ()),
        ("evt-2026-000002", ("CVE-2026-0001",)),
    ]
