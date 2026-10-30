"""worker/db/followup.py and the developing-update rule against a real Postgres: statuses from
the record, DECKARD's queue, and what a report may write."""

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from pydantic import SecretStr
from sqlalchemy import text

from worker.db.followup import (
    MAX_REPORT_ATTEMPTS,
    WORKER_NO_CHANGE,
    close_quiet_checks,
    count_accepted,
    count_due,
    load_due,
    load_new_since,
    load_queue_counts,
    schedule_followups,
    settle_statuses,
    submit_report,
)
from worker.db.migrate import run_migrations
from worker.db.notifications import load_update_entries
from worker.models import EventStatus, MaterialChange
from worker.ops_api import OpsApi
from worker.pipeline.run import settle
from worker.pipeline.status import FollowupConfig, Transition
from worker.version import PIPELINE_VERSION, SCHEMA_VERSION, SCORING_VERSION

NOW = datetime(2026, 10, 3, 8, 0, tzinfo=UTC)
CONFIG = FollowupConfig.load()
TABLES = (
    "notifications", "followup_tasks", "event_sources", "event_timeline", "event_cves", "cves",
    "events",
)
FEED = "followup-test-feed"
TOKEN = "fake-ops-token-TESTONLY-0123456789abcdef"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
URL = "https://www.cyber.gov.au/about-us/alerts/x"
SUMMARY = "Exploitation was confirmed against two hospitals in Victoria this week."
S = EventStatus


def clear(engine) -> None:
    with engine.begin() as conn:
        for table in TABLES:
            conn.execute(text(f"delete from {table}"))
        conn.execute(text("delete from source_registry where id = :id"), {"id": FEED})


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
            text("insert into event_timeline (event_id, ts, type, summary, sources, created_at) "
                 "values (:e, :ts, 'NEW_FACT', 'First reported by Somebody', '{feed}', :ts)"),
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


def add_task(db, event_id, *, kind="check", status="pending", at=NOW, outcome=None) -> int:
    """A task as the queue leaves it: `at` is when a closed one ended, or when one is due."""
    with db.begin() as conn:
        return conn.execute(
            text("insert into followup_tasks (event_id, kind, status, due_at, payload, "
                 "created_at, updated_at) values (:e, :kind, :status, :at, cast(:p as jsonb), "
                 ":at, :at) returning id"),
            {"e": event_id, "kind": kind, "status": status, "at": at,
             "p": json.dumps({"outcome": outcome} if outcome else {})},
        ).scalar_one()


def finish(db, event_id, outcome, *, at) -> None:
    """Close the event's open check as an accepted report does: done, with its outcome."""
    with db.begin() as conn:
        conn.execute(
            text("update followup_tasks set status = 'done', payload = cast(:p as jsonb), "
                 "updated_at = :at where event_id = :e and kind = 'check' and status = 'pending'"),
            {"e": event_id, "p": json.dumps({"outcome": outcome}), "at": at},
        )


def pending_due(db, event_id) -> datetime:
    with db.connect() as conn:
        return conn.execute(
            text("select due_at from followup_tasks where event_id = :e and status = 'pending'"),
            {"e": event_id},
        ).scalar_one()


QUIET = CONFIG.followup.model_copy(update={"max_backoff_doublings": 3})


def quiet_checks(db, event_id, *, days: list[int]) -> None:
    """A check on each of `days` (days ago, oldest first) that found nothing."""
    with db.begin() as conn:
        schedule_followups(conn, QUIET, now=ago(days=days[0] + 1))
    for day in days:
        finish(db, event_id, "no_change", at=ago(days=day))
        with db.begin() as conn:
            schedule_followups(conn, QUIET, now=ago(days=day))


def test_each_check_that_finds_nothing_doubles_the_wait_up_to_the_most(db):
    add_event(db, "evt-2026-000001", status="developing", severity="high",
              first_seen=ago(days=20))
    hours = QUIET.every_hours[S.DEVELOPING]["high"]
    with db.begin() as conn:
        schedule_followups(conn, QUIET, now=ago(days=11))
    waits = []
    for day in (10, 9, 8, 7, 6):
        finish(db, "evt-2026-000001", "no_change", at=ago(days=day))
        with db.begin() as conn:
            schedule_followups(conn, QUIET, now=ago(days=day))
        waits.append(pending_due(db, "evt-2026-000001") - ago(days=day))
    # 1 to 5 quiet checks in a row: twice, four and then eight times the cadence, no more.
    assert waits == [timedelta(hours=hours * n) for n in (2, 4, 8, 8, 8)]


