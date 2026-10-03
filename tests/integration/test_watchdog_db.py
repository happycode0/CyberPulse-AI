"""The watchdog against a real Postgres: migration 015, the incidents' lifecycle and breaker,
the snapshot's queries, and one whole pass.

The watchdog opens its own connections, so rows are committed here and the tables it reads are
cleared before and after each test, as tests/integration/test_ops_db.py does.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from worker.db.incidents import (
    BREAKER_LIMIT,
    MAX_VERDICTS,
    load_incident,
    load_incidents,
    load_verdicts,
    record_pass,
    record_verdict,
)
from worker.db.jobs import JobRun, load_latest_jobs, record_job
from worker.db.migrate import run_migrations
from worker.db.notifications import claim
from worker.db.watchdog import LANE_RUNS, cost, gather, lanes, sources, volume
from worker.notify.telegram import SendResult
from worker.settings import Settings
from worker.version import PIPELINE_VERSION, SCHEMA_VERSION, SCORING_VERSION
from worker.watchdog.checks import COVERS, Finding
from worker.watchdog.reconcile import REOPEN_WITHIN, RESOLVE_AFTER
from worker.watchdog.run import Watchdog

NOW = datetime(2026, 10, 3, 8, 0, tzinfo=UTC)
MIN = timedelta(minutes=1)
SOURCES = frozenset(COVERS["sources"])
LANES = frozenset(COVERS["lanes"])
TABLES = (
    "incident_verdicts",
    "incidents",
    "notifications",
    "job_runs",
    "cost_ledger",
    "events",
    "source_health",
    "runs",
    "source_registry",
)


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


def add_source(db, sid, *, lane="fast", priority=1, enabled=True, url=None):
    with db.begin() as conn:
        conn.execute(
            text(
                "insert into source_registry (id, name, type, region, category, source_class, "
                "priority, lane, enabled, url, parser, expected_frequency, lifecycle_state) "
                "values (:id, :name, 'rss', 'AU', 'advisory', 'AUTHORITATIVE', :priority, :lane, "
                ":enabled, :url, 'feed', 'daily', 'active')"
            ),
            {
                "id": sid,
                "name": f"Feed {sid}",
                "priority": priority,
                "lane": lane,
                "enabled": enabled,
                "url": url or f"https://{sid}.example.org/feed",
            },
        )


def add_checks(db, sid, *statuses, items=0, error=None):
    """One source_health row per status, a minute apart, the last at NOW."""
    with db.begin() as conn:
        for n, status in enumerate(statuses):
            conn.execute(
                text(
                    "insert into source_health (source_id, checked_at, status, error, "
                    "items_fetched) values (:s, :at, :status, :error, :items)"
                ),
                {
                    "s": sid,
                    "at": NOW - (len(statuses) - 1 - n) * MIN,
                    "status": status,
                    "error": f"{error} {n}" if error else None,
                    "items": items,
                },
            )


def add_run(db, run_id, lane, finished, items=90):
    with db.begin() as conn:
        conn.execute(
            text(
                "insert into runs (run_id, lane, started_at, finished_at, items_fetched) "
                "values (:id, :lane, :started, :finished, :items)"
            ),
            {
                "id": run_id,
                "lane": lane,
                "started": (finished or NOW) - 2 * MIN,
                "finished": finished,
                "items": items,
            },
        )


def add_event(db, n, *, created, first_seen=None):
    with db.begin() as conn:
        conn.execute(
            text(
                "insert into events (event_id, schema_version, pipeline_version, "
                "scoring_version, enrichment_version, first_seen, last_seen, title, summary, "
                "created_at) values (:id, :schema, :pipeline, :scoring, '1', :seen, :seen, "
                "'t', 's', :created)"
            ),
            {
                "id": f"evt-2026-{n:06d}",
                "schema": SCHEMA_VERSION,
                "pipeline": PIPELINE_VERSION,
                "scoring": SCORING_VERSION,
                "seen": first_seen or created,
                "created": created,
            },
        )


def add_cost(db, ts, usd):
    with db.begin() as conn:
        conn.execute(
            text(
                "insert into cost_ledger (ts, provider, model, stage, cost_usd, outcome) "
                "values (:ts, 'openrouter', 'm', 'enrichment', :usd, 'ok')"
            ),
            {"ts": ts, "usd": usd},
        )


def read(db, sql, **params):
    with db.connect() as conn:
        return conn.execute(text(sql), params).all()


def failing(sid="wd-a", severity="high"):
    return Finding("feed-failing", sid, severity, f"Feed {sid} has failed its last 5 checks")


# ─── Migration 015 ────────────────────────────────────────────────────────────────────────────────


def test_an_incident_has_the_watchdog_columns_and_defaults(db):
    with db.begin() as conn:
        row = (
            conn.execute(
                text(
                    "insert into incidents (kind, severity, title) values ('stale-feed', 'low', "
                    "'t') returning subject, checks, evidence, fix_failures, needs_human, "
                    "reopened, clear_since, last_seen"
                )
            )
            .mappings()
            .one()
        )
    assert dict(row) == {
        "subject": "",
        "checks": 1,
        "evidence": {},
        "fix_failures": 0,
        "needs_human": False,
        "reopened": 0,
        "clear_since": None,
        "last_seen": None,
    }


@pytest.mark.parametrize(
    "column, value",
    [("severity", "unknown"), ("checks", 0), ("fix_failures", -1), ("reopened", -1)],
)
def test_an_incident_rejects_values_out_of_range(db, column, value):
    with pytest.raises(IntegrityError), db.begin() as conn:
        conn.execute(
            text(f"insert into incidents (kind, title, {column}) values ('stale-feed', 't', :v)"),
            {"v": value},
        )


def test_one_unresolved_incident_per_fault(db):
    insert = text("insert into incidents (kind, subject, title, status) values ('k', 's', 't', :s)")
    with db.begin() as conn:
        conn.execute(insert, {"s": "resolved"})
        conn.execute(insert, {"s": "resolved"})
        conn.execute(insert, {"s": "open"})
        conn.execute(
            text("insert into incidents (kind, subject, title) values ('k', 'other', 't')")
        )
    with pytest.raises(IntegrityError), db.begin() as conn:
        conn.execute(insert, {"s": "mitigated"})


def test_a_verdict_belongs_to_an_incident_and_says_pass_or_fail(db):
    with db.begin() as conn:
        incident = conn.execute(
            text("insert into incidents (kind, title) values ('k', 't') returning id")
        ).scalar_one()
    for verdict, pr in (("maybe", 1), ("pass", 0)):
        with pytest.raises(IntegrityError), db.begin() as conn:
            conn.execute(
                text(
                    "insert into incident_verdicts (incident_id, ts, verdict, pr) "
                    "values (:id, now(), :v, :pr)"
                ),
                {"id": incident, "v": verdict, "pr": pr},
            )
    with db.begin() as conn:
        conn.execute(
            text(
                "insert into incident_verdicts (incident_id, ts, verdict, pr) "
                "values (:id, now(), 'pass', 1)"
            ),
            {"id": incident},
        )
        conn.execute(text("delete from incidents where id = :id"), {"id": incident})
    assert read(db, "select count(*) from incident_verdicts") == [(0,)]


@pytest.mark.parametrize("job", ["watchdog", "publish", "push", "groundtruth"])
def test_job_runs_take_the_new_jobs_and_keep_a_note(db, job):
    run = JobRun(job, NOW - MIN, NOW, completed=False, errors=2, note="RuntimeError")
    record_job(db, run)
    with db.connect() as conn:
        assert load_latest_jobs(conn)[job] == run


def test_job_runs_still_reject_an_unknown_job(db):
    with pytest.raises(IntegrityError):
        record_job(db, JobRun("deploy", NOW, NOW, completed=True))  # type: ignore[arg-type]


def test_a_notice_can_go_to_paperclip_but_nowhere_else(db):
    claimed = claim(db, kind="incident", key="paperclip:incident:1", now=NOW, channel="paperclip")
    assert claimed is not None and claimed.attempt == 1
    assert read(db, "select channel from notifications") == [("paperclip",)]
    with pytest.raises(IntegrityError):
        claim(db, kind="incident", key="email:incident:1", now=NOW, channel="email")


# ─── record_pass ──────────────────────────────────────────────────────────────────────────────────


def test_a_finding_opens_an_incident_tied_to_its_source(db):
    add_source(db, "wd-a")
    lane = Finding("no-collection", "fast", "critical", "The fast lane stopped", {"limit": 35})
    result = record_pass(db, [failing(), failing("wd-gone"), lane], SOURCES | LANES, NOW)
    assert [(i.kind, i.subject, i.status, i.checks) for i in result.opened] == [
        ("feed-failing", "wd-a", "open", 1),
        ("feed-failing", "wd-gone", "open", 1),
        ("no-collection", "fast", "open", 1),
    ]
    assert result.unresolved == result.opened and result.resolved == []
    assert result.opened[2].evidence == {"limit": 35}
    assert result.opened[2].opened_at == result.opened[2].last_seen == NOW
    rows = read(db, "select subject, source_id from incidents order by id")
    # Only a source still in the registry is tied; a lane is never a source.
    assert rows == [("wd-a", "wd-a"), ("wd-gone", None), ("fast", None)]


def test_an_incident_seen_again_is_updated_not_duplicated(db):
    first = record_pass(db, [failing()], SOURCES, NOW).opened[0]
    worse = Finding("feed-failing", "wd-a", "medium", "now 6", {"failures_in_a_row": 6})
    result = record_pass(db, [worse, worse], SOURCES, NOW + 5 * MIN)
    assert result.opened == [] and result.resolved == []
    (again,) = result.unresolved
    assert again.id == first.id
    assert (again.checks, again.last_seen, again.severity, again.title) == (
        2,
        NOW + 5 * MIN,
        "medium",
        "now 6",
    )
    assert again.evidence == {"failures_in_a_row": 6} and again.opened_at == NOW


def test_an_incident_resolves_once_clear_for_long_enough(db):
    opened = record_pass(db, [failing()], SOURCES, NOW).opened[0]
    first_quiet = NOW + 5 * MIN
    result = record_pass(db, [], SOURCES, first_quiet)
    assert result.resolved == [] and result.unresolved[0].clear_since == first_quiet
    # A later quiet pass keeps the first quiet time.
    result = record_pass(db, [], SOURCES, first_quiet + 5 * MIN)
    assert result.unresolved[0].clear_since == first_quiet
    result = record_pass(db, [], SOURCES, first_quiet + RESOLVE_AFTER)
    (resolved,) = result.resolved
    assert resolved.id == opened.id and resolved.status == "resolved"
    assert resolved.resolved_at == first_quiet + RESOLVE_AFTER and result.unresolved == []


def test_a_fault_seen_while_clearing_stops_clearing(db):
    record_pass(db, [failing()], SOURCES, NOW)
    record_pass(db, [], SOURCES, NOW + 5 * MIN)
    result = record_pass(db, [failing()], SOURCES, NOW + 10 * MIN)
    assert result.unresolved[0].clear_since is None
    result = record_pass(db, [], SOURCES, NOW + 10 * MIN + RESOLVE_AFTER)
    assert result.resolved == [] and result.unresolved[0].clear_since is not None


def test_a_kind_the_pass_could_not_check_is_left_alone(db):
    record_pass(db, [failing()], SOURCES, NOW)
    for minutes in (5, 30, 120):
        result = record_pass(db, [], LANES, NOW + minutes * MIN)
        assert result.resolved == [] and result.unresolved[0].clear_since is None


def test_a_fault_back_soon_reopens_its_incident_with_its_failed_fixes(db):
    opened = record_pass(db, [failing()], SOURCES, NOW).opened[0]
    record_verdict(db, opened.id, verdict="fail", pr=7, reasons="still fails", now=NOW + MIN)
    record_pass(db, [], SOURCES, NOW + 5 * MIN)
    resolved_at = NOW + 5 * MIN + RESOLVE_AFTER
    record_pass(db, [], SOURCES, resolved_at)
    back = resolved_at + REOPEN_WITHIN - MIN
    result = record_pass(db, [failing(severity="medium")], SOURCES, back)
    (reopened,) = result.opened
    assert reopened.id == opened.id
    assert (reopened.status, reopened.reopened, reopened.fix_failures, reopened.checks) == (
        "open",
        1,
        1,
        1,
    )
    assert (reopened.resolved_at, reopened.clear_since, reopened.last_seen) == (None, None, back)
    assert reopened.opened_at == NOW and reopened.severity == "medium"


def test_a_fault_back_much_later_is_a_new_incident(db):
    first = record_pass(db, [failing()], SOURCES, NOW).opened[0]
    record_pass(db, [], SOURCES, NOW + 5 * MIN)
    resolved_at = NOW + 5 * MIN + RESOLVE_AFTER
    record_pass(db, [], SOURCES, resolved_at)
    result = record_pass(db, [failing()], SOURCES, resolved_at + REOPEN_WITHIN + MIN)
    (fresh,) = result.opened
    assert fresh.id != first.id and fresh.reopened == 0
    assert read(db, "select count(*) from incidents") == [(2,)]


# ─── Verdicts and the breaker ─────────────────────────────────────────────────────────────────────


def opened(db, sid="wd-a"):
    return record_pass(db, [failing(sid)], SOURCES, NOW).opened[0]


def test_failed_fixes_trip_the_breaker_and_then_no_verdict_is_taken(db):
    incident = opened(db)
    passed = record_verdict(db, incident.id, verdict="pass", pr=40, reasons="ok", now=NOW)
    assert passed.outcome == "recorded" and passed.incident.fix_failures == 0
    for n in range(1, BREAKER_LIMIT + 1):
        result = record_verdict(
            db, incident.id, verdict="fail", pr=40 + n, reasons=f"fail {n}", now=NOW + n * MIN
        )
        assert result.outcome == "recorded"
        assert result.incident.fix_failures == n
        assert result.tripped == result.incident.needs_human == (n == BREAKER_LIMIT)
    halted = record_verdict(db, incident.id, verdict="pass", pr=50, reasons="ok", now=NOW + MIN)
    assert halted.outcome == "halted" and not halted.tripped
    with db.connect() as conn:
        verdicts = load_verdicts(conn, [incident.id, 999_999])
    assert [(v.verdict, v.pr) for v in verdicts[incident.id]] == [
        ("pass", 40),
        ("fail", 41),
        ("fail", 42),
        ("fail", 43),
    ]
    assert verdicts[999_999] == []


def test_a_tripped_breaker_survives_a_reopen(db):
    incident = opened(db)
    for _ in range(BREAKER_LIMIT):
        record_verdict(db, incident.id, verdict="fail", pr=1, reasons="no", now=NOW)
    record_pass(db, [], SOURCES, NOW + 5 * MIN)
    record_pass(db, [], SOURCES, NOW + 5 * MIN + RESOLVE_AFTER)
    (back,) = record_pass(db, [failing()], SOURCES, NOW + 2 * RESOLVE_AFTER).opened
    assert back.id == incident.id and back.needs_human and back.fix_failures == BREAKER_LIMIT
    result = record_verdict(db, incident.id, verdict="pass", pr=2, reasons="ok", now=NOW)
    assert result.outcome == "halted"


def test_no_verdict_on_a_resolved_or_missing_incident(db):
    incident = opened(db)
    record_pass(db, [], SOURCES, NOW + 5 * MIN)
    record_pass(db, [], SOURCES, NOW + 5 * MIN + RESOLVE_AFTER)
    resolved = record_verdict(db, incident.id, verdict="pass", pr=1, reasons="ok", now=NOW)
    assert resolved.outcome == "resolved" and resolved.incident.id == incident.id
    missing = record_verdict(db, 999_999, verdict="pass", pr=1, reasons="ok", now=NOW)
    assert missing.outcome == "not_found" and missing.incident is None
    assert read(db, "select count(*) from incident_verdicts") == [(0,)]


def test_an_incident_takes_so_many_verdicts_and_no_more(db):
    incident = opened(db)
    for n in range(MAX_VERDICTS):
        result = record_verdict(db, incident.id, verdict="pass", pr=n + 1, reasons="ok", now=NOW)
        assert result.outcome == "recorded"
    full = record_verdict(db, incident.id, verdict="fail", pr=99, reasons="no", now=NOW)
    assert full.outcome == "full" and full.incident.fix_failures == 0
    assert read(db, "select count(*) from incident_verdicts") == [(MAX_VERDICTS,)]


def test_loading_incidents_open_or_lately_resolved(db):
    old = opened(db, "wd-old")
    record_pass(db, [], SOURCES, NOW + 5 * MIN)
    record_pass(db, [], SOURCES, NOW + 5 * MIN + RESOLVE_AFTER)
    live = record_pass(db, [failing("wd-live")], SOURCES, NOW + 30 * MIN).opened[0]
    with db.connect() as conn:
        assert [i.id for i in load_incidents(conn, unresolved_only=True, since=NOW)] == [live.id]
        assert [i.id for i in load_incidents(conn, unresolved_only=False, since=NOW)] == [
            live.id,
            old.id,
        ]
        later = NOW + timedelta(days=1)
        assert [i.id for i in load_incidents(conn, unresolved_only=False, since=later)] == [live.id]
        assert [i.id for i in load_incidents(conn, unresolved_only=False, since=NOW, limit=1)] == [
            live.id
        ]
        assert load_incident(conn, old.id).status == "resolved"
        assert load_incident(conn, 999_999) is None


# ─── The snapshot's queries ───────────────────────────────────────────────────────────────────────


def test_lanes_are_their_newest_finished_runs(db):
    for n in range(5):
        add_run(db, f"fast-{n}", "fast", NOW - (5 * n) * MIN, items=n)
    add_run(db, "fast-running", "fast", None)
    add_run(db, "deep-1", "deep", NOW)
    with db.connect() as conn:
        got = lanes(conn)
    assert set(got) == {"fast", "normal"} and got["normal"] == []
    assert [(r.finished_at, r.items_fetched) for r in got["fast"]] == [
        (NOW - (5 * n) * MIN, n) for n in range(LANE_RUNS)
    ]


def test_sources_are_their_recent_checks_oldest_first(db):
    add_source(db, "wd-a", url="https://shared.example.org/a")
    add_source(db, "wd-b", url="https://shared.example.org/b", priority=2, lane="normal")
    add_source(db, "wd-off", enabled=False)
    add_checks(db, "wd-a", "ok", "disabled", "error", "timeout", error="HTTP 503")
    add_checks(db, "wd-off", "error")
    with db.begin() as conn:
        conn.execute(
            text(
                "insert into source_health (source_id, checked_at, status, items_fetched) "
                "values ('wd-a', :at, 'ok', 12)"
            ),
            {"at": NOW - timedelta(days=30)},
        )
    with db.connect() as conn:
        got = {s.source_id: s for s in sources(conn)}
    assert set(got) == {"wd-a", "wd-b"}
    a, b = got["wd-a"], got["wd-b"]
    assert a.statuses == ("ok", "ok", "error", "timeout")  # 'disabled' is not a check
    assert (a.last_error, a.last_checked, a.had_items) == ("HTTP 503 3", NOW, True)
    assert (a.host, a.lane, a.priority, a.lifecycle, a.name) == (
        "shared.example.org",
        "fast",
        1,
        "active",
        "Feed wd-a",
    )
    assert (b.statuses, b.last_error, b.last_checked, b.had_items) == ((), None, None, False)


def test_sources_look_back_only_so_far(db):
    add_source(db, "wd-a")
    add_checks(db, "wd-a", *(["ok"] * 5 + ["error"] * 10))
    with db.connect() as conn:
        (a,) = sources(conn)
    assert a.statuses == ("error",) * 10


def test_volume_counts_fresh_events_and_the_usual_days(db):
    n = iter(range(1, 1000))
    for hours in (1, 2, 7):  # two in the last 6 hours, three in the last 24
        add_event(db, next(n), created=NOW - timedelta(hours=hours))
    # A back catalogue: created now, but published long before.
    for _ in range(4):
        add_event(db, next(n), created=NOW - MIN, first_seen=NOW - timedelta(days=60))
    for _ in range(5):  # the day before
        add_event(db, next(n), created=NOW - timedelta(hours=30))
    for _ in range(2):  # where history starts
        add_event(db, next(n), created=NOW - timedelta(hours=80))
    with db.connect() as conn:
        v = volume(conn, NOW)
    # No day that starts within the first day of history is a usual day: the 48 to 72 hours
    # before the last 24 start 8 hours into it.
    assert (v.fresh_24h, v.fresh_6h, v.usual) == (3, 2, (5,))


def test_volume_with_no_events(db):
    with db.connect() as conn:
        v = volume(conn, NOW)
    assert (v.fresh_24h, v.fresh_6h, v.usual) == (0, 0, ())


def test_cost_is_the_last_day_the_usual_days_and_the_month(db):
    add_cost(db, NOW - timedelta(hours=1), "0.25")
    add_cost(db, NOW - timedelta(hours=26), "0.50")  # the day before
    add_cost(db, NOW - timedelta(hours=74), "0.10")  # 30 September: last month
    add_cost(db, NOW - timedelta(days=9), "0.01")
    with db.connect() as conn:
        c = cost(conn, NOW, Decimal(20))
    assert c.last_24h == Decimal("0.25") and c.budget == Decimal(20)
    assert c.month_to_date == Decimal("0.75")
    assert c.usual == (
        Decimal("0.50"),
        Decimal(0),
        Decimal("0.10"),
        Decimal(0),
        Decimal(0),
        Decimal(0),
        Decimal(0),
    )


def test_gather_reads_every_part(db):
    add_source(db, "wd-a")
    add_run(db, "fast-1", "fast", NOW)
    record_job(db, JobRun("publish", NOW - MIN, NOW, completed=False, note="RuntimeError"))
    record_job(db, JobRun("publish", NOW - 9 * MIN, NOW - 8 * MIN, completed=True))
    snapshot = gather(db, NOW, budget=Decimal(20))
    assert snapshot.now == NOW and snapshot.site is None and snapshot.paperclip is None
    assert [s.source_id for s in snapshot.sources] == ["wd-a"]
    assert len(snapshot.lanes["fast"]) == 1 and snapshot.volume.fresh_24h == 0
    assert snapshot.jobs.latest["publish"].note == "RuntimeError"
    assert snapshot.jobs.completed["publish"].finished_at == NOW - 8 * MIN
    assert snapshot.cost.last_24h == 0


# ─── One whole pass ───────────────────────────────────────────────────────────────────────────────


class FakeRoutine:
    def __init__(self):
        self.payloads = []

    async def fire(self, payload):
        self.payloads.append(payload)
        return SendResult(True)


async def test_a_pass_opens_an_incident_tells_the_crew_once_and_records_itself(db):
    add_source(db, "wd-a")
    add_checks(db, "wd-a", *(["error"] * 5), error="HTTP 500")
    add_run(db, "fast-1", "fast", NOW - 5 * MIN)
    crew = FakeRoutine()
    watchdog = Watchdog(
        # No probe: a test never calls the Paperclip it may be running beside.
        Settings(_env_file=None, database_url="postgresql://x", watchdog_paperclip_url=""),
        telegram=lambda s: None,
        routine=lambda s: crew,
        token_configured=lambda: False,
    )
    first = await watchdog.run_pass(db, NOW)
    assert (first.database, first.findings, first.opened, first.unresolved) == (True, 1, 1, 1)
    assert (first.uncovered, first.sent, first.failed) == ([], 1, 0)
    second = await watchdog.run_pass(db, NOW + 5 * MIN)
    assert (second.opened, second.unresolved, second.sent) == (0, 1, 0)

    (incident,) = read(db, "select id, kind, subject, severity, checks from incidents")
    assert tuple(incident)[1:] == ("feed-failing", "wd-a", "high", 2)
    assert crew.payloads == [
        {
            "incident": incident.id,
            "ref": f"INC-{incident.id}",
            "kind": "feed-failing",
            "subject": "wd-a",
            "severity": "high",
            "title": "Feed wd-a has failed its last 5 checks",
            "read": f"GET /ops/incidents/{incident.id}",
        }
    ]
    assert read(db, "select dedupe_key, channel, status from notifications") == [
        (f"paperclip:incident:{incident.id}", "paperclip", "sent")
    ]
    assert read(db, "select job, completed, errors, changed from job_runs order by started_at") == [
        ("watchdog", True, 0, True),
        ("watchdog", True, 0, False),
    ]
