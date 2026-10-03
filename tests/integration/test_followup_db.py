"""worker/db/followup.py and the developing-update rule against a real Postgres: statuses from
the record, DECKARD's queue, and what a report may write."""

import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from worker.db.followup import (
    MAX_REPORT_ATTEMPTS,
    load_due,
    load_queue_counts,
    schedule_followups,
    settle_statuses,
    submit_report,
)
from worker.db.migrate import run_migrations
from worker.db.notifications import load_update_entries
from worker.models import EventStatus
from worker.pipeline.run import settle
from worker.pipeline.status import FollowupConfig, Transition
from worker.version import PIPELINE_VERSION, SCHEMA_VERSION, SCORING_VERSION

NOW = datetime(2026, 10, 3, 8, 0, tzinfo=UTC)
CONFIG = FollowupConfig.load()
TABLES = ("notifications", "followup_tasks", "event_timeline", "event_cves", "cves", "events")
URL = "https://www.cyber.gov.au/about-us/alerts/x"
SUMMARY = "Exploitation was confirmed against two hospitals in Victoria this week."
S = EventStatus


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


def ago(**delta) -> datetime:
    return NOW - timedelta(**delta)


def add_event(db, event_id, *, status="new", severity="critical", first_seen=None, lmu=None,
              confirmed=False, merged_into=None, au=0.9, direct=False, kev=None):
    first_seen = first_seen or ago(days=1)
    with db.begin() as conn:
        conn.execute(
            text(
                "insert into events (event_id, schema_version, pipeline_version, "
                "scoring_version, enrichment_version, first_seen, last_seen, "
                "last_material_update, last_independent_confirmation, status, severity, "
                "merged_into, title, summary, prominence, au_relevance, au_directly_reported) "
                "values (:id, :schema, :pipeline, :scoring, '1', :seen, :seen, :lmu, :conf, "
                ":status, :severity, :merged, 'A title', 'A summary', 0.5, :au, :direct)"
            ),
            {"id": event_id, "schema": SCHEMA_VERSION, "pipeline": PIPELINE_VERSION,
             "scoring": SCORING_VERSION, "seen": first_seen, "lmu": lmu or first_seen,
             "conf": first_seen if confirmed else None, "status": status, "severity": severity,
             "merged": merged_into, "au": au, "direct": direct},
        )
        conn.execute(
            text("insert into event_timeline (event_id, ts, type, summary, sources) "
                 "values (:e, :ts, 'NEW_FACT', 'First reported by Somebody', '{feed}')"),
            {"e": event_id, "ts": first_seen},
        )
        if kev is not None:
            conn.execute(
                text("insert into cves (cve_id, kev_listed) values (:c, true) "
                     "on conflict do nothing"),
                {"c": kev},
            )
            conn.execute(text("insert into event_cves (event_id, cve_id) values (:e, :c)"),
                         {"e": event_id, "c": kev})


def add_entry(db, event_id, type, *, at, created_at=None, sources=("feed",), summary=None):
    with db.begin() as conn:
        return conn.execute(
            text("insert into event_timeline (event_id, ts, type, summary, sources, created_at) "
                 "values (:e, :ts, :type, :summary, cast(:sources as text[]), :created) "
                 "returning id"),
            {"e": event_id, "ts": at, "type": type, "summary": summary or f"{type} at {at}",
             "sources": list(sources), "created": created_at},
        ).scalar_one()


def statuses(db) -> dict[str, str]:
    with db.connect() as conn:
        return dict(conn.execute(text("select event_id, status from events")).all())


def tasks(db) -> list[tuple]:
    with db.connect() as conn:
        return [tuple(r) for r in conn.execute(text(
            "select event_id, kind, status, due_at, attempts from followup_tasks order by id"
        ))]


# --- Status ------------------------------------------------------------------------------------


