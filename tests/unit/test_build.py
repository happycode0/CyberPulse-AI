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
from worker.db.crew import Activity, PipelineAi, Seen
from worker.db.discovery import Candidate, GateCounts
from worker.db.jobs import JobRun
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
        # A curated source: an event with none is held back (worker/publish/hold.py).
        "sources": [{"source_id": "acsc-alerts", "url": f"https://example.org/{n}",
                     "evidence_class": "AUTHORITATIVE"}],
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
        },
        "pipeline_ai": PipelineAi(12, Decimal("0.0412"), NOW - timedelta(minutes=25),
                                  "openrouter"),
        "unfinished": [],
        "lanes": {"fast": (NOW - timedelta(minutes=5), NOW - timedelta(minutes=3))},
        "jobs": {},
        "completed_jobs": {},
        "gate_counts": GateCounts(ready_waiting=0, testing=0, queued=0, homes_waiting=0,
                                  at_the_gate=0, rejected=0, retired=0),
        "settled": [],
        "active_discovered": 0,
        "seen": {name: Seen(None, 0) for name in ("tachikoma", "deckard", "teletraan")},
        "daily_report": None,
    }
    monkeypatch.setattr(build, "load_live_events", lambda conn, **kw: state["events"])
    monkeypatch.setattr(build, "load_event_dates", lambda conn: state["dates"])
    monkeypatch.setattr(build, "load_last_completed_collection", lambda conn: state["completed"])
    monkeypatch.setattr(build, "load_latest_finished_run", lambda conn: state["run"])
    monkeypatch.setattr(build, "load_unfinished_runs", lambda conn, **kw: state["unfinished"])
    monkeypatch.setattr(build, "load_lane_activity", lambda conn: state["lanes"])
    monkeypatch.setattr(
        build, "load_latest_jobs",
        lambda conn, completed_only=False: state["completed_jobs" if completed_only else "jobs"],
    )
    monkeypatch.setattr(build, "load_gate_counts", lambda conn, **kw: state["gate_counts"])
    monkeypatch.setattr(build, "load_settled", lambda conn, **kw: state["settled"][:kw["limit"]])
    monkeypatch.setattr(build, "active_discovered", lambda conn: state["active_discovered"])
    monkeypatch.setattr(build, "load_crew_seen", lambda conn, **kw: state["seen"])
    monkeypatch.setattr(build, "load_latest_report", lambda conn: state["daily_report"])
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
        "crew.json", "daily-report.json", "history/2026-09-30.json",
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


def test_a_held_event_is_in_no_file_and_is_counted(tmp_path, db):
    """worker/publish/hold.py: the off-beat item of a mostly off-beat discovered source, and a
    directory listing, appear on neither the live page nor their day, and the index's count and
    system-status.json agree."""
    found = [{"source_id": "found_theguardian_com", "url": "https://www.theguardian.com/x",
              "evidence_class": "COMMUNITY"}]
    db["events"] = [
        make_event(1, title="Medibank hackers leak customer records", sources=found,
                   domains=["cybersecurity"]),
        make_event(2, title="How to turn brown bananas into vegan bread – recipe",
                   summary="Waste not.", sources=found, domains=["cybersecurity"]),
        make_event(3, title="Clayface review – very gory but not very good",
                   summary="Body horror.", sources=found, domains=["cybersecurity"]),
        make_event(4, title="plex-mcp-server by vladimir-tutin — ★ 150"),
        make_event(5),
    ]
    build_all(None, tmp_path, now=NOW)
    kept = ["evt-2026-000001", "evt-2026-000005"]
    assert [e["event_id"] for e in read(tmp_path, "live.json")["events"]] == kept
    assert [e["event_id"] for e in read(tmp_path, "history/2026-09-30.json")["events"]] == kept
    assert read(tmp_path, "index.json")["days"][0]["event_count"] == 2
    counts = read(tmp_path, "system-status.json")["counts"]
    assert (counts["events_total"], counts["events_held"]) == (2, 3)