def test_a_material_change_found_late_ends_the_back_off_and_brings_the_check_forward(db):
    add_event(db, "evt-2026-000001", status="developing", severity="high",
              first_seen=ago(days=20))
    hours = QUIET.every_hours[S.DEVELOPING]["high"]
    quiet_checks(db, "evt-2026-000001", days=[9, 8, 7])
    assert pending_due(db, "evt-2026-000001") == ago(days=7) + timedelta(hours=hours * 8)
    # A register finds a patch today that was dated before those checks.
    add_entry(db, "evt-2026-000001", "NEW_PATCH", at=ago(days=12), created_at=ago(hours=2))
    with db.begin() as conn:
        result = schedule_followups(conn, QUIET, now=NOW)
        again = schedule_followups(conn, QUIET, now=NOW)
    assert (result.brought_forward, again.brought_forward) == (1, 0)
    assert pending_due(db, "evt-2026-000001") == ago(days=7) + timedelta(hours=hours)


def test_a_changed_report_ends_the_back_off(db):
    add_event(db, "evt-2026-000001", status="developing", severity="high",
              first_seen=ago(days=20), confirmed=True)
    hours = QUIET.every_hours[S.DEVELOPING]["high"]
    quiet_checks(db, "evt-2026-000001", days=[9, 8])
    with db.connect() as conn:
        task_id = conn.execute(text(
            "select id from followup_tasks where status = 'pending'"
        )).scalar_one()
    done = report(db, task_id, {"outcome": "changed", "changes": [
        {"type": "NEW_TARGET", "summary": SUMMARY, "url": URL}]}, now=ago(days=7))
    assert done.result == "recorded"
    with db.begin() as conn:
        schedule_followups(conn, QUIET, now=ago(days=7))
    assert pending_due(db, "evt-2026-000001") == ago(days=7) + timedelta(hours=hours)


def test_another_outlet_repeating_the_story_keeps_the_back_off(db):
    add_event(db, "evt-2026-000001", status="developing", severity="high",
              first_seen=ago(days=20))
    hours = QUIET.every_hours[S.DEVELOPING]["high"]
    quiet_checks(db, "evt-2026-000001", days=[9, 8])
    add_entry(db, "evt-2026-000001", "NEW_EVIDENCE", at=ago(hours=2), created_at=ago(hours=2))
    add_entry(db, "evt-2026-000001", "NEW_FACT", at=ago(hours=1), created_at=ago(hours=1))
    with db.begin() as conn:
        assert schedule_followups(conn, QUIET, now=NOW).brought_forward == 0
    assert pending_due(db, "evt-2026-000001") == ago(days=8) + timedelta(hours=hours * 4)


def test_the_reports_accepted_in_a_window_are_counted(db):
    add_event(db, "evt-2026-000001", status="developing")
    add_event(db, "evt-2026-000002", status="resolved")
    add_task(db, "evt-2026-000001", status="done", at=ago(hours=30), outcome="no_change")
    add_task(db, "evt-2026-000001", status="done", at=ago(hours=20), outcome="no_change")
    add_task(db, "evt-2026-000001", status="failed", at=ago(hours=10))
    add_task(db, "evt-2026-000001", status="cancelled", at=ago(hours=9))
    add_task(db, "evt-2026-000002", kind="final_summary", status="done", at=ago(hours=2),
             outcome="summary")
    add_task(db, "evt-2026-000001", at=ago(hours=1))
    with db.connect() as conn:
        assert count_accepted(conn, since=ago(hours=24)) == 2
        assert count_accepted(conn, since=ago(hours=48)) == 3


def add_source(db, event_id, name, *, added):
    with db.begin() as conn:
        conn.execute(
            text("insert into source_registry (id, name, type, region, category, "
                 "source_class, priority, lane, enabled, url, parser, expected_frequency) "
                 "values (:id, :id, 'rss', 'AU', 'news', 'NEWS', 1, 'fast', true, "
                 "'https://example.org/feed', 'feed', 'daily') on conflict (id) do nothing"),
            {"id": FEED},
        )
        conn.execute(
            text("insert into event_sources (event_id, source_id, url, published, "
                 "evidence_class, url_hash, created_at) values (:e, :s, :url, :at, 'NEWS', "
                 ":hash, :at)"),
            {"e": event_id, "s": FEED, "url": f"https://example.org/{name}", "at": added,
             "hash": f"{event_id}-{name}"},
        )


