import asyncio
import functools
import re
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from sqlalchemy import text

from worker.db.migrate import run_migrations
from worker.db.runs import load_run
from worker.models import Lane
from worker.pipeline.run import run_lane as _run_lane
from worker.settings import get_settings
from worker.version import SCORING_VERSION

FEEDS = Path(__file__).parent.parent / "fixtures" / "feeds"
CONFIG = Path(__file__).resolve().parents[2] / "config"
NOW = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)

URL_A = "https://feeds.example.org/a.xml"
URL_B = "https://feeds.example.org/b.xml"


def fixture_bytes(name: str) -> bytes:
    return (FEEDS / name).read_bytes()


def source_yaml(sid, url, *, lane="fast", type="rss", parser="rss", category="advisory",
                region="au", frequency="daily"):
    return f"""  - id: {sid}
    name: Source {sid}
    type: {type}
    region: {region}
    category: {category}
    class: feed
    priority: 1
    lane: {lane}
    enabled: true
    url: {url}
    parser: {parser}
    expected_frequency: {frequency}
    notes: null
"""


def write_registry(path: Path, sources: list[str]) -> Path:
    path.write_text("sources:\n" + "\n".join(sources))
    return path


@pytest.fixture(autouse=True)
def clean_db(pg_engine, tmp_path, monkeypatch):
    """Each test starts from an empty pipeline database and a private raw cache."""
    run_migrations(pg_engine)
    with pg_engine.begin() as conn:
        for table in ("events", "source_health", "source_fetch_state", "runs", "source_registry"):
            conn.execute(text(f"delete from {table}"))
    monkeypatch.setenv("RAW_CACHE_DIR", str(tmp_path / "raw"))
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def registry(tmp_path):
    def make(*sources: str) -> Path:
        return write_registry(tmp_path / "sources.yaml", list(sources))

    return make


@pytest.fixture
def two_feeds_registry(registry):
    return registry(source_yaml("feed_a", URL_A, category="advisory", region="us"),
                    source_yaml("feed_b", URL_B, category="news", region="au"))


@pytest.fixture
def run_lane(pg_engine, two_feeds_registry):
    return functools.partial(
        _run_lane, engine=pg_engine, registry_path=two_feeds_registry,
        scoring_path=CONFIG / "scoring.yaml", now=NOW,
    )


def mock_two_feeds(respx_mock):
    respx_mock.get(URL_A).respond(200, content=fixture_bytes("cisa_advisories.xml"))
    respx_mock.get(URL_B).respond(200, content=fixture_bytes("acsc_alerts.xml"))


def scalar(pg_engine, sql, **params):
    with pg_engine.connect() as conn:
        return conn.execute(text(sql), params).scalar_one()


async def test_run_lane_end_to_end_creates_events(pg_engine, respx_mock, run_lane):
    mock_two_feeds(respx_mock)
    s = await run_lane(Lane.FAST, once=True)
    assert s.new_events > 0 and s.sources_ok == 2


async def test_second_identical_run_creates_no_new_events(pg_engine, respx_mock, run_lane):
    mock_two_feeds(respx_mock)
    first = await run_lane(Lane.FAST, once=True)
    second = await run_lane(Lane.FAST, once=True)
    assert second.new_events == 0 and second.updated_events == 0
    assert second.duplicates == first.items_fetched
    assert scalar(pg_engine, "select count(*) from events") == first.new_events


async def test_one_failing_source_does_not_stop_the_others(pg_engine, respx_mock, run_lane):
    respx_mock.get(URL_A).respond(500)
    respx_mock.get(URL_B).respond(200, content=fixture_bytes("acsc_alerts.xml"))
    s = await run_lane(Lane.FAST, once=True)
    assert s.sources_failed == 1 and s.sources_ok == 1 and s.items_fetched > 0
    assert any(e.startswith("feed_a:") for e in s.errors)


async def test_run_summary_is_persisted(pg_engine, respx_mock, run_lane):
    s = await run_lane(Lane.FAST, once=True)
    loaded = load_run(pg_engine, s.run_id)
    assert loaded.lane == "fast"
    assert loaded == s and loaded.finished_at is not None