def test_a_stored_summary_is_published_without_its_feed_boilerplate(tmp_path, db):
    db["events"] = [
        make_event(1, summary="Patch now. The post Patch now appeared first on SecurityWeek."),
        make_event(2, summary="Continue reading..."),
    ]
    build_all(None, tmp_path, now=NOW)
    summaries = [e["summary"] for e in read(tmp_path, "live.json")["events"]]
    assert summaries == ["Patch now.", "Event 2"]


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
        (False, LifecycleState.DISCOVERED, [], ("blocked", "Not collected.")),
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
    """Blocked, not coming: nothing moves a disabled curated source on, so the six of them used to
    sit under COMING for good."""
    db["registry"] = [
        RegistryRow("securityweek", "SecurityWeek", "global", "news", "normal", False)
    ]
    build_all(None, tmp_path, now=NOW)
    [source] = read(tmp_path, "source-health.json")["sources"]
    assert source["attention"] == {
        "kind": "blocked",
        "reason": "Not collected. Blocked: Cloudflare 403 error on feed endpoint.",
    }


def _candidate(n, state, *, found_by="search", passes=0, failures=0, name=None, feed=None):
    # A find is `discovered` without a feed, or with one when it is a queued proposal.
    feed = state != "discovered" if feed is None else feed
    return Candidate(
        id=n, host=f"feed{n}.example.org",
        feed_url=f"https://feed{n}.example.org/rss?k=1" if feed else None,
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
        _candidate(5, "discovered", found_by="tachikoma", feed=True),
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
        {"name": None, "host": "feed5.example.org", "state": "discovered",
         "found_at": "2026-09-25T12:00:00Z",
         "reason": "Proposed by TACHIKOMA; feed found, queued until a testing slot frees."},
    ]
    # Never the feed URL, the evidence, the finder's words or the probe's error.
    for leaked in ("rss?k=1", "elsewhere", "ignore previous", "HTTP 500"):
        assert leaked not in text


def test_the_pipeline_is_capped(tmp_path, db):
    db["gate"] = [_candidate(n, "discovered") for n in range(1, 40)]
    build_all(None, tmp_path, now=NOW)
    assert len(read(tmp_path, "source-health.json")["pipeline"]) == build.PIPELINE_LIMIT == 25


def test_a_ready_find_says_it_passed_the_gate(tmp_path, db):
    ready = build.DiscoveryConfig.load().gate.probes_to_activate
    db["gate"] = [_candidate(1, "testing", passes=ready + 3),
                  _candidate(2, "testing", passes=ready - 1)]
    build_all(None, tmp_path, now=NOW)
    reasons = [p["reason"] for p in read(tmp_path, "source-health.json")["pipeline"]]
    assert reasons == [
        "Found by the nightly search; passed the gate, waiting for one of the 25 "
        "discovered-source slots.",
        f"Found by the nightly search; {ready - 1} healthy probes in a row so far.",
    ]