def test_each_standing_event_gets_the_status_its_record_says(db):
    add_event(db, "evt-2026-000001", confirmed=True)
    add_event(db, "evt-2026-000002", lmu=ago(days=1))
    add_entry(db, "evt-2026-000002", "NEW_EXPLOIT", at=ago(days=1))
    add_event(db, "evt-2026-000003", status="developing", first_seen=ago(days=30),
              lmu=ago(days=20))
    add_entry(db, "evt-2026-000003", "NEW_PATCH", at=ago(days=20))
    add_event(db, "evt-2026-000004", status="archived")
    add_entry(db, "evt-2026-000004", "NEW_EXPLOIT", at=ago(hours=1))
    add_event(db, "evt-2026-000005", status="archived", merged_into="evt-2026-000001")
    add_event(db, "evt-2026-000006")
    add_entry(db, "evt-2026-000006", "NEW_FACT", at=ago(hours=2), sources=("deckard", URL))
    add_event(db, "evt-2026-000007")
    add_entry(db, "evt-2026-000007", "NEW_FACT", at=ago(hours=2))  # another source's report
    add_entry(db, "evt-2026-000007", "NEW_EVIDENCE", at=ago(hours=1))

    with db.begin() as conn:
        moved = settle_statuses(conn, CONFIG.status, now=NOW)
    assert sorted(moved, key=lambda t: t.event_id) == [
        Transition("evt-2026-000001", S.NEW, S.ACTIVE),
        Transition("evt-2026-000002", S.NEW, S.DEVELOPING),
        Transition("evt-2026-000003", S.DEVELOPING, S.RESOLVED),
        Transition("evt-2026-000006", S.NEW, S.DEVELOPING),
    ]
    assert statuses(db)["evt-2026-000004"] == "archived"
    assert statuses(db)["evt-2026-000007"] == "new"
    with db.begin() as conn:
        assert settle_statuses(conn, CONFIG.status, now=NOW) == []


# --- The queue ---------------------------------------------------------------------------------


def test_followed_up_events_get_one_check_each_and_resolved_ones_a_final_summary(db):
    add_event(db, "evt-2026-000001", status="developing", severity="critical")
    add_event(db, "evt-2026-000002", status="monitoring", severity="medium")
    add_event(db, "evt-2026-000003", status="developing", severity="low")  # not followed up
    add_event(db, "evt-2026-000004", status="active", severity="critical")  # not followed up
    add_event(db, "evt-2026-000005", status="resolved", severity="critical")
    add_event(db, "evt-2026-000006", status="resolved", severity="medium")  # no final summary
    add_event(db, "evt-2026-000007", status="archived", severity="critical",
              merged_into="evt-2026-000001")

    with db.begin() as conn:
        first = schedule_followups(conn, CONFIG.followup, now=NOW)
        again = schedule_followups(conn, CONFIG.followup, now=NOW)
    assert first.opened == ["evt-2026-000001", "evt-2026-000002", "evt-2026-000005"]
    assert (again.opened, again.cancelled) == ([], 0)
    assert [(e, k, s) for e, k, s, _, _ in tasks(db)] == [
        ("evt-2026-000001", "check", "pending"),
        ("evt-2026-000002", "check", "pending"),
        ("evt-2026-000005", "final_summary", "pending"),
    ]
    with db.connect() as conn:
        due = load_due(conn, now=NOW, limit=10)
        counts = load_queue_counts(conn, now=NOW, overdue_after=24)
    # Critical first; the final summary's event is critical too.
    assert [t.event_id for t in due][-1] == "evt-2026-000002"
    assert (counts["due"], counts["waiting"], counts["overdue"]) == (3, 0, 0)
    assert counts["statuses"]["developing"] == 2 and counts["statuses"]["contained"] == 0


def test_the_next_check_is_due_its_cadence_after_the_last(db):
    add_event(db, "evt-2026-000001", status="developing", severity="high")
    with db.begin() as conn:
        schedule_followups(conn, CONFIG.followup, now=NOW)
        conn.execute(text("update followup_tasks set status = 'done', updated_at = :t"),
                     {"t": ago(hours=1)})
        schedule_followups(conn, CONFIG.followup, now=NOW)
    hours = CONFIG.followup.every_hours[S.DEVELOPING]["high"]
    [_, (_, _, status, due_at, _)] = tasks(db)
    assert status == "pending" and due_at == ago(hours=1) + timedelta(hours=hours)
    with db.connect() as conn:
        assert load_due(conn, now=NOW, limit=10) == []
        assert load_queue_counts(conn, now=NOW, overdue_after=24)["waiting"] == 1