def test_a_due_check_says_what_its_event_gained_since_the_last_one(db):
    add_event(db, "evt-2026-000001", status="developing", severity="high",
              first_seen=ago(days=5))
    add_event(db, "evt-2026-000002", status="developing", severity="critical",
              first_seen=ago(days=5))
    add_event(db, "evt-2026-000003", status="resolved", severity="critical",
              first_seen=ago(days=40))
    add_task(db, "evt-2026-000001", status="done", at=ago(hours=30), outcome="no_change")
    add_task(db, "evt-2026-000001", status="failed", at=ago(hours=10))  # the last check
    add_task(db, "evt-2026-000003", status="done", at=ago(days=20), outcome="no_change")
    checked = add_task(db, "evt-2026-000001", at=ago(hours=1))
    add_task(db, "evt-2026-000002", at=ago(hours=1))  # never checked before
    add_task(db, "evt-2026-000003", kind="final_summary", at=ago(hours=1))
    add_source(db, "evt-2026-000001", "before", added=ago(hours=12))
    for n in range(4):
        add_source(db, "evt-2026-000001", f"after-{n}", added=ago(hours=9 - n))
    add_source(db, "evt-2026-000002", "other", added=ago(hours=2))
    # A register's patch dated last week, written today: new. One written before: not.
    add_entry(db, "evt-2026-000001", "NEW_PATCH", at=ago(days=7), created_at=ago(hours=3))
    add_entry(db, "evt-2026-000001", "NEW_TARGET", at=ago(hours=11), created_at=ago(hours=11))

    with db.connect() as conn:
        due = load_due(conn, now=NOW, limit=10)
        gained = load_new_since(conn, due, most=3)
    last = {t.event_id: t.last_check_at for t in due}
    assert last == {"evt-2026-000001": ago(hours=10), "evt-2026-000002": None,
                    "evt-2026-000003": ago(days=20)}
    # Only a check on an event checked before: not a first check, not a final summary.
    assert list(gained) == [checked]
    new = gained[checked]
    assert (new.since, new.sources, new.timeline) == (ago(hours=10), 4, 1)
    assert [s.url for s in new.newest_sources] == [
        f"https://example.org/after-{n}" for n in (3, 2, 1)
    ]
    assert [t.type for t in new.newest_timeline] == [MaterialChange.NEW_PATCH]


def test_an_event_that_gained_nothing_since_its_last_check_says_so(db):
    add_event(db, "evt-2026-000001", status="developing", severity="high",
              first_seen=ago(days=5))
    add_task(db, "evt-2026-000001", status="done", at=ago(hours=10), outcome="no_change")
    task_id = add_task(db, "evt-2026-000001", at=ago(hours=1))
    add_source(db, "evt-2026-000001", "before", added=ago(hours=12))
    with db.connect() as conn:
        gained = load_new_since(conn, load_due(conn, now=NOW, limit=10), most=3)
    new = gained[task_id]
    assert (new.sources, new.timeline, new.newest_sources, new.newest_timeline) == (0, 0, (), ())


def task_row(db, task_id) -> dict:
    with db.connect() as conn:
        return dict(conn.execute(
            text("select status, attempts, payload, updated_at from followup_tasks where id = :id"),
            {"id": task_id},
        ).mappings().one())