def test_source_health_says_where_discovery_stands(tmp_path, db):
    """The truth behind the old COMING 31: 25 of 25 slots taken and finds waiting for one."""
    gate = build.DiscoveryConfig.load().gate
    db["active_discovered"] = 25
    db["gate_counts"] = GateCounts(ready_waiting=39, testing=4, queued=2, homes_waiting=15,
                                   at_the_gate=60, rejected=31, retired=8)
    settled = [
        _candidate(7, "active", name="DEV Community"),
        _candidate(8, "retired"),
    ]
    db["settled"] = [
        c.__class__(**{**c.__dict__, "source_id": f"found_{c.id}",
                       "updated_at": NOW - timedelta(days=c.id - 6)})
        for c in settled
    ]
    db["completed_jobs"] = {
        "source-gate": JobRun(
            "source-gate", NOW - timedelta(hours=1), NOW - timedelta(minutes=59), True,
            detail={"homes": 5, "feeds_found": 0, "probes": 10, "healthy": 7, "rejected": 3,
                    "activated": ["found_dev_to"], "retired": [],
                    "pruned": ["found_spiceworks_com"], "waiting": 39, "promoted": 0},
        ),
        "discovery": JobRun(
            "discovery", NOW - timedelta(hours=9), NOW - timedelta(hours=9), True,
            detail={"searches": 16, "hits": 140, "new_hosts": 4, "credits": 16,
                    "skipped": False},
        ),
    }
    db["jobs"] = dict(db["completed_jobs"])
    build_all(None, tmp_path, now=NOW)
    summary = read(tmp_path, "source-health.json")["discovery"]
    assert summary == {
        "active": 25, "max_active": gate.max_active, "ready_waiting": 39, "testing": 4,
        "queued": 2, "homes_waiting": 15, "at_the_gate": 60, "shown": 0,
        "rejected_total": 31, "retired_total": 8,
        "probes_to_activate": gate.probes_to_activate, "max_open": gate.max_open,
        "last_gate": {
            "finished_at": "2026-09-30T11:01:00Z", "completed": True, "probes": 10,
            "healthy": 7, "rejected": 3, "activated": ["found_dev_to"], "retired": [],
            "pruned": ["found_spiceworks_com"], "waiting": 39, "promoted": 0,
        },
        "last_search": {
            "finished_at": "2026-09-30T03:00:00Z", "completed": True, "searches": 16,
            "new_hosts": 4, "skipped": False,
        },
        "recent": [
            {"name": "DEV Community", "host": "feed7.example.org", "state": "active",
             "at": "2026-09-29T12:00:00Z", "found_by": "search"},
            {"name": None, "host": "feed8.example.org", "state": "retired",
             "at": "2026-09-28T12:00:00Z", "found_by": "search"},
        ],
    }


def test_a_gate_pass_that_raised_is_reported_without_counts(tmp_path, db):
    db["jobs"] = {"source-gate": JobRun("source-gate", NOW, NOW, False, note="RuntimeError")}
    build_all(None, tmp_path, now=NOW)
    summary = read(tmp_path, "source-health.json")["discovery"]
    assert summary["last_gate"] == {"finished_at": "2026-09-30T12:00:00Z", "completed": False}
    assert summary["last_search"] is None
    assert "RuntimeError" not in (tmp_path / "source-health.json").read_text()


def test_system_status_reports_the_last_finished_run_and_what_is_still_going(tmp_path, db):
    """28 Oct: the NORMAL run started with the FAST one and was still going when FAST published,
    so the page read "0 sources checked" for two days."""
    db["unfinished"] = [
        RunSummary(run_id="run-normal", lane=Lane.NORMAL, started_at=NOW - timedelta(minutes=5),
                   sources_ok=0, sources_failed=0, sources_stale=0, items_fetched=0,
                   new_events=0, updated_events=0, duplicates=0, archived_events=0),
    ]
    build_all(None, tmp_path, now=NOW)
    status = read(tmp_path, "system-status.json")
    assert status["last_run"]["run_id"] == "run-1"
    assert status["last_run"]["finished_at"] == "2026-09-30T11:57:00Z"
    assert status["runs_in_progress"] == [
        {"run_id": "run-normal", "lane": "normal", "started_at": "2026-09-30T11:55:00Z"},
    ]