def test_a_task_is_cancelled_when_its_event_no_longer_needs_it(db):
    add_event(db, "evt-2026-000001", status="developing")
    add_event(db, "evt-2026-000002", status="developing")
    add_event(db, "evt-2026-000003", status="resolved")
    with db.begin() as conn:
        schedule_followups(conn, CONFIG.followup, now=NOW)
        conn.execute(text("update events set status = 'contained' "
                          "where event_id = 'evt-2026-000001'"))
        conn.execute(text("update events set status = 'archived', "
                          "merged_into = 'evt-2026-000001' where event_id = 'evt-2026-000002'"))
        conn.execute(text("update events set resolution = 'Done and dusted, all of it.' "
                          "where event_id = 'evt-2026-000003'"))
        result = schedule_followups(conn, CONFIG.followup, now=NOW)
    assert (result.opened, result.cancelled) == ([], 3)
    assert {s for _, _, s, _, _ in tasks(db)} == {"cancelled"}


def test_settle_runs_both_under_the_lock_and_reports_rather_than_raises(db):
    add_event(db, "evt-2026-000001", lmu=ago(hours=2))
    add_entry(db, "evt-2026-000001", "NEW_EXPLOIT", at=ago(hours=2))
    moved, errors = settle(db, CONFIG, now=NOW)
    assert moved == [Transition("evt-2026-000001", S.NEW, S.DEVELOPING)] and errors == []
    assert [(e, k) for e, k, *_ in tasks(db)] == [("evt-2026-000001", "check")]


# --- Reports -----------------------------------------------------------------------------------


def open_check(db, event_id="evt-2026-000001", **event) -> int:
    add_event(db, event_id, **{"status": "active", "confirmed": True, **event})
    with db.begin() as conn:
        return conn.execute(
            text("insert into followup_tasks (event_id, kind, due_at) "
                 "values (:e, 'check', :now) returning id"),
            {"e": event_id, "now": NOW},
        ).scalar_one()


def report(db, task_id, body, *, now=NOW):
    with db.begin() as conn:
        return submit_report(conn, task_id, body, rule=CONFIG.status, now=now)


def test_a_change_is_written_to_the_timeline_and_moves_the_status(db):
    task_id = open_check(db)
    done = report(db, task_id, {"outcome": "changed", "changes": [
        {"type": "EXPLOIT_CONFIRMED", "summary": SUMMARY, "url": URL + "?ref=x"},
        {"type": "NO_MATERIAL_CHANGE", "summary": SUMMARY, "url": URL},
    ]})
    assert done.result == "recorded" and done.task_status == "done" and done.attempts == 1
    assert done.transition == Transition("evt-2026-000001", S.ACTIVE, S.DEVELOPING)
    with db.connect() as conn:
        entry = conn.execute(text(
            "select ts, type, summary, sources, created_at from event_timeline "
            "where event_id = 'evt-2026-000001' and type = 'EXPLOIT_CONFIRMED'"
        )).one()
        lmu = conn.execute(text("select last_material_update from events")).scalar_one()
        payload = conn.execute(text("select payload from followup_tasks")).scalar_one()
    assert tuple(entry) == (NOW, "EXPLOIT_CONFIRMED", SUMMARY, ["deckard", URL], NOW)
    assert lmu == NOW
    assert payload["outcome"] == "changed" and payload["refused"][0]["index"] == 1
    assert json.dumps(payload).count(URL) == 1


def test_no_change_closes_the_task_and_writes_nothing_else(db):
    task_id = open_check(db)
    done = report(db, task_id, {"outcome": "no_change"})
    assert (done.result, done.transition) == ("recorded", None)
    with db.connect() as conn:
        assert conn.execute(text("select count(*) from event_timeline")).scalar_one() == 1
        assert conn.execute(text("select last_material_update from events")).scalar_one() \
            == ago(days=1)


def test_a_refused_report_counts_an_attempt_until_the_task_is_given_up(db):
    task_id = open_check(db)
    bad = {"outcome": "changed", "changes": [
        {"type": "NEW_TARGET", "summary": "Too short.", "url": URL}]}
    for attempt in range(1, MAX_REPORT_ATTEMPTS + 1):
        done = report(db, task_id, bad)
        assert done.result == "rejected" and done.attempts == attempt
        assert "20 to 280" in done.message
    assert done.task_status == "failed"
    assert report(db, task_id, {"outcome": "no_change"}).result == "closed"
    with db.connect() as conn:
        assert conn.execute(text("select count(*) from event_timeline")).scalar_one() == 1