def test_the_worker_records_no_change_for_a_due_check_with_nothing_new(db):
    """W3: what DECKARD's skill told it to do without a read, the worker does itself, so the
    Follow-up routine is fired only for tasks with something to look at."""
    def checked(event_id, **event):
        add_event(db, event_id, status="developing", severity="high", first_seen=ago(days=5),
                  **event)
        add_task(db, event_id, status="done", at=ago(hours=10), outcome="no_change")
        return add_task(db, event_id, at=ago(hours=1))

    quiet = checked("evt-2026-000001")
    news = checked("evt-2026-000002")
    add_source(db, "evt-2026-000002", "after", added=ago(hours=5))
    entry = checked("evt-2026-000003")
    add_entry(db, "evt-2026-000003", "NEW_PATCH", at=ago(days=7), created_at=ago(hours=3))
    merged = checked("evt-2026-000004", lmu=ago(hours=2))  # a merge moved it, no entry
    add_event(db, "evt-2026-000005", status="developing")
    first = add_task(db, "evt-2026-000005", at=ago(hours=1))  # never checked: a first look
    add_event(db, "evt-2026-000006", status="developing", first_seen=ago(days=5))
    add_task(db, "evt-2026-000006", status="done", at=ago(hours=10), outcome="no_change")
    later = add_task(db, "evt-2026-000006", at=NOW + timedelta(hours=2))  # not due yet

    with db.begin() as conn:
        assert close_quiet_checks(conn, now=NOW) == ["evt-2026-000001"]
    row = task_row(db, quiet)
    assert (row["status"], row["attempts"], row["updated_at"]) == ("done", 1, NOW)
    assert row["payload"] == WORKER_NO_CHANGE and row["payload"]["by"] == "worker"
    for task_id in (news, entry, merged, first, later):
        assert task_row(db, task_id)["status"] == "pending"
    with db.begin() as conn:
        assert close_quiet_checks(conn, now=NOW) == []  # nothing twice

    with db.connect() as conn:
        # The four due tasks left have something to look at. DECKARD's cap counts its five
        # earlier reports, not the worker's own record, which cost no tokens.
        assert count_due(conn, now=NOW) == 4
        assert count_accepted(conn, since=ago(hours=24)) == 5
        assert {t.event_id for t in load_due(conn, now=NOW, limit=10)} == {
            "evt-2026-000002", "evt-2026-000003", "evt-2026-000004", "evt-2026-000005",
        }


def test_a_worker_no_change_backs_off_the_next_check_like_deckard_s(db):
    add_event(db, "evt-2026-000001", status="developing", severity="high", first_seen=ago(days=5))
    add_task(db, "evt-2026-000001", status="done", at=ago(hours=13), outcome="no_change")
    add_task(db, "evt-2026-000001", at=ago(hours=1))
    with db.begin() as conn:
        close_quiet_checks(conn, now=NOW)
        schedule_followups(conn, CONFIG.followup, now=NOW)
    # high + developing is every 12 hours; two quiet checks since the last change: x4.
    assert pending_due(db, "evt-2026-000001") == NOW + timedelta(hours=48)


def ops(db, tmp_path, **changes) -> OpsApi:
    config = CONFIG.model_copy(update={"followup": CONFIG.followup.model_copy(update=changes)})
    return OpsApi(engine=db, token=SecretStr(TOKEN), data_dir=tmp_path, key_status=lambda: None,
                  monthly_budget=Decimal(20), followup=config, clock=lambda: NOW)


def test_the_queue_hands_out_one_batch_with_what_is_new_and_stops_at_the_day_s_cap(db, tmp_path):
    for n in range(1, 5):
        add_event(db, f"evt-2026-00000{n}", status="developing")
    # Checked five hours ago, and nothing since: one report accepted today.
    add_task(db, "evt-2026-000001", status="done", at=ago(hours=5), outcome="no_change")
    add_task(db, "evt-2026-000001", at=ago(hours=2))
    for n in range(2, 5):
        add_task(db, f"evt-2026-00000{n}", at=ago(hours=1))

    response = ops(db, tmp_path, batch=3, per_day=4).handle("GET", "/ops/followup", AUTH)
    assert response.status == 200, response.json()
    body = response.json()
    assert body["cap"] == {"batch": 3, "per_day": 4, "accepted_last_24h": 1}
    tasks = {t["event"]["event_id"]: t for t in body["tasks"]}
    assert list(tasks) == ["evt-2026-000001", "evt-2026-000002", "evt-2026-000003"]
    assert tasks["evt-2026-000001"]["nothing_new"] is True
    assert tasks["evt-2026-000001"]["new_since_last_check"]["since"] == "2026-10-03T03:00:00Z"
    assert tasks["evt-2026-000002"]["nothing_new"] is False
    assert tasks["evt-2026-000002"]["new_since_last_check"] is None

    full = ops(db, tmp_path, batch=3, per_day=1).handle("GET", "/ops/followup", AUTH).json()
    assert full["tasks"] == [] and full["queue"]["due"] == 4


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