def test_system_status_says_how_each_job_last_went(tmp_path, db):
    hour = timedelta(hours=1)
    db["lanes"] = {"fast": (NOW - timedelta(minutes=5), NOW - timedelta(minutes=3)),
                   "normal": (NOW - timedelta(minutes=5), NOW - 4 * hour)}
    db["jobs"] = {
        "groundtruth": JobRun("groundtruth", NOW - 2 * hour, NOW - 2 * hour, True, changed=True),
        "publish": JobRun("publish", NOW - hour, NOW - hour, False, note="ValidationFailure"),
        "push": JobRun("push", NOW - 3 * hour, NOW - 3 * hour, True),
    }
    db["completed_jobs"] = {
        "groundtruth": db["jobs"]["groundtruth"],
        "publish": JobRun("publish", NOW - 2 * hour, NOW - 2 * hour, True),
        "push": db["jobs"]["push"],
    }
    build_all(None, tmp_path, now=NOW)
    jobs = {j["job"]: j for j in read(tmp_path, "system-status.json")["jobs"]}
    # Every scheduled job has a row, then the publish and the push after each run.
    assert list(jobs) == [j["job"] for j in public_schedule()] + ["publish", "push"]
    assert jobs["lane-fast"] == {
        "job": "lane-fast", "label": "Fast lane", "recorded": True, "running": False,
        "last_run_at": "2026-09-30T11:55:00Z", "last_success_at": "2026-09-30T11:57:00Z",
        "ok": True,
    }
    # The newest NORMAL run is still going; its last finished one was four hours ago.
    assert jobs["lane-normal"]["running"] is True and jobs["lane-normal"]["ok"] is True
    assert jobs["lane-normal"]["last_success_at"] == "2026-09-30T08:00:00Z"
    assert jobs["groundtruth-sync"]["ok"] is True
    assert jobs["groundtruth-sync"]["last_run_at"] == "2026-09-30T10:00:00Z"
    assert jobs["publish"] == {
        "job": "publish", "label": "Publish", "recorded": True, "running": False,
        "last_run_at": "2026-09-30T11:00:00Z", "last_success_at": "2026-09-30T10:00:00Z",
        "ok": False,
    }
    # A job that leaves no row says so, rather than looking healthy or broken.
    assert jobs["daily-digest"] == {
        "job": "daily-digest", "label": "Daily digest", "recorded": False, "running": False,
        "last_run_at": None, "last_success_at": None, "ok": None,
    }
    assert "ValidationFailure" not in (tmp_path / "system-status.json").read_text()


def test_system_status_says_where_the_ai_layer_stands(tmp_path, db):
    db["events"] = [make_event(1, pending_enrichment=True),
                    make_event(2, prominence=0.6, pending_enrichment=True), make_event(3)]
    db["jobs"] = {"enrichment": JobRun("enrichment", NOW - timedelta(minutes=25),
                                       NOW - timedelta(minutes=25), True,
                                       detail={"mode": "free_only"})}
    build_all(None, tmp_path, now=NOW, ai_budget_usd=Decimal("12"))
    ai = read(tmp_path, "system-status.json")["ai"]
    assert ai == {
        "provider": "openrouter", "monthly_budget_usd": 12.0, "mode": "free_only",
        "calls_this_month": 12, "cost_this_month_usd": 0.0412,
        "last_call_at": "2026-09-30T11:35:00Z", "last_pass_at": "2026-09-30T11:35:00Z",
        "events": 3, "awaiting": 2,
    }


def test_the_ai_state_without_a_recorded_mode_or_a_budget(tmp_path, db):
    db["pipeline_ai"] = PipelineAi(0, Decimal(0))
    build_all(None, tmp_path, now=NOW)
    ai = read(tmp_path, "system-status.json")["ai"]
    assert (ai["provider"], ai["mode"], ai["monthly_budget_usd"], ai["last_call_at"]) == (
        None, None, None, None
    )


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


def test_an_allowlisted_url_param_is_dropped_from_the_published_source(tmp_path, db):
    """INC-22: Cisco's advisory search encodes opaque state in `vs_f`, high-entropy enough
    to read as a secret. The allowlisted (host, param) is dropped so publish does not block;
    an unlisted param on the same host is left alone, proving the match is on both parts."""
    secret = "ghp_0123456789abcdefghij0123456789abcdef"  # same shape test_build.py proves blocks
    db["events"] = [make_event(1, sources=[{
        "source_id": "acsc-alerts",
        "url": f"https://sec.cloudapps.cisco.com/security/center/x?vs_f={secret}&id=42",
        "evidence_class": "NEWS",
    }])]
    build_all(None, tmp_path, now=NOW)
    url = read(tmp_path, "live.json")["events"][0]["sources"][0]["url"]
    assert "vs_f" not in url
    assert "id=42" in url


