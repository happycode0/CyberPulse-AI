"""The ops API against a real Postgres: job_runs, the verdicts' SQL and the read endpoints.

The API opens its own connections, so rows are committed here and the tables it reads are
cleared before and after each test, as tests/integration/test_run.py does.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from worker.ai.budget import KeyStatus
from worker.db.jobs import JobRun, load_latest_completed_job, load_latest_job, record_job
from worker.db.migrate import run_migrations
from worker.ops_api import OpsApi
from worker.version import PIPELINE_VERSION, SCHEMA_VERSION, SCORING_VERSION

NOW = datetime(2026, 10, 3, 8, 0, tzinfo=UTC)
TOKEN = "fake-ops-token-TESTONLY-0123456789abcdef"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
TABLES = (
    "job_runs", "cost_ledger", "events", "source_health", "source_fetch_state", "runs",
    "source_lineage", "source_registry",
)


def clear(engine) -> None:
    with engine.begin() as conn:
        for table in TABLES:
            conn.execute(text(f"delete from {table}"))


@pytest.fixture
def db(pg_engine):
    run_migrations(pg_engine)
    clear(pg_engine)
    with pg_engine.begin() as conn:
        for sid, lane, enabled in (
            ("ops-fast", "fast", True), ("ops-normal", "normal", True), ("ops-off", "fast", False)
        ):
            conn.execute(
                text(
                    "insert into source_registry (id, name, type, region, category, "
                    "source_class, priority, lane, enabled, url, parser, expected_frequency) "
                    "values (:id, :id, 'rss', 'AU', 'advisory', 'AUTHORITATIVE', 1, :lane, "
                    ":enabled, 'https://example.org/feed', 'feed', 'daily')"
                ),
                {"id": sid, "lane": lane, "enabled": enabled},
            )
    yield pg_engine
    clear(pg_engine)


@pytest.fixture
def api(db, tmp_path):
    return OpsApi(
        engine=db,
        token=SecretStr(TOKEN),
        data_dir=tmp_path,
        key_status=lambda: KeyStatus(
            limit=Decimal(20),
            limit_remaining=Decimal("4.64"),
            limit_reset="monthly",
            usage_daily=Decimal("0.5"),
            usage_monthly=Decimal("15.36"),
            free_requests_remaining=None,
        ),
        monthly_budget=Decimal(20),
        clock=lambda: NOW,
    )


def get(api, target):
    response = api.handle("GET", target, AUTH)
    assert response.status == 200, response.json()
    return response.json()


def add_event(db, event_id, *, first_seen, lmu=None, status="new", severity="unknown",
              prominence=0.5, au=None, title="t", merged_into=None, updated_at=None,
              source=None):
    with db.begin() as conn:
        conn.execute(
            text(
                "insert into events (event_id, schema_version, pipeline_version, "
                "scoring_version, enrichment_version, first_seen, last_seen, "
                "last_material_update, status, severity, merged_into, title, summary, "
                "prominence, au_relevance, updated_at) values (:id, :schema, :pipeline, "
                ":scoring, '1', :first_seen, :first_seen, :lmu, :status, :severity, "
                ":merged_into, :title, 's', :prominence, :au, coalesce(:updated_at, now()))"
            ),
            {
                "id": event_id, "schema": SCHEMA_VERSION, "pipeline": PIPELINE_VERSION,
                "scoring": SCORING_VERSION, "first_seen": first_seen, "lmu": lmu or first_seen,
                "status": status, "severity": severity, "merged_into": merged_into,
                "title": title, "prominence": prominence, "au": au, "updated_at": updated_at,
            },
        )
        if source:
            conn.execute(
                text(
                    "insert into event_sources (event_id, source_id, url, title, published, "
                    "evidence_class, url_hash) values (:e, :s, :url, :title, :at, "
                    "'AUTHORITATIVE', :hash)"
                ),
                {"e": event_id, "s": source, "url": f"https://example.org/{event_id}",
                 "title": title, "at": first_seen, "hash": f"hash-{event_id}"},
            )


def add_health(db, source_id, status, at, error=None):
    with db.begin() as conn:
        conn.execute(
            text(
                "insert into source_health (source_id, checked_at, status, error) "
                "values (:s, :at, :status, :error)"
            ),
            {"s": source_id, "at": at, "status": status, "error": error},
        )


def add_run(db, run_id, lane, started, finished, errors=(), **counts):
    columns = ", ".join(counts)
    values = ", ".join(f":{c}" for c in counts)
    with db.begin() as conn:
        conn.execute(
            text(
                f"insert into runs (run_id, lane, started_at, finished_at, errors"
                f"{', ' + columns if counts else ''}) values (:run_id, :lane, :started, "
                f":finished, :errors{', ' + values if counts else ''})"
            ),
            {"run_id": run_id, "lane": lane, "started": started, "finished": finished,
             "errors": list(errors), **counts},
        )


def add_cost(db, ts, cost, *, stage="brief", model="m/cheap", requested=None, agent=None,
             outcome="ok"):
    with db.begin() as conn:
        conn.execute(
            text(
                "insert into cost_ledger (ts, provider, model, requested_model, stage, agent, "
                "cost_usd, tokens_in, tokens_out, outcome) values (:ts, 'openrouter', :model, "
                ":requested, :stage, :agent, :cost, 100, 10, :outcome)"
            ),
            {"ts": ts, "model": model, "requested": requested, "stage": stage, "agent": agent,
             "cost": cost, "outcome": outcome},
        )


# --- job_runs ------------------------------------------------------------------------------------


def test_the_latest_pass_and_the_latest_completed_pass(db):
    record_job(db, JobRun("groundtruth", NOW - timedelta(hours=7), NOW - timedelta(hours=6),
                          completed=True, errors=1, changed=True))
    record_job(db, JobRun("groundtruth", NOW - timedelta(hours=1), NOW, completed=False))
    record_job(db, JobRun("enrichment", NOW, NOW, completed=True))
    with db.connect() as conn:
        latest = load_latest_job(conn, "groundtruth")
        done = load_latest_completed_job(conn, "groundtruth")
        assert load_latest_job(conn, "enrichment").completed is True
    assert (latest.completed, latest.finished_at) == (False, NOW)
    assert (done.completed, done.errors, done.changed) == (True, 1, True)


def test_job_runs_refuses_an_unknown_job_or_a_backwards_pass(db):
    with pytest.raises(DBAPIError):
        record_job(db, JobRun("collection", NOW, NOW, completed=True))
    with pytest.raises(DBAPIError):
        record_job(db, JobRun("groundtruth", NOW, NOW - timedelta(seconds=1), completed=True))


def test_reads_cannot_write(api):
    with pytest.raises(DBAPIError, match="read-only"), api._read() as conn:
        conn.execute(text("delete from job_runs"))


# --- Verdicts --------------------------------------------------------------------------------------


def test_groundtruth_needs_a_completed_pass_within_seven_hours(api, db):
    assert api.verdict("groundtruth").ok is False
    record_job(db, JobRun("groundtruth", NOW - timedelta(hours=8), NOW - timedelta(hours=8),
                          completed=True))
    assert api.verdict("groundtruth").ok is False
    record_job(db, JobRun("groundtruth", NOW - timedelta(hours=1), NOW - timedelta(hours=1),
                          completed=False))
    verdict = api.verdict("groundtruth")
    assert verdict.ok is False and verdict.summary["last_pass"]["completed"] is False
    record_job(db, JobRun("groundtruth", NOW - timedelta(minutes=50), NOW - timedelta(minutes=49),
                          completed=True, errors=2))
    verdict = api.verdict("groundtruth")
    assert verdict.ok is True
    assert (verdict.summary["age_minutes"], verdict.summary["last_completed_errors"]) == (49, 2)


def test_source_verify_counts_the_enabled_sources_latest_checks(api, db):
    assert api.verdict("source-verify").ok is False  # nothing checked yet
    add_health(db, "ops-fast", "error", NOW - timedelta(hours=1), "HTTP 500")
    add_health(db, "ops-fast", "ok", NOW - timedelta(minutes=10))
    add_health(db, "ops-normal", "error", NOW - timedelta(minutes=10), "HTTP 503")
    add_health(db, "ops-off", "disabled", NOW - timedelta(minutes=10))
    with db.begin() as conn:
        conn.execute(text("update source_registry set lifecycle_state = 'degraded' "
                          "where id = 'ops-normal'"))
    verdict = api.verdict("source-verify")
    assert verdict.ok is True
    assert verdict.summary["sources_enabled"] == 2
    assert verdict.summary["latest_status"] == {"ok": 1, "error": 1}
    assert verdict.summary["lifecycle"] == {"active": 1, "degraded": 1}
    assert verdict.summary["not_ok"] == ["ops-normal"]


def test_source_verify_fails_when_no_check_is_recent(api, db):
    add_health(db, "ops-fast", "ok", NOW - timedelta(minutes=31))
    assert api.verdict("source-verify").ok is False


def test_correlation_report_reads_the_last_run_and_the_merges(api, db):
    add_run(db, "run-fast-1", "fast", NOW - timedelta(minutes=12), NOW - timedelta(minutes=11),
            errors=["a", "b"], new_events=3)
    add_run(db, "run-fast-2", "fast", NOW - timedelta(minutes=1), None)  # still running
    add_event(db, "evt-2026-000001", first_seen=NOW - timedelta(days=2))
    add_event(db, "evt-2026-000002", first_seen=NOW - timedelta(days=2), status="archived",
              merged_into="evt-2026-000001", updated_at=NOW - timedelta(hours=2))
    add_event(db, "evt-2026-000003", first_seen=NOW - timedelta(days=2), status="archived",
              merged_into="evt-2026-000001", updated_at=NOW - timedelta(hours=30))
    verdict = api.verdict("correlation-report")
    assert verdict.ok is True
    assert verdict.summary["last_run"]["run_id"] == "run-fast-1"
    assert verdict.summary["last_run"]["error_count"] == 2
    assert (verdict.summary["merged_last_24h"], verdict.summary["standing_events"]) == (1, 1)


# --- Reads ----------------------------------------------------------------------------------------


def test_the_digest(api, db):
    day = timedelta(days=1)
    # New, critical, from the FAST lane: an escalation candidate.
    add_event(db, "evt-2026-000001", first_seen=NOW - timedelta(hours=1), severity="critical",
              prominence=0.9, source="ops-fast", title="New critical")
    # Old, but updated in the window, and Australian: also a candidate.
    add_event(db, "evt-2026-000002", first_seen=NOW - 3 * day, lmu=NOW - timedelta(hours=2),
              au=0.8, prominence=0.4, source="ops-normal")
    # The back catalogue: new to the database, archived on arrival.
    add_event(db, "evt-2026-000003", first_seen=NOW - timedelta(hours=3), status="archived",
              prominence=0.01)
    # Old and quiet: standing, but not in the window.
    add_event(db, "evt-2026-000004", first_seen=NOW - 5 * day, severity="critical",
              prominence=0.6)
    add_event(db, "evt-2026-000005", first_seen=NOW - timedelta(hours=4), status="archived",
              merged_into="evt-2026-000001", updated_at=NOW - timedelta(hours=4))
    add_health(db, "ops-fast", "ok", NOW - 2 * day)
    add_health(db, "ops-fast", "error", NOW - timedelta(minutes=10), "HTTP 500")
    add_health(db, "ops-normal", "ok", NOW - 2 * day)
    add_health(db, "ops-normal", "ok", NOW - timedelta(minutes=10))
    add_run(db, "run-fast-1", "fast", NOW - timedelta(hours=1), NOW - timedelta(hours=1),
            errors=["x"], new_events=2)
    add_run(db, "run-normal-1", "normal", NOW - timedelta(hours=2), None, new_events=1)
    add_run(db, "run-fast-old", "fast", NOW - 2 * day, NOW - 2 * day, new_events=50)
    add_cost(db, NOW - timedelta(hours=1), Decimal("0.01"))

    digest = get(api, "/ops/digest?hours=24")
    assert digest["window"] == {"hours": 24, "from": "2026-10-02T08:00:00Z",
                                "to": "2026-10-03T08:00:00Z"}
    assert digest["events"] == {"new": 2, "new_archived_on_arrival": 1, "updated": 1,
                                "merged": 1, "standing": 3}
    assert [e["event_id"] for e in digest["top"]] == [
        "evt-2026-000001", "evt-2026-000004", "evt-2026-000002"
    ]
    candidates = {e["event_id"]: e for e in digest["escalation_candidates"]}
    assert set(candidates) == {"evt-2026-000001", "evt-2026-000002"}
    assert candidates["evt-2026-000001"]["fast_lane"] is True
    assert candidates["evt-2026-000002"]["lanes"] == ["normal"]
    [change] = digest["source_health_changes"]
    assert (change["source_id"], change["was"], change["now"], change["error"]) == (
        "ops-fast", "ok", "error", "HTTP 500"
    )
    assert digest["runs"] == {
        "fast": {"runs": 1, "unfinished": 0, "new_events": 2, "updated_events": 0,
                 "archived_events": 0, "errors": 1},
        "normal": {"runs": 1, "unfinished": 1, "new_events": 1, "updated_events": 0,
                   "archived_events": 0, "errors": 0},
    }
    assert digest["cost_this_month"]["calls"] == 1


def test_events_filters(api, db):
    add_event(db, "evt-2026-000001", first_seen=NOW - timedelta(hours=1), title="100% exposed",
              severity="high", prominence=0.9)
    add_event(db, "evt-2026-000002", first_seen=NOW - timedelta(days=3), title="1000 exposed",
              prominence=0.5)
    add_event(db, "evt-2026-000003", first_seen=NOW - timedelta(days=3), title="old-news",
              status="archived", prominence=0.01)
    add_event(db, "evt-2026-000004", first_seen=NOW - timedelta(hours=1), title="dupe",
              status="archived", merged_into="evt-2026-000001")

    def ids(target):
        return [e["event_id"] for e in get(api, target)["events"]]

    assert ids("/ops/events") == ["evt-2026-000001", "evt-2026-000002"]
    assert ids("/ops/events?status=archived") == ["evt-2026-000003"]
    assert ids("/ops/events?status=all") == ["evt-2026-000001", "evt-2026-000002",
                                             "evt-2026-000003"]
    assert ids("/ops/events?severity=high") == ["evt-2026-000001"]
    assert ids("/ops/events?q=100%25") == ["evt-2026-000001"]  # % is literal, not a wildcard
    assert ids("/ops/events?q=d_news&status=all") == []  # and so is _: it would match "-"
    assert ids("/ops/events?q=EXPOSED&limit=1") == ["evt-2026-000001"]
    assert ids("/ops/events?since=2026-10-02T00:00:00%2B00:00") == ["evt-2026-000001"]


def test_one_event(api, db):
    add_event(db, "evt-2026-000001", first_seen=NOW - timedelta(hours=1), source="ops-fast",
              title="One event")
    body = get(api, "/ops/events/evt-2026-000001")
    assert body["event"]["event_id"] == "evt-2026-000001" and body["merged_into"] is None
    assert body["event"]["title"] == "One event"
    assert api.handle("GET", "/ops/events/evt-2026-000999", AUTH).status == 404


def test_runs_truncate_their_errors(api, db):
    errors = ["e" * 400] + [f"error {n}" for n in range(6)]
    add_run(db, "run-fast-1", "fast", NOW - timedelta(hours=1), NOW, errors=errors)
    add_run(db, "run-fast-2", "fast", NOW - timedelta(minutes=5), None)
    body = get(api, "/ops/runs?limit=5")
    assert [r["run_id"] for r in body["runs"]] == ["run-fast-2", "run-fast-1"]
    run = body["runs"][1]
    assert run["error_count"] == 7 and len(run["errors"]) == 5
    assert run["errors"][0] == "e" * 300 + "..."


def test_cost_by_stage_model_and_agent(api, db):
    add_cost(db, NOW - timedelta(hours=1), Decimal("0.02"), stage="brief", model="m/cheap")
    add_cost(db, NOW - timedelta(hours=2), Decimal("0.01"), stage="triage", model=None,
             requested="m/free", agent="librarian")
    add_cost(db, NOW - timedelta(hours=3), None, stage="triage", model="m/cheap",
             outcome="invalid_output")
    add_cost(db, datetime(2026, 9, 30, 23, 59, tzinfo=UTC), Decimal(5))

    body = get(api, "/ops/cost")
    assert body["month"] == "2026-10"
    assert body["ledger"] == {"calls": 3, "cost_usd": 0.03, "unknown_cost_calls": 1}
    assert {r["key"]: r["cost_usd"] for r in body["by_model"]} == {"m/cheap": 0.02, "m/free": 0.01}
    assert {r["key"]: r["calls"] for r in body["by_agent"]} == {"none": 2, "librarian": 1}
    assert {r["key"]: r["calls"] for r in body["by_outcome"]} == {"ok": 2, "invalid_output": 1}
    assert body["key"]["outside_ledger_usd"] == pytest.approx(15.33)

    september = get(api, "/ops/cost?month=2026-09")
    assert september["ledger"]["cost_usd"] == 5.0 and "key" not in september


def test_cost_reconcile_fails_on_a_call_with_no_cost(api, db):
    add_cost(db, NOW - timedelta(hours=1), None, outcome="invalid_output")
    verdict = api.verdict("cost-reconcile")
    assert verdict.ok is False and "1 calls" in verdict.summary["reason"]


def test_sources_and_jobs(api, db):
    add_health(db, "ops-fast", "ok", NOW - timedelta(minutes=5))
    sources = get(api, "/ops/sources")
    assert {s["source_id"] for s in sources["sources"]} >= {"ops-fast", "ops-normal"}

    record_job(db, JobRun("enrichment", NOW - timedelta(minutes=4), NOW - timedelta(minutes=1),
                          completed=True, errors=3, changed=True))
    jobs = get(api, "/ops/jobs")
    assert set(jobs["wakes"]) == {"rogue", "librarian", "seraph", "prowl", "link"}
    assert jobs["wakes"]["seraph"]["ok"] is True
    assert jobs["wakes"]["link"]["ok"] is False  # nothing published in this test's data dir
    assert jobs["passes"]["enrichment"]["errors"] == 3 and jobs["passes"]["groundtruth"] is None