async def test_sources_are_fetched_concurrently(pg_engine, respx_mock, registry):
    """20 sources each delayed 100ms must finish well under serial time (2s)."""
    urls = [f"https://slow.example.org/{i}.xml" for i in range(20)]
    reg = registry(*(source_yaml(f"slow_{i}", u) for i, u in enumerate(urls)))
    body = fixture_bytes("securitybrief.xml")

    async def slow(request):
        await asyncio.sleep(0.1)
        return httpx.Response(200, content=body)

    for u in urls:
        respx_mock.get(u).mock(side_effect=slow)
    t0 = time.monotonic()
    s = await _run_lane(Lane.FAST, once=True, engine=pg_engine, registry_path=reg,
                        scoring_path=CONFIG / "scoring.yaml", now=NOW)
    assert time.monotonic() - t0 < 1.0
    assert s.sources_ok == 20


async def test_conditional_request_skips_unchanged_source(pg_engine, respx_mock, run_lane):
    def answer(request):
        if request.headers.get("if-none-match") == '"v1"':
            return httpx.Response(304)
        return httpx.Response(200, content=fixture_bytes("acsc_alerts.xml"),
                              headers={"ETag": '"v1"'})

    respx_mock.get(URL_A).mock(side_effect=answer)
    respx_mock.get(URL_B).mock(side_effect=answer)
    first = await run_lane(Lane.FAST, once=True)
    assert first.items_fetched > 0
    s = await run_lane(Lane.FAST, once=True)
    assert s.items_fetched == 0 and s.sources_failed == 0 and s.sources_ok == 2


# --- data flow: a RawItem really becomes a stored, scored Event -----------------------------


async def test_stored_event_is_dated_by_publication_and_scored(pg_engine, respx_mock, run_lane):
    mock_two_feeds(respx_mock)
    await run_lane(Lane.FAST, once=True)
    with pg_engine.connect() as conn:
        row = conn.execute(text(
            "select e.first_seen, e.last_seen, e.last_material_update, e.prominence, "
            "e.urgency, e.confidence, e.novelty, e.scoring_version, e.pending_enrichment "
            "from events e join event_sources es using (event_id) "
            "where es.url = 'https://www.cyber.gov.au/alerts/alert-1'"
        )).one()
    assert row.first_seen == datetime(2026, 9, 27, 12, 0, tzinfo=UTC)   # published, not fetched
    assert row.last_material_update == row.first_seen
    assert row.last_seen >= row.first_seen
    assert None not in (row.prominence, row.urgency, row.confidence, row.novelty)
    assert row.scoring_version == SCORING_VERSION and row.pending_enrichment is True


async def test_every_stored_event_loads_as_a_valid_event(pg_engine, respx_mock, run_lane):
    from worker.db.events import load_live_events

    mock_two_feeds(respx_mock)
    s = await run_lane(Lane.FAST, once=True)
    with pg_engine.connect() as conn:
        events = load_live_events(conn, min_prominence=-1.0, limit=1000)
    assert len(events) == s.new_events
    by_source = {e.sources[0].source_id: e for e in events}
    assert by_source["feed_a"].sources[0].evidence_class == "AUTHORITATIVE"
    assert by_source["feed_b"].sources[0].evidence_class == "NEWS"
    assert by_source["feed_b"].au.directly_reported_in_au is True
    assert by_source["feed_a"].au.directly_reported_in_au is False
    # The AU engine ran in the rescore: an Australian outlet's report has a floor and says why.
    assert by_source["feed_b"].au.relevance >= 0.3
    assert "reported by an Australian source" in by_source["feed_b"].au.reasons
    assert all(any(t.type == "NEW_FACT" for t in e.timeline) for e in events)


async def test_second_source_reporting_same_story_merges_and_corroborates(
    pg_engine, respx_mock, run_lane
):
    feed = fixture_bytes("acsc_alerts.xml").decode()
    other = re.sub(r"https://www\.cyber\.gov\.au/alerts/", "https://mirror.example.org/", feed)
    respx_mock.get(URL_A).respond(200, content=feed.encode())
    respx_mock.get(URL_B).respond(200, content=other.encode())
    s = await run_lane(Lane.FAST, once=True)
    assert s.new_events == 7 and s.updated_events == 7        # B's items merge into A's events
    with pg_engine.connect() as conn:
        n_sources = conn.execute(text(
            "select count(*) filter (where n = 2) from ("
            "select count(*) n from event_sources group by event_id) x")).scalar_one()
        confirmed = conn.execute(text(
            "select count(*) from events where last_independent_confirmation is not null"
        )).scalar_one()
    assert n_sources == 7 and confirmed == 7