def test_a_non_allowlisted_param_on_the_same_host_still_blocks(tmp_path, db):
    secret = "ghp_0123456789abcdefghij0123456789abcdef"
    db["events"] = [make_event(1, sources=[{
        "source_id": "acsc-alerts",
        "url": f"https://sec.cloudapps.cisco.com/security/center/x?other={secret}",
        "evidence_class": "NEWS",
    }])]
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
    # RIPPERDOC was counted from the cost ledger until it was retired on 2026-10-20; the
    # worker's own model calls are pipeline_ai.
    assert set(agents) == {"SERAPH"}
    assert crew["month"] == "2026-09"
    # SERAPH is doing its publish job by writing the file, so its last work is now.
    assert agents["SERAPH"] == {
        "callsign": "SERAPH", "status": "active", "tasks_completed": 894,
        "items_processed": 4045, "cost_usd": 0.0, "last_active_at": "2026-09-30T12:00:00Z",
    }
    assert crew["pipeline_ai"]["calls"] == 12 and crew["pipeline_ai"]["cost_usd"] == 0.0412


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


def test_seraph_is_degraded_while_pushes_fail(tmp_path, db):
    db["jobs"] = {"push": JobRun("push", NOW, NOW, False, note="RuntimeError")}
    build_all(None, tmp_path, now=NOW)
    agents = {a["callsign"]: a for a in read(tmp_path, "crew.json")["agents"]}
    # The site keeps the last data it was sent, so a push that fails is SERAPH's problem.
    assert agents["SERAPH"]["status"] == "degraded"


def test_crew_json_says_what_the_worker_saw_of_the_crew(tmp_path, db):
    db["seen"] = {
        "tachikoma": Seen(datetime(2026, 9, 6, 3, 30, tzinfo=UTC), 0),
        "deckard": Seen(NOW - timedelta(days=16), 4),
        "teletraan": Seen(None, 0),
    }
    build_all(None, tmp_path, now=NOW)
    crew = read(tmp_path, "crew.json")
    assert crew["seen"] == [
        {"callsign": "TACHIKOMA", "what": "source proposal", "last_at": "2026-09-06T03:30:00Z",
         "this_month": 0},
        {"callsign": "DECKARD", "what": "follow-up report", "last_at": "2026-09-14T12:00:00Z",
         "this_month": 4},
        {"callsign": "TELETRAAN", "what": "incident verdict", "last_at": None, "this_month": 0},
    ]
    assert crew["pipeline_ai"] == {"calls": 12, "cost_usd": 0.0412,
                                   "last_call_at": "2026-09-30T11:35:00Z",
                                   "provider": "openrouter"}


def test_the_site_roster_has_every_agent_crew_json_names():
    hud = (Path(__file__).resolve().parents[2] / "site/assets/hud.js").read_text()
    assert "callsign: 'SERAPH'" in hud
    # Retired: an older crew.json that still names it is not counted (hud.js renderCrew).
    assert "callsign: 'RIPPERDOC'" not in hud


# ─── daily-report.json (worker/ai/report.py) ──────────────────────────────────────────────────────


def test_no_report_yet_publishes_none(tmp_path, db):
    build_all(None, tmp_path, now=NOW)
    assert read(tmp_path, "daily-report.json")["report"] is None


def test_the_latest_report_is_published_with_only_the_events_the_site_has(tmp_path, db):
    db["daily_report"] = {
        "report_date": NOW.date(),
        "generated_at": NOW - timedelta(minutes=20),
        "window_hours": 24,
        "summary": "A quiet day apart from one breach.",
        "items": [
            {"event_id": "evt-2026-000001", "why": "It matters for defenders."},
            {"event_id": "evt-2026-999999", "why": "Held back, so not on the site."},
        ],
        "gaps": [{"id": "category:ddos", "topic": "ddos", "kind": "category", "quiet_days": 30.0}],
        "model": "deepseek/deepseek-v4-flash",
    }
    build_all(None, tmp_path, now=NOW)
    report = read(tmp_path, "daily-report.json")["report"]
    assert report["date"] == NOW.date().isoformat()
    assert report["summary"] == "A quiet day apart from one breach."
    [item] = report["items"]
    assert item["event_id"] == "evt-2026-000001" and item["why"] == "It matters for defenders."
    assert item["title"] and item["severity"] in {"critical", "high", "medium", "low", "unknown"}
    assert report["gaps"] == [{"topic": "ddos", "kind": "category", "quiet_days": 30.0}]
    assert report["model"] == "deepseek/deepseek-v4-flash"
