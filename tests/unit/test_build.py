"""Publisher orchestration with the database readers stubbed out.

These run everywhere; `tests/integration/test_build.py` covers the real SQL. Together they
pin Review Focus #5: a secret anywhere blocks every write, and a failed run leaves the
previous output byte-for-byte intact.
"""

import json
import os
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

import worker.publish.build as build
from worker.db.crew import Activity, PipelineAi
from worker.db.discovery import Candidate
from worker.db.sources import RegistryRow
from worker.db.trends import TrendInputs
from worker.models import (
    AiSignificance,
    Event,
    HealthStatus,
    Lane,
    LifecycleState,
    RunSummary,
    SourceHealth,
)
from worker.pipeline.reputation import Corroboration
from worker.pipeline.trends import Report, Story
from worker.publish.build import build_all
from worker.publish.validate import ValidationFailure
from worker.schedule import public_schedule

NOW = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)
OR_KEY = "sk-or-v1-deadbeefdeadbeefdeadbeefdeadbeef"


def make_event(n=1, *, prominence=0.9, first_seen=NOW, **overrides) -> Event:
    fields = {
        "event_id": f"evt-2026-{n:06d}",
        "title": f"Event {n}",
        "summary": "CVE-2026-88772 exploited; patch available.",
        "first_seen": first_seen,
        "last_seen": first_seen,
        "risk": {"prominence": prominence},
    }
    fields.update(overrides)
    return Event(**fields)


@pytest.fixture
def db(monkeypatch):
    """Stub every reader `build` uses; tests mutate the returned dict."""
    state = {
        "events": [make_event(1, prominence=0.9), make_event(2, prominence=0.4)],
        "dates": [NOW.date()],
        "completed": NOW - timedelta(minutes=3),
        "run": RunSummary(
            run_id="run-1", lane=Lane.FAST, started_at=NOW - timedelta(minutes=5),
            finished_at=NOW - timedelta(minutes=3), sources_ok=9, sources_failed=1,
            sources_stale=0, items_fetched=40, new_events=2, updated_events=1,
            duplicates=3, archived_events=0, errors=["boom"],
        ),
        "registry": [
            RegistryRow("acsc-alerts", "ACSC Alerts", "AU", "advisory", "fast", True),
            RegistryRow("old-feed", "Old feed", "US", "news", "normal", False),
        ],
        "health": {
            "acsc-alerts": [
                SourceHealth(source_id="acsc-alerts", checked_at=NOW, status=HealthStatus.OK,
                             newest_item_age_days=0.5, items_fetched=12, duration_ms=200),
            ],
        },
        "lifecycle": {"old-feed": LifecycleState.RETIRED},
        "corroboration": {},
        "gate": [],
        "merged": {},
        "trends": TrendInputs([], [], {}, NOW - timedelta(days=3)),
        "crew": {
            "seraph": Activity(894, 4045, NOW - timedelta(minutes=50)),
            "ripperdoc": Activity(0, None, None),
        },
        "pipeline_ai": PipelineAi(12, Decimal("0.0412")),
    }
    monkeypatch.setattr(build, "load_live_events", lambda conn, **kw: state["events"])
    monkeypatch.setattr(build, "load_event_dates", lambda conn: state["dates"])
    monkeypatch.setattr(build, "load_last_completed_collection", lambda conn: state["completed"])
    monkeypatch.setattr(build, "load_latest_run", lambda conn: state["run"])
    monkeypatch.setattr(build, "load_registry_rows", lambda conn: state["registry"])
    monkeypatch.setattr(
        build, "load_health_history", lambda conn, sid, limit=20: state["health"].get(sid, [])
    )
    monkeypatch.setattr(build, "load_lifecycle_states", lambda conn: state["lifecycle"])
    monkeypatch.setattr(build, "load_corroboration", lambda conn, **kw: state["corroboration"])
    monkeypatch.setattr(build, "load_at_the_gate", lambda conn, **kw: state["gate"][:kw["limit"]])
    monkeypatch.setattr(build, "merged_redirects", lambda conn, **kw: state["merged"])
    monkeypatch.setattr(build, "load_trend_inputs", lambda conn, **kw: state["trends"])
    monkeypatch.setattr(build, "load_crew_activity", lambda conn, **kw: state["crew"])
    monkeypatch.setattr(build, "load_pipeline_ai", lambda conn, **kw: state["pipeline_ai"])
    return state


