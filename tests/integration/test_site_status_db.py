"""The readers behind the site's SYSTEM, SOURCES and CREW pages against a real Postgres: runs
finished and still going, what each job's newest pass found (migration 018), where the finds
stand at the gate, and what the crew's jobs did."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import text

from worker.db import discovery as db
from worker.db.crew import JobActivity, load_job_activity, load_pipeline_ai
from worker.db.jobs import JobRun, load_latest_jobs, record_job
from worker.db.migrate import run_migrations
from worker.db.runs import load_lane_activity, load_latest_finished_run, load_unfinished_runs
from worker.discovery.gate import DiscoveryConfig

NOW = datetime(2026, 10, 28, 9, 0, tzinfo=UTC)
MONTH = datetime(2026, 10, 1, tzinfo=UTC)
GATE = DiscoveryConfig.load().gate
TABLES = ("job_runs", "cost_ledger", "incident_verdicts", "incidents", "followup_tasks",
          "events", "source_candidates", "runs")
# The registry rows this module adds for its active and retired finds, and only those: other
# modules leave rows that their source checks still point at.
OWN_SOURCES = "found_sitetest_%"


def clear(engine) -> None:
    with engine.begin() as conn:
        for table in TABLES:
            conn.execute(text(f"delete from {table}"))
        conn.execute(text("delete from source_registry where id like :own"), {"own": OWN_SOURCES})


@pytest.fixture
def engine(pg_engine):
    run_migrations(pg_engine)
    clear(pg_engine)
    yield pg_engine
    clear(pg_engine)


def _runs(engine, *rows) -> None:
    with engine.begin() as conn:
        for run_id, lane, started, finished in rows:
            conn.execute(
                text("insert into runs (run_id, lane, started_at, finished_at) "
                     "values (:id, :lane, :s, :f)"),
                {"id": run_id, "lane": lane, "s": started, "f": finished},
            )


def test_the_latest_finished_run_is_not_the_one_still_going(engine):
    # 28 Oct: the fast and normal lanes start at 08:00; the fast one finishes in 22 seconds.
    eight = NOW - timedelta(hours=1)
    _runs(engine,
          ("fast-07", "fast", eight - timedelta(hours=1), eight - timedelta(minutes=59)),
          ("normal-08", "normal", eight, None),
          ("fast-08", "fast", eight + timedelta(seconds=1), eight + timedelta(seconds=22)),
          ("normal-04", "normal", eight - timedelta(hours=4), None))
    with engine.connect() as conn:
        finished = load_latest_finished_run(conn)
        going = load_unfinished_runs(conn, since=NOW - timedelta(hours=2))
        lanes = load_lane_activity(conn)
    assert finished.run_id == "fast-08"
    # Only one started inside the window is still going; the older one never finished.
    assert [r.run_id for r in going] == ["normal-08"]
    assert lanes == {
        "fast": (eight + timedelta(seconds=1), eight + timedelta(seconds=22)),
        "normal": (eight, None),
    }


def test_no_finished_run_is_none(engine):
    _runs(engine, ("normal-08", "normal", NOW, None))
    with engine.connect() as conn:
        assert load_latest_finished_run(conn) is None


def test_a_pass_keeps_what_it_found(engine):
    detail = {"probes": 10, "healthy": 7, "activated": ["found_dev_to"], "waiting": 39}
    record_job(engine, JobRun("source-gate", NOW - timedelta(hours=4), NOW - timedelta(hours=4),
                              completed=True))
    record_job(engine, JobRun("source-gate", NOW, NOW, completed=True, detail=detail))
    record_job(engine, JobRun("discovery", NOW, NOW, completed=False, note="RuntimeError"))
    with engine.connect() as conn:
        latest = load_latest_jobs(conn)
    assert latest["source-gate"].detail == detail
    assert latest["discovery"].detail is None and latest["discovery"].note == "RuntimeError"


def _candidate(engine, host, state, *, feed=True, passes=0, failures=0, found_by="search",
               proposed_at=None, updated_at=NOW, source_id=None):
    with engine.begin() as conn:
        if source_id is not None:  # an active or retired find names its registry row
            conn.execute(
                text(
                    "insert into source_registry (id, name, type, region, category, "
                    "source_class, priority, lane, enabled, url, parser, expected_frequency) "
                    "values (:id, :id, 'rss', 'global', 'news', 'discovered', 3, 'normal', "
                    ":enabled, :url, 'feed', 'daily')"
                ),
                {"id": source_id, "enabled": state == "active", "url": f"https://{host}/rss"},
            )
        conn.execute(
            text(
                "insert into source_candidates (host, feed_url, found_by, state, passes, failures, "
                "proposed_at, source_id, created_at, updated_at) values (:host, :feed, :by, "
                ":state, :passes, :failures, :proposed, :source_id, :at, :at)"
            ),
            {"host": host, "feed": f"https://{host}/rss" if feed else None, "by": found_by,
             "state": state, "passes": passes, "failures": failures, "proposed": proposed_at,
             "source_id": source_id, "at": updated_at},
        )


def test_the_gate_counts_say_where_every_find_stands(engine):
    ready = GATE.probes_to_activate
    _candidate(engine, "ready1.example.org", "testing", passes=ready + 20)
    _candidate(engine, "ready2.example.org", "testing", passes=ready)
    _candidate(engine, "trying.example.org", "testing", passes=ready - 1)
    _candidate(engine, "new.example.org", "candidate")
    _candidate(engine, "queued.example.org", "discovered", found_by="tachikoma",
               proposed_at=NOW)
    _candidate(engine, "home.example.org", "discovered", feed=False)
    _candidate(engine, "nofeed.example.org", "discovered", feed=False,
               failures=GATE.failures_to_reject)
    _candidate(engine, "no.example.org", "rejected", failures=3)
    _candidate(engine, "gone.example.org", "retired", source_id="found_sitetest_gone",
               updated_at=NOW - timedelta(days=2))
    _candidate(engine, "old.example.org", "retired", source_id="found_sitetest_old",
               updated_at=NOW - timedelta(days=30))
    _candidate(engine, "dev.to", "active", source_id="found_sitetest_dev_to",
               updated_at=NOW - timedelta(hours=1))
    with engine.connect() as conn:
        counts = db.load_gate_counts(conn, gate=GATE)
        settled = db.load_settled(conn, since=NOW - timedelta(days=14), limit=10)
    assert counts == db.GateCounts(
        ready_waiting=2, testing=2, queued=1, homes_waiting=1, at_the_gate=7, rejected=1,
        retired=2,
    )
    assert [(c.host, c.state) for c in settled] == [
        ("dev.to", "active"), ("gone.example.org", "retired"),
    ]


def _ledger(engine, *rows) -> None:
    with engine.begin() as conn:
        for ts, provider, model, stage, agent, cost in rows:
            conn.execute(
                text("insert into cost_ledger (ts, provider, model, stage, agent, cost_usd, "
                     "outcome) values (:ts, :p, :m, :s, :a, :c, 'ok')"),
                {"ts": ts, "p": provider, "m": model, "s": stage, "a": agent, "c": cost},
            )


def test_each_ai_job_is_counted_from_its_own_stage(engine):
    _ledger(
        engine,
        (MONTH - timedelta(days=2), "mixroute", "old", "report.daily", "worker", 0.5),
        (NOW - timedelta(days=3), "mixroute", "zhipu/glm-5.3-flash", "report.daily", "worker",
         0.002),
        (NOW - timedelta(days=1), "mixroute", "deepseek/deepseek-v4-flash", "report.daily",
         "worker", 0.001),
        # A sub-stage counts as its job's; a neighbouring name does not.
        (NOW - timedelta(hours=2), "mixroute", "m", "followup.report.retry", "worker", 0.003),
        (NOW - timedelta(hours=1), "mixroute", "m", "followup.reports", "worker", 9),
        # Not the worker's: the retired gauntlet's label.
        (NOW, "openrouter", "m", "incident.diagnosis", "ripperdoc", 1),
    )
    stages = {"morpheus": "report.daily", "deckard": "followup.report",
              "teletraan": "incident.diagnosis"}
    with engine.connect() as conn:
        jobs = load_job_activity(conn, since=MONTH, stages=stages)
    assert jobs["morpheus"] == JobActivity(
        2, Decimal("0.003"), NOW - timedelta(days=1), "deepseek/deepseek-v4-flash", "mixroute"
    )
    assert (jobs["deckard"].calls, jobs["deckard"].cost_usd) == (1, Decimal("0.003"))
    # Never run: the defaults, which the page reads as "not run yet".
    assert jobs["teletraan"] == JobActivity()


def test_the_pipelines_last_model_call_and_its_provider(engine):
    with engine.begin() as conn:
        conn.execute(
            text(
                "insert into cost_ledger (ts, provider, model, stage, agent, cost_usd, outcome) "
                "values (:a, 'openrouter', 'm', 'enrich.brief', 'worker', 0.01, 'ok'), "
                "(:b, 'mixroute', 'm', 'enrich.triage', null, null, 'invalid_output'), "
                "(:c, 'openrouter', 'm', 'gauntlet', 'ripperdoc', 0.5, 'ok'), "
                "(:c, 'mixroute', 'm', 'report.daily', 'worker', 0.2, 'ok')"
            ),
            {"a": NOW - timedelta(days=15), "b": NOW - timedelta(days=14), "c": NOW},
        )
    with engine.connect() as conn:
        ai = load_pipeline_ai(conn, since=MONTH)
    assert (ai.calls, ai.cost_usd) == (2, Decimal("0.01"))
    # The gauntlet's call is not the pipeline's, and nor is a crew job's (JobActivity).
    assert (ai.last_call_at, ai.provider) == (NOW - timedelta(days=14), "mixroute")
