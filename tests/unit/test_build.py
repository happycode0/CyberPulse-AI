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
from worker.db.sources import RegistryRow
from worker.db.trends import TrendInputs
from worker.models import (
    Event,
    HealthStatus,
    Lane,
    LifecycleState,
    RunSummary,
    SourceHealth,
)
from worker.pipeline.trends import Report, Story
from worker.publish.build import build_all
from worker.publish.validate import ValidationFailure

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
        "merged": {},
        "trends": TrendInputs([], [], {}, NOW - timedelta(days=3)),
        "crew": {
            "librarian": Activity(4, None, NOW - timedelta(hours=2)),
            "prowl": Activity(90, 4000, NOW - timedelta(minutes=3)),
            "seraph": Activity(800, 45, NOW - build.CREW_FRESH["seraph"] - timedelta(minutes=10)),
            "rogue": Activity(0, None, None),
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
        "critical_high": 1, "au_stories": 0,
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
    db["crew"]["librarian"] = Activity(4, None, NOW - timedelta(hours=2), failing=True)
    build_all(None, tmp_path, now=NOW)
    crew = read(tmp_path, "crew.json")
    agents = {a["callsign"]: a for a in crew["agents"]}
    assert set(agents) == {"LIBRARIAN", "PROWL", "SERAPH", "ROGUE", "LINK"}
    assert crew["month"] == "2026-09"
    # The newest ground-truth pass raised: degraded, whatever the last good one says.
    assert agents["LIBRARIAN"]["status"] == "degraded"
    assert agents["PROWL"] | {"last_active_at": "-"} == {
        "callsign": "PROWL", "status": "active", "tasks_completed": 90,
        "items_processed": 4000, "cost_usd": 0.0, "last_active_at": "-",
    }
    assert agents["SERAPH"]["status"] == "idle"  # no source checked in two FAST intervals
    assert agents["ROGUE"] == {
        "callsign": "ROGUE", "status": "idle", "tasks_completed": 0, "items_processed": None,
        "cost_usd": 0.0, "last_active_at": None,
    }
    assert agents["LINK"]["last_active_at"] == "2026-09-30T12:00:00Z"
    assert crew["pipeline_ai"] == {"calls": 12, "cost_usd": 0.0412}


def test_prowl_and_seraph_stay_active_between_fast_runs(tmp_path, db):
    # An enrichment pass publishes between runs, and a restart can cost one run.
    since = NOW - build.FAST_INTERVAL - timedelta(minutes=5)
    db["crew"]["prowl"] = Activity(90, 4000, since)
    db["crew"]["seraph"] = Activity(800, 45, since)
    build_all(None, tmp_path, now=NOW)
    agents = {a["callsign"]: a["status"] for a in read(tmp_path, "crew.json")["agents"]}
    assert agents["PROWL"] == agents["SERAPH"] == "active"


def test_the_site_roster_has_every_agent_crew_json_names():
    hud = (Path(__file__).resolve().parents[2] / "site/assets/hud.js").read_text()
    for callsign in ("LIBRARIAN", "PROWL", "SERAPH", "ROGUE", "LINK"):
        assert f"callsign: '{callsign}'" in hud