async def test_source_health_is_recorded_with_stale_detection(pg_engine, respx_mock, registry):
    reg = registry(source_yaml("hourly_feed", URL_A, frequency="hourly"))
    respx_mock.get(URL_A).respond(200, content=fixture_bytes("acsc_alerts.xml"))
    s = await _run_lane(Lane.FAST, once=True, engine=pg_engine, registry_path=reg,
                        scoring_path=CONFIG / "scoring.yaml", now=NOW)
    # newest item is 3 days old and an hourly feed is stale after 2 days, despite HTTP 200
    assert s.sources_stale == 1 and s.sources_ok == 0
    assert scalar(pg_engine, "select status from source_health") == "stale"
    assert s.new_events == 7        # stale sources still contribute their items


async def test_five_consecutive_failures_degrade_the_source(pg_engine, respx_mock, run_lane):
    respx_mock.get(URL_A).respond(500)
    respx_mock.get(URL_B).respond(200, content=fixture_bytes("acsc_alerts.xml"))
    for _ in range(4):
        await run_lane(Lane.FAST, once=True)
    assert scalar(pg_engine, "select lifecycle_state from source_registry where id='feed_a'") is None
    await run_lane(Lane.FAST, once=True)
    assert scalar(pg_engine, "select lifecycle_state from source_registry where id='feed_a'") == "degraded"
    assert scalar(pg_engine, "select lifecycle_state from source_registry where id='feed_b'") is None
    # the registry re-sync on every run must not wipe the tracked state
    await run_lane(Lane.FAST, once=True)
    assert scalar(pg_engine, "select lifecycle_state from source_registry where id='feed_a'") == "degraded"


async def test_a_304_after_a_transient_error_recovers_the_source(
    pg_engine, respx_mock, two_feeds_registry
):
    """200 (ETag saved), one 500, then 304 forever: the first 304 must clear the error, and
    six more must never degrade a source the server keeps confirming is up."""
    state = {"fail": False}

    def answer(request):
        if state["fail"]:
            return httpx.Response(500)
        if request.headers.get("if-none-match") == '"v1"':
            return httpx.Response(304)
        return httpx.Response(200, content=fixture_bytes("acsc_alerts.xml"),
                              headers={"ETag": '"v1"'})

    respx_mock.get(URL_A).mock(side_effect=answer)
    respx_mock.get(URL_B).respond(200, content=fixture_bytes("acsc_alerts.xml"))
    clock = {"n": 0}

    async def run():      # a real clock moves between runs; identical timestamps would hide ordering bugs
        clock["n"] += 1
        return await _run_lane(
            Lane.FAST, once=True, engine=pg_engine, registry_path=two_feeds_registry,
            scoring_path=CONFIG / "scoring.yaml", now=NOW + timedelta(minutes=15 * clock["n"]),
        )

    def latest():
        return scalar(pg_engine, "select status from source_health where source_id='feed_a' "
                                 "order by id desc limit 1")

    await run()
    assert latest() == "ok"
    state["fail"] = True
    await run()
    assert latest() == "error"
    state["fail"] = False
    for _ in range(6):
        await run()
        assert latest() == "ok"
    assert scalar(pg_engine, "select lifecycle_state from source_registry where id='feed_a'") is None


async def test_a_bad_body_from_one_source_is_a_failure_not_a_crash(pg_engine, respx_mock, run_lane):
    respx_mock.get(URL_A).respond(200, content=b"<html>not a feed</html>")
    respx_mock.get(URL_B).respond(200, content=fixture_bytes("acsc_alerts.xml"))
    s = await run_lane(Lane.FAST, once=True)
    assert s.sources_failed == 1 and s.sources_ok == 1


async def test_unsupported_source_type_is_a_failure_not_a_crash(pg_engine, respx_mock, registry):
    reg = registry(source_yaml("odd", URL_A, type="web_page"),
                   source_yaml("good", URL_B))
    respx_mock.get(URL_A).respond(200, content=b"<html/>")
    respx_mock.get(URL_B).respond(200, content=fixture_bytes("acsc_alerts.xml"))
    s = await _run_lane(Lane.FAST, once=True, engine=pg_engine, registry_path=reg,
                        scoring_path=CONFIG / "scoring.yaml", now=NOW)
    assert s.sources_failed == 1 and s.sources_ok == 1
    assert any("unsupported source type" in e for e in s.errors)