def snapshot(directory):
    return {
        str(p.relative_to(directory)): p.read_bytes()
        for p in sorted(directory.rglob("*"))
        if p.is_file()
    }


def read(directory, name):
    return json.loads((directory / name).read_text())


def test_build_emits_expected_files(tmp_path, db):
    names = {p.relative_to(tmp_path).as_posix() for p in build_all(None, tmp_path, now=NOW)}
    assert names == {
        "live.json", "index.json", "source-health.json", "system-status.json", "trends.json",
        "crew.json", "history/2026-09-30.json",
    }
    assert {p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*") if p.is_file()} == names


def test_trends_are_counted_from_the_stored_reports(tmp_path, db):
    reports = [
        Report("evt-2026-000001", f"Akira ransomware hits firm {n}", NOW - timedelta(hours=n))
        for n in range(1, 4)
    ]
    stories = [Story("evt-2026-000001", NOW - timedelta(hours=3), 0.9, True, False)]
    db["trends"] = TrendInputs(reports, stories, {NOW.date(): 2}, NOW - timedelta(days=3))
    build_all(None, tmp_path, now=NOW)
    trends = read(tmp_path, "trends.json")
    akira = next(t for t in trends["topics"] if t["key"] == "akira")
    assert (akira["recent"], akira["state"], akira["event_ids"]) == (3, "new", ["evt-2026-000001"])
    assert trends["activity"][-1] | {"coverage": "-"} == {
        "date": "2026-09-30", "coverage": "-", "stories": 1, "reports": 3, "kev_added": 2,
        "critical_high": 1, "au_stories": 0, "ai_stories": 0,
    }
    assert trends["generated_at"] == read(tmp_path, "live.json")["generated_at"]


def test_live_filters_by_strict_prominence_and_keeps_order(tmp_path, db):
    db["events"] = [
        make_event(1, prominence=0.9), make_event(2, prominence=0.4),
        make_event(3, prominence=0.05), make_event(4, prominence=0.01),
    ]
    build_all(None, tmp_path, now=NOW)
    ids = [e["event_id"] for e in read(tmp_path, "live.json")["events"]]
    assert ids == ["evt-2026-000001", "evt-2026-000002"]
    assert read(tmp_path, "live.json")["counts"]["events"] == 2


def test_every_published_event_names_its_beat(tmp_path, db):
    """The site's Events filter reads `beat` and `ai_significance` (docs/wiki/ai-news-beat.md);
    build_all checks every file against its schema before it writes."""
    db["events"] = [
        make_event(1, domains=["ai"], ai_significance=AiSignificance.MAJOR),
        make_event(2, domains=["cybersecurity", "ai"], ai_significance=AiSignificance.MINOR),
        make_event(3, domains=["cybersecurity"]),
        make_event(4, domains=[]),
    ]
    build_all(None, tmp_path, now=NOW)
    for name in ("live.json", "history/2026-09-30.json"):
        events = {e["event_id"]: e for e in read(tmp_path, name)["events"]}
        assert [(e["beat"], e["ai_significance"]) for e in events.values()] == [
            ("ai", "major"), ("both", "minor"), ("cyber", None), ("other", None),
        ]


def test_history_groups_by_first_seen_utc_date_and_index_links_to_it(tmp_path, db):
    yesterday = NOW - timedelta(days=1)
    db["events"] = [make_event(1, first_seen=NOW), make_event(2, first_seen=yesterday)]
    db["dates"] = [NOW.date(), yesterday.date()]
    build_all(None, tmp_path, now=NOW)
    index = read(tmp_path, "index.json")
    assert index["latest"] == "2026-09-30"
    assert [d["date"] for d in index["days"]] == ["2026-09-30", "2026-09-29"]
    for day in index["days"]:
        h = read(tmp_path, day["path"])
        assert h["date"] == day["date"] and len(h["events"]) == day["event_count"] == 1


def test_no_raw_article_body_is_published(tmp_path, db):
    build_all(None, tmp_path, now=NOW)
    for path in tmp_path.rglob("*.json"):
        assert "raw_body" not in path.read_text()
    assert all("raw_body" not in e for e in read(tmp_path, "live.json")["events"])


def test_system_status_uses_last_completed_collection_not_live(tmp_path, db):
    build_all(None, tmp_path, now=NOW)
    status = read(tmp_path, "system-status.json")
    assert status["last_completed_collection"] == "2026-09-30T11:57:00Z"
    assert "live" not in json.dumps(status).lower()


def test_system_status_does_not_publish_run_error_text(tmp_path, db):
    build_all(None, tmp_path, now=NOW)
    status = read(tmp_path, "system-status.json")
    assert status["last_run"]["error_count"] == 1
    assert "boom" not in json.dumps(status)


def test_empty_database_still_publishes_valid_files(tmp_path, db):
    db.update(events=[], dates=[], completed=None, run=None, registry=[])
    build_all(None, tmp_path, now=NOW)
    assert read(tmp_path, "live.json")["events"] == []
    assert read(tmp_path, "live.json")["last_completed_collection"] is None
    assert read(tmp_path, "index.json") == {
        "generated_at": "2026-09-30T12:00:00Z", "pipeline_version": "1.0.0",
        "latest": None, "days": [], "merged": {},
    }
    assert read(tmp_path, "system-status.json")["last_run"] is None


def test_source_health_payload(tmp_path, db):
    build_all(None, tmp_path, now=NOW)
    sources = {s["source_id"]: s for s in read(tmp_path, "source-health.json")["sources"]}
    assert sources["acsc-alerts"]["latest"]["status"] == "ok"
    assert sources["acsc-alerts"]["lifecycle_state"] == "active"
    assert sources["old-feed"]["latest"] is None
    assert sources["old-feed"]["lifecycle_state"] == "retired"
    assert read(tmp_path, "source-health.json")["counts"] == {"ok": 1, "no_data": 1}


def test_every_published_event_is_rated(tmp_path, db):
    build_all(None, tmp_path, now=NOW)
    for name in ("live.json", "history/2026-09-30.json"):
        for event in read(tmp_path, name)["events"]:
            importance = event["importance"]
            assert set(importance) == {"version", "score", "tier", "reasons"}
            assert importance["tier"] in ("key", "notable", "routine")


def test_importance_reads_the_reputation_of_each_source(tmp_path, db):
    """A source config/sources.yaml lists carries its standing into the events it reports."""
    db["registry"].append(RegistryRow("asd", "ASD", "au", "advisory", "normal", True))
    db["health"]["asd"] = [
        SourceHealth(source_id="asd", checked_at=NOW, status=HealthStatus.OK, items_fetched=3)
    ]
    source = {
        "url": "https://www.asd.gov.au/news/x", "evidence_class": "AUTHORITATIVE",
        "independent": True,
    }
    db["events"] = [
        make_event(1, sources=[{**source, "source_id": "asd"}]),
        make_event(2, sources=[{**source, "source_id": "acsc-alerts"}]),
        make_event(3, sources=[{**source, "source_id": "gone"}]),
    ]
    build_all(None, tmp_path, now=NOW)
    events = {e["event_id"]: e["importance"] for e in read(tmp_path, "live.json")["events"]}
    asd, community, gone = (events[f"evt-2026-00000{n}"] for n in (1, 2, 3))
    # ASD: authoritative (90) and every check ok, so 92, and a quarter of it is 23.
    assert "reported by Australian Signals Directorate (authoritative)" in asd["reasons"]
    # Only the database has acsc-alerts, so it is community (40) and ok: 52, so 13.
    assert "reported by ACSC Alerts (community)" in community["reasons"]
    assert asd["score"] - community["score"] == 23 - 13
    # A source no longer registered counts as the floor, community's 40: 10, and says nothing.
    assert community["score"] - gone["score"] == 13 - 10
    assert not any(r.startswith("reported by") for r in gone["reasons"])


def test_source_health_describes_each_source_from_the_registry(tmp_path, db):
    db["registry"].append(RegistryRow(
        "abc_cyber", "ABC News Cyber Security", "au", "news", "normal", True,
        url="https://www.abc.net.au/news/feed/104475720/rss.xml", priority=3,
        expected_frequency="weekly",
    ))
    db["health"]["abc_cyber"] = [
        SourceHealth(source_id="abc_cyber", checked_at=NOW - timedelta(hours=h),
                     status=status, items_fetched=5)
        for h, status in ((8, HealthStatus.OK), (4, HealthStatus.ERROR), (0, HealthStatus.OK))
    ]
    db["corroboration"] = {"abc_cyber": Corroboration(events=20, corroborated=5)}
    build_all(None, tmp_path, now=NOW)
    sources = {s["source_id"]: s for s in read(tmp_path, "source-health.json")["sources"]}
    abc = sources["abc_cyber"]
    assert abc["description"].startswith("Cyber security news from the ABC")
    assert (abc["publisher"], abc["standing"], abc["beat"]) == ("ABC", "established", "cyber")
    assert (abc["url"], abc["priority"], abc["expected_frequency"]) == (
        "https://www.abc.net.au/news/feed/104475720/rss.xml", 3, "weekly",
    )
    # 0.6 * 75 + 0.15 * 66.7 + 0.25 * 25 = 61.25
    assert abc["reputation"] == {
        "version": "1", "score": 61, "standing": "established", "uptime": 0.667,
        "corroboration": 0.25, "events_90d": 20,
        "basis": (
            "standing, uptime over the last 3 checks and corroboration of 20 events in 90 days"
        ),
    }
    assert abc["attention"] is None
    # Only the database has these two: community, with no description and the cyber beat.
    feed = sources["acsc-alerts"]
    assert (feed["description"], feed["publisher"], feed["standing"], feed["beat"]) == (
        None, None, "community", "cyber",
    )
    assert feed["reputation"]["basis"].startswith("standing and uptime over the last check;")


def _health(*statuses, age=None, error=None):
    return [
        SourceHealth(source_id="s", checked_at=NOW - timedelta(hours=len(statuses) - i),
                     status=s, newest_item_age_days=age, error=error if s != "ok" else None)
        for i, s in enumerate(statuses)
    ]


@pytest.mark.parametrize(
    ("enabled", "state", "history", "expected"),
    [
        (True, LifecycleState.ACTIVE, _health("ok", "ok"), None),
        (True, LifecycleState.ACTIVE, [], None),
        (True, LifecycleState.TESTING, _health("error", "ok"), None),
        (False, LifecycleState.RETIRED, _health("error"), None),
        (False, LifecycleState.DISCOVERED, [], ("coming", "Not collected yet.")),
        (True, LifecycleState.CANDIDATE, [],
         ("coming", "A new find, on trial before it is collected.")),
        (True, LifecycleState.ACTIVE, _health("ok", "timeout", error="ReadTimeout after 30s"),
         ("fix", "The last check timed out: ReadTimeout after 30s")),
        (True, LifecycleState.ACTIVE, _health("error", error="HTTP 404"),
         ("fix", "The last check failed: HTTP 404")),
        (True, LifecycleState.ACTIVE, _health("empty"),
         ("fix", "The last check came back empty.")),
        (True, LifecycleState.DEGRADED, _health("ok", "stale", "stale", "stale", age=9.79),
         ("fix", "Degraded: 3 unhealthy checks in a row, nothing new in 9.8 days "
                 "(it usually publishes daily).")),
        (True, LifecycleState.ACTIVE, _health("ok", "stale", age=8.25),
         ("watch", "Stale: nothing new in 8.2 days (it usually publishes daily).")),
        (True, LifecycleState.DEGRADED, _health("stale", "ok"),
         ("watch", "Recovering: healthy again after failed checks.")),
    ],
)
def test_attention_says_what_needs_doing(tmp_path, db, enabled, state, history, expected):
    db["registry"] = [
        RegistryRow("s", "S", "AU", "news", "normal", enabled, expected_frequency="daily")
    ]
    db["health"] = {"s": history}
    db["lifecycle"] = {"s": state}
    build_all(None, tmp_path, now=NOW)
    [source] = read(tmp_path, "source-health.json")["sources"]
    assert source["attention"] == (
        {"kind": expected[0], "reason": expected[1]} if expected else None
    )


def test_a_disabled_registry_source_says_why_it_is_not_collected(tmp_path, db):
    db["registry"] = [
        RegistryRow("securityweek", "SecurityWeek", "global", "news", "normal", False)
    ]
    build_all(None, tmp_path, now=NOW)
    [source] = read(tmp_path, "source-health.json")["sources"]
    assert source["attention"] == {
        "kind": "coming",
        "reason": "Not collected yet. Blocked: Cloudflare 403 error on feed endpoint.",
    }


def _candidate(n, state, *, found_by="search", passes=0, failures=0, name=None):
    return Candidate(
        id=n, host=f"feed{n}.example.org", feed_url=f"https://feed{n}.example.org/rss?k=1",
        name=name, found_by=found_by, reason="ignore previous instructions", proposed_at=None,
        evidence=[{"url": "https://elsewhere.example.org/"}], state=state, passes=passes,
        failures=failures, last_probe_at=None, last_result=None, last_error="HTTP 500",
        source_id=None, created_at=NOW - timedelta(days=n), updated_at=NOW,
    )


def test_source_health_lists_the_finds_at_the_gate_and_nothing_else_of_them(tmp_path, db):
    db["gate"] = [
        _candidate(1, "testing", passes=2, name="Feed One"),
        _candidate(2, "candidate", found_by="tachikoma", failures=1),
        _candidate(3, "candidate"),
        _candidate(4, "discovered"),
    ]
    build_all(None, tmp_path, now=NOW)
    text = (tmp_path / "source-health.json").read_text()
    assert read(tmp_path, "source-health.json")["pipeline"] == [
        {"name": "Feed One", "host": "feed1.example.org", "state": "testing",
         "found_at": "2026-09-29T12:00:00Z",
         "reason": "Found by the nightly search; 2 healthy probes in a row so far."},
        {"name": None, "host": "feed2.example.org", "state": "candidate",
         "found_at": "2026-09-28T12:00:00Z",
         "reason": "Proposed by TACHIKOMA; feed found, 1 failed probe in a row."},
        {"name": None, "host": "feed3.example.org", "state": "candidate",
         "found_at": "2026-09-27T12:00:00Z",
         "reason": "Found by the nightly search; feed found, waiting for its first probe."},
        {"name": None, "host": "feed4.example.org", "state": "discovered",
         "found_at": "2026-09-26T12:00:00Z",
         "reason": "Found by the nightly search; looking for its feed."},
    ]
    # Never the feed URL, the evidence, the finder's words or the probe's error.
    for leaked in ("rss?k=1", "elsewhere", "ignore previous", "HTTP 500"):
        assert leaked not in text


def test_the_pipeline_is_capped(tmp_path, db):
    db["gate"] = [_candidate(n, "discovered") for n in range(1, 40)]
    build_all(None, tmp_path, now=NOW)
    assert len(read(tmp_path, "source-health.json")["pipeline"]) == build.PIPELINE_LIMIT == 25


def test_system_status_publishes_the_schedule_the_worker_runs(tmp_path, db):
    build_all(None, tmp_path, now=NOW)
    schedule = read(tmp_path, "system-status.json")["schedule"]
    assert schedule == public_schedule()
    assert [j["job"] for j in schedule][:2] == ["lane-fast", "lane-normal"]
    assert {j["kind"] for j in schedule} <= {"code", "ai", "mixed"}
    # No budget figures and nothing internal: the summaries are for readers.
    text = json.dumps(schedule)
    for leaked in ("$", "USD", "worker:", "http", "key"):
        assert leaked not in text


def test_naive_now_is_rejected_before_anything_is_written(tmp_path, db):
    with pytest.raises(ValueError):
        build_all(None, tmp_path / "out", now=datetime(2026, 9, 30, 12, 0))
    assert not (tmp_path / "out").exists()


# --- fail closed (Review Focus #5) -------------------------------------------------------


def test_build_writes_nothing_when_a_secret_is_found(tmp_path, db, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", OR_KEY)
    db["events"] = [make_event(1, summary=f"contains {OR_KEY}")]
    with pytest.raises(ValidationFailure, match="secret") as exc:
        build_all(None, tmp_path, now=NOW)
    assert list(tmp_path.iterdir()) == []
    assert OR_KEY not in str(exc.value)


@pytest.mark.parametrize(
    "where",
    [
        "title", "summary", "why_it_matters",
        "tags", "domains", "categories",
        "claims", "sources", "timeline", "entities", "cves", "mitre",
    ],
)
def test_a_secret_in_any_event_field_blocks_every_write(tmp_path, db, where):
    """Not just the summary: any string the model can carry must be caught."""
    secret = "ghp_0123456789abcdefghij0123456789abcdef"
    fields = {
        "title": {"title": secret},
        "summary": {"summary": secret},
        "why_it_matters": {"why_it_matters": secret},
        "tags": {"tags": ["ok", secret]},
        "domains": {"domains": [secret]},
        "categories": {"categories": [secret]},
        "claims": {"claims": [{"text": "x", "confidence": 0.5, "evidence": [secret]}]},
        "sources": {"sources": [{
            "source_id": "acsc-alerts", "url": f"https://x.test/?token={secret}",
            "evidence_class": "NEWS",
        }]},
        "timeline": {"timeline": [{
            "timestamp": NOW, "type": "NEW_FACT", "summary": secret, "sources": [],
        }]},
        "entities": {"entities": {"actors": [secret]}},
        "cves": {"cves": [{"id": "CVE-2026-1234", "cvss": {
            "score": 5.0, "vector": None, "source": secret,
        }}]},
        "mitre": {"mitre_techniques": [{
            "id": "T1059", "name": secret, "confidence": 0.5, "confidence_type": "rule",
            "dataset_version": "v1", "evidence": [],
        }]},
    }[where]
    db["events"] = [make_event(1), make_event(2, **fields)]
    with pytest.raises(ValidationFailure, match="secret"):
        build_all(None, tmp_path, now=NOW)
    assert list(tmp_path.iterdir()) == []


def test_a_secret_in_source_health_error_blocks_every_write(tmp_path, db):
    secret = "postgresql://cyber:hunter2@db:5432/cyber_intel"
    db["health"]["acsc-alerts"] = [
        SourceHealth(source_id="acsc-alerts", checked_at=NOW, status=HealthStatus.ERROR,
                     error=f"connect failed: {secret}")
    ]
    with pytest.raises(ValidationFailure, match="secret"):
        build_all(None, tmp_path, now=NOW)
    assert list(tmp_path.iterdir()) == []


def test_a_secret_in_a_source_name_blocks_every_write(tmp_path, db):
    db["registry"] = [RegistryRow("s", "tvly-abcdef0123456789abcdef0123", "AU", "x", "fast", True)]
    with pytest.raises(ValidationFailure, match="secret"):
        build_all(None, tmp_path, now=NOW)
    assert list(tmp_path.iterdir()) == []


def test_a_secret_leaves_previous_output_untouched(tmp_path, db, monkeypatch):
    build_all(None, tmp_path, now=NOW)
    before = snapshot(tmp_path)
    monkeypatch.setenv("OPENROUTER_API_KEY", OR_KEY)
    db["events"] = [make_event(1, summary=f"contains {OR_KEY}")]
    with pytest.raises(ValidationFailure, match="secret"):
        build_all(None, tmp_path, now=NOW + timedelta(minutes=15))
    assert snapshot(tmp_path) == before


def test_build_never_creates_out_dir_on_failure(tmp_path, db, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", OR_KEY)
    db["events"] = [make_event(1, summary=OR_KEY)]
    target = tmp_path / "nested" / "data"
    with pytest.raises(ValidationFailure):
        build_all(None, target, now=NOW)
    assert not target.exists() and not target.parent.exists()


def test_secret_only_visible_in_serialised_text_still_blocks(tmp_path, db, monkeypatch):
    """The structural walk and the on-disk text are scanned independently."""
    monkeypatch.setattr(build, "scan_for_secrets", lambda payload: [])
    db["events"] = [make_event(1, summary=f"contains {OR_KEY}")]
    with pytest.raises(ValidationFailure, match="secret"):
        build_all(None, tmp_path, now=NOW)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.filterwarnings("ignore::UserWarning")
def test_build_is_atomic_on_schema_failure(tmp_path, db):
    (tmp_path / "live.json").write_text('{"previous": true}')
    bad = make_event(1)
    bad.severity = "spicy"  # models do not validate on assignment
    db["events"] = [bad]
    with pytest.raises(ValidationFailure):
        build_all(None, tmp_path, now=NOW)
    assert json.loads((tmp_path / "live.json").read_text()) == {"previous": True}
    assert sorted(p.name for p in tmp_path.iterdir()) == ["live.json"]


def test_the_index_maps_merged_events_to_the_event_they_joined(tmp_path, db):
    db["merged"] = {"evt-2026-000007": "evt-2026-000001"}
    build_all(None, tmp_path, now=NOW)
    assert read(tmp_path, "index.json")["merged"] == {"evt-2026-000007": "evt-2026-000001"}


def test_a_malformed_merged_entry_blocks_every_write(tmp_path, db):
    db["merged"] = {"evt-2026-000007": "https://example.test/elsewhere"}
    with pytest.raises(ValidationFailure, match="index"):
        build_all(None, tmp_path, now=NOW)
    assert list(tmp_path.iterdir()) == []


def test_schema_failure_in_a_later_file_blocks_earlier_files(tmp_path, db):
    """The invalid payload is not live.json (written last) - nothing may be written."""
    db["registry"] = [RegistryRow("s", "n", "AU", "x", "sideways", True)]
    with pytest.raises(ValidationFailure, match="source-health"):
        build_all(None, tmp_path, now=NOW)
    assert list(tmp_path.iterdir()) == []


def test_an_unhydratable_stored_event_fails_closed(tmp_path, db, monkeypatch):
    def boom(conn, **kw):
        Event(event_id="bad", title="t", summary="s", first_seen=NOW, last_seen=NOW)

    monkeypatch.setattr(build, "load_live_events", boom)
    (tmp_path / "live.json").write_text('{"previous": true}')
    with pytest.raises(ValidationFailure, match="event_id"):
        build_all(None, tmp_path, now=NOW)
    assert json.loads((tmp_path / "live.json").read_text()) == {"previous": True}


def test_failure_while_moving_restores_previous_output(tmp_path, db, monkeypatch):
    build_all(None, tmp_path, now=NOW)
    before = snapshot(tmp_path)

    real_replace = os.replace
    calls = {"n": 0}

    def flaky(src, dst):
        calls["n"] += 1
        if calls["n"] == 3:
            raise OSError("disk went away")
        return real_replace(src, dst)

    monkeypatch.setattr(build.os, "replace", flaky)
    db["events"] = [make_event(9, title="Brand new title")]
    with pytest.raises(OSError, match="disk went away"):
        build_all(None, tmp_path, now=NOW + timedelta(minutes=15))
    monkeypatch.setattr(build.os, "replace", real_replace)
    assert snapshot(tmp_path) == before


def test_failure_while_moving_into_an_empty_dir_leaves_it_empty(tmp_path, db, monkeypatch):
    real_replace = os.replace
    calls = {"n": 0}

    def flaky(src, dst):
        calls["n"] += 1
        if calls["n"] == 4:
            raise OSError("disk went away")
        return real_replace(src, dst)

    monkeypatch.setattr(build.os, "replace", flaky)
    with pytest.raises(OSError):
        build_all(None, tmp_path, now=NOW)
    monkeypatch.setattr(build.os, "replace", real_replace)
    assert snapshot(tmp_path) == {}


def test_staging_directory_never_survives(tmp_path, db):
    build_all(None, tmp_path, now=NOW)
    assert not [p for p in tmp_path.iterdir() if p.name.startswith(build.STAGING_PREFIX)]


def test_stale_staging_from_a_crashed_run_is_swept(tmp_path, db):
    crashed = tmp_path / f"{build.STAGING_PREFIX}dead"
    (crashed / "new").mkdir(parents=True)
    (crashed / "new" / "live.json").write_text("{}")
    build_all(None, tmp_path, now=NOW)
    assert not crashed.exists()


def test_rebuild_replaces_previous_output(tmp_path, db):
    build_all(None, tmp_path, now=NOW)
    db["events"] = [make_event(7, prominence=0.8)]
    build_all(None, tmp_path, now=NOW + timedelta(minutes=15))
    live = read(tmp_path, "live.json")
    assert [e["event_id"] for e in live["events"]] == ["evt-2026-000007"]
    assert live["generated_at"] == "2026-09-30T12:15:00Z"


def test_crew_json_counts_only_what_the_worker_does(tmp_path, db):
    build_all(None, tmp_path, now=NOW)
    crew = read(tmp_path, "crew.json")
    agents = {a["callsign"]: a for a in crew["agents"]}
    assert set(agents) == {"SERAPH", "RIPPERDOC"}
    assert crew["month"] == "2026-09"
    # SERAPH is doing its publish job by writing the file, so its last work is now.
    assert agents["SERAPH"] == {
        "callsign": "SERAPH", "status": "active", "tasks_completed": 894,
        "items_processed": 4045, "cost_usd": 0.0, "last_active_at": "2026-09-30T12:00:00Z",
    }
    # RIPPERDOC's model runs are in Paperclip, which the worker never reads: no cost, not 0.
    assert agents["RIPPERDOC"] == {
        "callsign": "RIPPERDOC", "status": "idle", "tasks_completed": 0, "items_processed": None,
        "cost_usd": None, "last_active_at": None,
    }
    assert crew["pipeline_ai"] == {"calls": 12, "cost_usd": 0.0412}


def test_seraph_stays_active_between_fast_runs_at_any_cadence(tmp_path, db):
    # An enrichment pass publishes between runs, and a restart can cost one run. SERAPH's
    # publish job is writing this file, so it reads active however old its last source check is.
    db["crew"]["seraph"] = Activity(894, 4045, NOW - timedelta(hours=3))
    build_all(None, tmp_path, now=NOW)
    agents = {a["callsign"]: a for a in read(tmp_path, "crew.json")["agents"]}
    assert agents["SERAPH"]["status"] == "active"
    assert agents["SERAPH"]["last_active_at"] == "2026-09-30T12:00:00Z"


def test_crew_json_degrades_seraph_when_ground_truth_fails(tmp_path, db):
    db["crew"]["seraph"] = Activity(894, 4045, NOW - timedelta(minutes=5), failing=True)
    build_all(None, tmp_path, now=NOW)
    agents = {a["callsign"]: a for a in read(tmp_path, "crew.json")["agents"]}
    # The newest ground-truth pass raised: degraded, whatever the last good one says.
    assert agents["SERAPH"]["status"] == "degraded"


@pytest.mark.parametrize("hours_ago, status", [(2, "active"), (23, "active"), (25, "idle")])
def test_crew_json_ripperdoc_is_active_for_a_day(tmp_path, db, hours_ago, status):
    """Active within 24 hours of its last ledger row (CREW_FRESH), idle after."""
    db["crew"]["ripperdoc"] = Activity(3, None, NOW - timedelta(hours=hours_ago))
    build_all(None, tmp_path, now=NOW)
    agents = {a["callsign"]: a for a in read(tmp_path, "crew.json")["agents"]}
    assert agents["RIPPERDOC"]["status"] == status
    assert agents["RIPPERDOC"]["tasks_completed"] == 3


def test_the_site_roster_has_every_agent_crew_json_names():
    hud = (Path(__file__).resolve().parents[2] / "site/assets/hud.js").read_text()
    for callsign in ("SERAPH", "RIPPERDOC"):
        assert f"callsign: '{callsign}'" in hud