def test_reports_on_tasks_that_are_gone(db):
    assert report(db, 999_999, {"outcome": "no_change"}).result == "not_found"
    task_id = open_check(db)
    assert report(db, task_id, {"outcome": "no_change"}).result == "recorded"
    assert report(db, task_id, {"outcome": "no_change"}).result == "closed"

    merged = open_check(db, "evt-2026-000002")
    with db.begin() as conn:
        conn.execute(text("update events set status = 'archived', "
                          "merged_into = 'evt-2026-000001' where event_id = 'evt-2026-000002'"))
    done = report(db, merged, {"outcome": "no_change"})
    assert done.result == "closed" and "merged into evt-2026-000001" in done.message
    assert tasks(db)[-1][2] == "cancelled"


def test_a_final_summary_is_stored_once(db):
    add_event(db, "evt-2026-000001", status="resolved", first_seen=ago(days=30),
              lmu=ago(days=20))
    add_entry(db, "evt-2026-000001", "NEW_PATCH", at=ago(days=20))
    with db.begin() as conn:
        schedule_followups(conn, CONFIG.followup, now=NOW)
    [(_, kind, _, _, _)] = tasks(db)
    with db.connect() as conn:
        task_id = conn.execute(text("select id from followup_tasks")).scalar_one()
    text_ = ("A ransomware crew exploited a flaw in a records system used by hospitals. "
             "The vendor patched it and no new victims have been reported since.")
    assert kind == "final_summary"
    assert report(db, task_id, {"outcome": "no_change"}).result == "rejected"
    done = report(db, task_id, {"outcome": "summary", "summary": text_})
    assert done.result == "recorded" and done.transition is None
    with db.begin() as conn:
        assert conn.execute(text("select resolution from events")).scalar_one() == text_
        assert schedule_followups(conn, CONFIG.followup, now=NOW).opened == []
    assert statuses(db) == {"evt-2026-000001": "resolved"}


# --- Developing updates ------------------------------------------------------------------------


def test_the_developing_update_rule(db):
    since = ago(hours=12)
    add_event(db, "evt-2026-000001", first_seen=ago(days=1))  # critical, AU 0.9
    patch = add_entry(db, "evt-2026-000001", "NEW_PATCH", at=ago(hours=3), created_at=ago(hours=1))
    deckard = add_entry(db, "evt-2026-000001", "NEW_FACT", at=ago(minutes=30),
                        created_at=ago(minutes=30), sources=("deckard", URL))
    add_entry(db, "evt-2026-000001", "NEW_FACT", at=ago(minutes=20), created_at=ago(minutes=20))
    add_entry(db, "evt-2026-000001", "NEW_EVIDENCE", at=ago(minutes=10),
              created_at=ago(minutes=10))
    add_entry(db, "evt-2026-000001", "NEW_EXPLOIT", at=ago(hours=2))  # before migration 012
    add_entry(db, "evt-2026-000001", "NEW_TARGET", at=ago(hours=20), created_at=ago(hours=13))
    # Its first hour is the alert's.
    add_event(db, "evt-2026-000002", first_seen=ago(minutes=40))
    add_entry(db, "evt-2026-000002", "NEW_EXPLOIT", at=ago(minutes=5), created_at=ago(minutes=5))
    # Newly Australian, and high.
    add_event(db, "evt-2026-000003", severity="high", au=0.2)
    exposure = add_entry(db, "evt-2026-000003", "NEW_AU_EXPOSURE", at=ago(hours=2),
                         created_at=ago(hours=2))
    add_entry(db, "evt-2026-000003", "NEW_PATCH", at=ago(hours=2), created_at=ago(hours=2))
    # Medium, newly Australian: no.
    add_event(db, "evt-2026-000004", severity="medium")
    add_entry(db, "evt-2026-000004", "NEW_AU_EXPOSURE", at=ago(hours=2), created_at=ago(hours=2))
    # KEV-listed and reported in Australia.
    add_event(db, "evt-2026-000005", severity="high", au=None, direct=True, kev="CVE-2026-0001")
    kev = add_entry(db, "evt-2026-000005", "NEW_MITIGATION", at=ago(hours=4),
                    created_at=ago(hours=4))
    # Archived.
    add_event(db, "evt-2026-000006", status="archived")
    add_entry(db, "evt-2026-000006", "NEW_PATCH", at=ago(hours=2), created_at=ago(hours=2))

    with db.connect() as conn:
        entries = load_update_entries(conn, since=since)
    assert [e.entry_id for e in entries] == [kev, exposure, patch, deckard]
    assert entries[-1].link == URL and entries[0].link is None