async def test_failure_while_storing_rolls_back_that_source_only(
    pg_engine, respx_mock, run_lane, monkeypatch
):
    import worker.pipeline.run as run_mod

    real = run_mod.insert_new_event

    def flaky(conn, event, item, **kw):
        if event.sources[0].source_id == "feed_a" and item.title.startswith("Alert 3"):
            raise RuntimeError("boom")
        return real(conn, event, item, **kw)

    monkeypatch.setattr(run_mod, "insert_new_event", flaky)
    respx_mock.get(URL_A).respond(200, content=fixture_bytes("acsc_alerts.xml"))
    respx_mock.get(URL_B).respond(200, content=fixture_bytes("cisa_advisories.xml"))
    s = await run_lane(Lane.FAST, once=True)
    assert s.sources_failed == 1 and s.sources_ok == 1
    assert scalar(pg_engine, "select count(*) from event_sources where source_id='feed_a'") == 0
    assert scalar(pg_engine, "select count(*) from event_sources where source_id='feed_b'") == 3
    assert scalar(pg_engine, "select count(*) from source_fetch_state where source_id='feed_a'") == 0
    assert scalar(pg_engine, "select status from source_health where source_id='feed_a'") == "error"


async def test_kev_entries_become_distinct_events_with_their_cves(pg_engine, respx_mock, registry):
    reg = registry(source_yaml("kev", URL_A, type="json_api", parser="json_kev", region="us"))
    kev = (Path(__file__).parent.parent / "fixtures" / "kev_sample.json").read_bytes()
    respx_mock.get(URL_A).respond(200, content=kev)
    s = await _run_lane(Lane.FAST, once=True, engine=pg_engine, registry_path=reg,
                        scoring_path=CONFIG / "scoring.yaml", now=NOW)
    assert s.new_events == 3 and s.duplicates == 0
    assert scalar(pg_engine, "select count(distinct cve_id) from event_cves") == 3
    again = await _run_lane(Lane.FAST, once=True, engine=pg_engine, registry_path=reg,
                            scoring_path=CONFIG / "scoring.yaml", now=NOW)
    assert again.new_events == 0 and again.duplicates == 3


async def test_repeat_sightings_advance_last_seen_only(pg_engine, respx_mock, run_lane):
    from datetime import timedelta

    mock_two_feeds(respx_mock)
    await run_lane(Lane.FAST, once=True)
    before = scalar(pg_engine, "select min(last_seen) from events")
    later = NOW + timedelta(hours=6)
    await _run_lane(Lane.FAST, once=True, engine=pg_engine, now=later,
                    registry_path=run_lane.keywords["registry_path"],
                    scoring_path=CONFIG / "scoring.yaml")
    assert scalar(pg_engine, "select min(last_seen) from events") == later > before
    assert scalar(pg_engine, "select count(*) from events where last_material_update is null") == 0
    assert scalar(pg_engine, "select count(*) from events where last_material_update < first_seen") == 0


async def test_prominence_decays_between_runs_without_new_items(pg_engine, respx_mock, run_lane):
    from datetime import timedelta

    mock_two_feeds(respx_mock)
    await run_lane(Lane.FAST, once=True)
    before = scalar(pg_engine, "select max(prominence) from events")
    later = functools.partial(
        _run_lane, engine=pg_engine, scoring_path=CONFIG / "scoring.yaml",
        registry_path=run_lane.keywords["registry_path"], now=NOW + timedelta(days=2),
    )
    await later(Lane.FAST, once=True)
    assert scalar(pg_engine, "select max(prominence) from events") < before


async def test_published_output_builds_from_a_real_run(pg_engine, respx_mock, run_lane, tmp_path):
    """The pipeline's output must pass the publisher's schema and secret gates."""
    from worker.publish.build import build_all

    mock_two_feeds(respx_mock)
    await run_lane(Lane.FAST, once=True)
    with pg_engine.connect() as conn:
        written = build_all(conn, tmp_path / "out", now=NOW)
    assert {p.name for p in written} >= {"live.json", "source-health.json", "system-status.json"}


async def test_full_committed_registry_and_scoring_config_load(pg_engine, respx_mock):
    """The real config files work with the orchestrator: every enabled fast source is
    fetched (all answering 404 here) and recorded, none crash the run."""
    respx_mock.route().respond(404)
    s = await _run_lane(Lane.FAST, once=True, engine=pg_engine)     # wall clock, real config
    assert s.sources_ok == 0 and s.sources_failed > 5 and s.new_events == 0
