"""Source discovery against a real Postgres: migration 013, where each find stands at SERAPH's
gate, and a found site's way from a search hit to a collected source."""

import functools
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from worker.db import discovery as db
from worker.db.jobs import JobRun, record_job
from worker.db.migrate import run_migrations
from worker.db.sources import record_health, upsert_registry
from worker.discovery import guard
from worker.discovery.gate import (
    DISCOVERED_CLASS,
    DiscoveryConfig,
    Probe,
    check_proposal,
    discovered_source,
)
from worker.discovery.run import run_gate
from worker.models import HealthStatus, Lane, LifecycleState, SourceConfig, SourceHealth
from worker.pipeline import run as pipeline_run
from worker.settings import get_settings

NOW = datetime(2026, 10, 3, 8, 0, tzinfo=UTC)
CONFIG = DiscoveryConfig.load()
GATE = CONFIG.gate
TABLES = ("source_candidates", "discovery_searches", "events", "source_health",
          "source_fetch_state", "runs", "job_runs", "source_registry")
REASON = "Writes up Australian ransomware incidents that no registered source covers."
HOST = "news.example.org"


def clear(engine) -> None:
    with engine.begin() as conn:
        for table in TABLES:
            conn.execute(text(f"delete from {table}"))


@pytest.fixture
def engine(pg_engine, tmp_path, monkeypatch):
    run_migrations(pg_engine)
    clear(pg_engine)
    monkeypatch.setenv("RAW_CACHE_DIR", str(tmp_path / "raw"))
    get_settings.cache_clear()
    yield pg_engine
    clear(pg_engine)
    get_settings.cache_clear()


def call(engine, fn, /, **kwargs):
    with engine.begin() as conn:
        return fn(conn, **kwargs)


def candidate(engine, host=HOST) -> db.Candidate:
    with engine.connect() as conn:
        [row] = db._candidates(conn, "host = :host", {"host": host})
    return row


def register(engine, host: str) -> None:
    source = SourceConfig(
        id=host.replace(".", "_"), name=host, type="rss", region="us", category="advisory",
        source_class="feed", priority=1, lane=Lane.FAST, enabled=True,
        url=f"https://{host}/feed", parser="rss", expected_frequency="daily",
    )
    call(engine, upsert_registry, sources=[source])


def proposal(url=f"https://{HOST}/feed/", **body):
    return check_proposal({"url": url, "reason": REASON, **body})


def propose(engine, url=f"https://{HOST}/feed/", *, max_open=20, now=NOW, **body):
    return call(engine, db.add_proposal, proposal=proposal(url, **body), max_open=max_open,
                now=now)


def healthy() -> Probe:
    return Probe(items=10, recent=6, on_beat=4, already_collected=0, newest_age_days=0.5)


def sick() -> Probe:
    return Probe(items=2, recent=0, on_beat=0, already_collected=0, newest_age_days=40.0,
                 failures=["0 items dated in the last 14 days; 3 needed"])


# --- The schema ----------------------------------------------------------------------------------


def test_the_job_runs_check_takes_the_two_discovery_jobs(engine):
    for job in ("discovery", "source-gate"):
        record_job(engine, JobRun(job, NOW, NOW, completed=True))
    with engine.connect() as conn:
        assert conn.execute(text("select count(*) from job_runs")).scalar_one() == 2


@pytest.mark.parametrize(
    "columns, values",
    [
        # An active candidate names its source ...
        ("feed_url, state", "'https://a.example.org/feed', 'active'"),
        # ... and anything past `discovered` has a feed.
        ("state", "'candidate'"),
        ("found_by", "'someone'"),
    ],
)
def test_a_candidate_that_breaks_the_rules_is_refused(engine, columns, values):
    found_by = "" if "found_by" in columns else "found_by, "
    by = "" if "found_by" in columns else "'search', "
    with pytest.raises(IntegrityError), engine.begin() as conn:
        conn.execute(text(
            f"insert into source_candidates (host, {found_by}{columns}) "
            f"values ('a.example.org', {by}{values})"
        ))


# --- Searches and finds --------------------------------------------------------------------------


def test_credits_count_what_tavily_said_or_one_for_a_search_it_answered(engine):
    for credits, error, at in [(2, None, NOW), (None, None, NOW), (None, "HTTP 502", NOW),
                               (5, None, NOW - timedelta(days=2))]:
        call(engine, db.record_search, query="q", results=0, credits=credits, error=error, now=at)
    assert call(engine, db.credits_used, since=NOW - timedelta(days=1)) == 3
    assert call(engine, db.credits_used, since=NOW - timedelta(days=3)) == 8


def test_a_host_found_again_gathers_evidence_up_to_the_cap(engine):
    found = [call(engine, db.add_found, host=HOST, evidence={"url": f"https://{HOST}/{i}"},
                  now=NOW) for i in range(8)]
    assert found == [True] + [False] * 7
    row = candidate(engine)
    assert (row.state, row.found_by, row.feed_url) == ("discovered", "search", None)
    assert len(row.evidence) == db.EVIDENCE_KEPT


def test_evidence_stops_once_a_host_has_a_feed(engine):
    call(engine, db.add_found, host=HOST, evidence={"url": f"https://{HOST}/a"}, now=NOW)
    propose(engine)
    before = len(candidate(engine).evidence)
    call(engine, db.add_found, host=HOST, evidence={"url": f"https://{HOST}/b"}, now=NOW)
    assert len(candidate(engine).evidence) == before


def test_registered_hosts_are_read_without_www(engine):
    register(engine, "www.cisa.gov")
    assert call(engine, db.registered_hosts) == {"cisa.gov"}


# --- TACHIKOMA's proposals -----------------------------------------------------------------------


def test_a_feed_proposal_becomes_a_candidate(engine):
    result, row = propose(engine, name="News")
    assert result == "created"
    assert (row.state, row.found_by, row.feed_url, row.name) == (
        "candidate", "tachikoma", f"https://{HOST}/feed/", "News"
    )
    assert row.proposed_at == NOW


def test_a_home_page_proposal_waits_for_its_feed(engine):
    result, row = propose(engine, f"https://{HOST}/")
    assert result == "created" and (row.state, row.feed_url) == ("discovered", None)


def test_a_searched_host_takes_tachikomas_feed(engine):
    call(engine, db.add_found, host=HOST, evidence={"url": f"https://{HOST}/a"}, now=NOW)
    result, row = propose(engine, examples=[f"https://{HOST}/2026/story"])
    assert result == "updated" and row.state == "candidate" and row.reason == REASON
    assert [e["url"] for e in row.evidence] == [f"https://{HOST}/a", f"https://{HOST}/2026/story"]


def test_a_known_or_registered_site_is_not_proposed_twice(engine):
    propose(engine)
    assert propose(engine)[0] == "exists"
    register(engine, "cisa.gov")
    assert propose(engine, "https://www.cisa.gov/news") == ("exists", None)
    assert propose(engine, "https://blog.cisa.gov/") == ("exists", None)


def test_proposals_stop_when_the_gate_is_full(engine):
    propose(engine, max_open=1)
    assert propose(engine, "https://other.example.org/rss", max_open=1) == ("full", None)
    # A home page is not a candidate yet, so it still gets in.
    assert propose(engine, "https://home.example.org/", max_open=1)[0] == "created"


def test_proposals_are_capped_per_day(engine):
    for i in range(db.PROPOSALS_PER_DAY):
        assert propose(engine, f"https://s{i}.example.org/")[0] == "created"
    assert propose(engine, "https://late.example.org/") == ("full", None)
    tomorrow = NOW + timedelta(days=1, minutes=1)
    assert propose(engine, "https://late.example.org/", now=tomorrow)[0] == "created"


# --- The gate ------------------------------------------------------------------------------------


def test_a_home_page_without_a_feed_is_tried_weekly_then_rejected(engine):
    call(engine, db.add_found, host=HOST, evidence={}, now=NOW)
    tries = GATE.failures_to_reject
    at = NOW
    for expected in ["discovered"] * (tries - 1) + ["rejected"]:
        [due] = call(engine, db.homes_due, limit=5, now=at, gate=GATE)
        assert call(engine, db.record_home, candidate_id=due.id, feed_url=None, name=None,
                    error="no feed", gate=GATE, now=at) == expected
        assert call(engine, db.homes_due, limit=5, now=at + timedelta(days=1), gate=GATE) == []
        at += db.HOME_RETRY
    assert call(engine, db.homes_due, limit=5, now=at, gate=GATE) == []


def test_tachikomas_homes_are_read_first(engine):
    call(engine, db.add_found, host="a.example.org", evidence={}, now=NOW)
    propose(engine, "https://b.example.org/")
    homes = call(engine, db.homes_due, limit=5, now=NOW, gate=GATE)
    assert [h.host for h in homes] == ["b.example.org", "a.example.org"]


def test_a_home_page_with_a_feed_makes_a_candidate(engine):
    call(engine, db.add_found, host=HOST, evidence={}, now=NOW)
    [due] = call(engine, db.homes_due, limit=5, now=NOW, gate=GATE)
    assert call(engine, db.record_home, candidate_id=due.id, feed_url=f"https://{HOST}/rss",
                name="News", error=None, gate=GATE, now=NOW) == "candidate"
    row = candidate(engine)
    assert (row.feed_url, row.name, row.passes, row.failures) == (f"https://{HOST}/rss", "News",
                                                                  0, 0)


def test_probes_in_a_row_move_a_candidate_through_the_gate(engine):
    assert GATE.failures_to_reject == 3, "the sequence below assumes three"
    _, row = propose(engine)
    record = functools.partial(call, engine, db.record_probe, candidate_id=row.id, gate=GATE,
                               now=NOW)
    assert record(probe=healthy(), error=None) == ("testing", 1)
    assert record(probe=healthy(), error=None) == ("testing", 2)
    # One bad probe breaks the run ...
    assert record(probe=sick(), error=None) == ("candidate", 0)
    assert candidate(engine).last_error.startswith("0 items dated")
    assert record(probe=None, error="refused: no") == ("candidate", 0)
    # ... and three in a row reject it for good.
    assert record(probe=None, error="HTTP 500") == ("rejected", 0)
    assert record(probe=healthy(), error=None) == ("gone", 0)
    assert candidate(engine).last_result is None


def test_probes_wait_for_the_gap(engine):
    _, row = propose(engine)
    assert [c.id for c in call(engine, db.probes_due, limit=5, now=NOW)] == [row.id]
    call(engine, db.record_probe, candidate_id=row.id, probe=healthy(), error=None, gate=GATE,
         now=NOW)
    assert call(engine, db.probes_due, limit=5, now=NOW + timedelta(hours=2)) == []
    assert len(call(engine, db.probes_due, limit=5, now=NOW + db.PROBE_GAP)) == 1


def activate(engine, host=HOST) -> str:
    _, row = propose(engine, f"https://{host}/feed/")
    source = discovered_source(host=host, feed_url=row.feed_url, name="News",
                               found_by="tachikoma", now=NOW)
    assert call(engine, db.activate, candidate=row, source=source, now=NOW)
    return source.id


def test_activation_registers_the_source_for_collection(engine):
    source_id = activate(engine)
    row = candidate(engine)
    assert (row.state, row.source_id) == ("active", source_id)
    [source] = call(engine, db.load_discovered_sources)
    assert (source.id, source.source_class, source.lane) == (
        source_id, DISCOVERED_CLASS, Lane.NORMAL
    )
    assert source.lifecycle_state is LifecycleState.ACTIVE
    assert call(engine, db.active_discovered) == 1
    [notice] = call(engine, db.load_activations, since=NOW - timedelta(hours=1))
    assert (notice.source_id, notice.host, notice.name) == (source_id, HOST, "News")
    assert call(engine, db.load_activations, since=NOW + timedelta(hours=1)) == []


def test_activation_never_takes_an_id_already_registered(engine):
    activate(engine)
    _, other = propose(engine, "https://other.example.org/feed")
    same_id = discovered_source(host=HOST, feed_url=other.feed_url, name=None, found_by="search",
                                now=NOW)
    assert call(engine, db.activate, candidate=other, source=same_id, now=NOW) is False
    assert candidate(engine, "other.example.org").state == "candidate"


def health(engine, source_id, status, at):
    call(engine, record_health,
         health=SourceHealth(source_id=source_id, checked_at=at, status=status))


def test_a_source_silent_for_three_weeks_is_retired(engine):
    source_id = activate(engine)
    health(engine, source_id, HealthStatus.OK, NOW)
    health(engine, source_id, HealthStatus.ERROR, NOW + timedelta(days=20))
    assert call(engine, db.retire_silent, now=NOW + timedelta(days=20)) == []
    later = NOW + db.RETIRE_AFTER + timedelta(hours=1)
    assert call(engine, db.retire_silent, now=later) == [source_id]
    assert call(engine, db.load_discovered_sources) == []
    row = candidate(engine)
    assert (row.state, row.source_id) == ("retired", source_id)
    # Retired is final: the host is not proposed again.
    assert propose(engine, now=later)[0] == "exists"


def test_a_healthy_fetch_in_the_three_weeks_keeps_a_source(engine):
    source_id = activate(engine)
    health(engine, source_id, HealthStatus.ERROR, NOW)
    health(engine, source_id, HealthStatus.OK, NOW + timedelta(days=10))
    assert call(engine, db.retire_silent, now=NOW + timedelta(days=25)) == []


def test_a_new_source_is_not_retired_before_it_has_had_its_three_weeks(engine):
    source_id = activate(engine)
    health(engine, source_id, HealthStatus.ERROR, NOW)
    assert call(engine, db.retire_silent, now=NOW + timedelta(days=20)) == []


def test_finds_never_given_a_feed_are_forgotten(engine):
    call(engine, db.add_found, host="old.example.org", evidence={},
         now=NOW - timedelta(days=GATE.forget_after_days + 1))
    call(engine, db.add_found, host="new.example.org", evidence={}, now=NOW)
    assert call(engine, db.forget_unfed, now=NOW, gate=GATE) == 1
    assert candidate(engine, "new.example.org").state == "discovered"


def test_the_overview_has_every_part(engine):
    activate(engine, "a.example.org")
    propose(engine)
    call(engine, db.add_found, host="c.example.org", evidence={}, now=NOW)
    call(engine, db.record_search, query="q", results=3, credits=1, error=None, now=NOW)
    overview = call(engine, db.load_overview, now=NOW)
    assert overview["states"] == {"active": 1, "candidate": 1, "discovered": 1}
    assert [c.host for c in overview["open"]] == [HOST]
    assert [c.host for c in overview["waiting_for_a_feed"]] == ["c.example.org"]
    assert [c.host for c in overview["settled_last_14_days"]] == ["a.example.org"]
    assert overview["credits"] == {"last_24h": 1, "this_month": 1}
    assert overview["active_discovered_sources"] == 1


# --- From a search hit to a collected source -----------------------------------------------------

TITLES = (
    "Ransomware gang claims an attack on a Victorian council",
    "Critical vulnerability found in a popular VPN appliance",
    "Hospital data breach exposes patient records",
    "Phishing campaign targets Australian tax payers",
    "Botnet hijacks home routers across Queensland",
)


def feed(now: datetime) -> bytes:
    items = "".join(
        f"<item><title>{title}</title>"
        f"<link>https://{HOST}/2026/story-{i}</link>"
        f"<guid>https://{HOST}/2026/story-{i}</guid>"
        f"<pubDate>{format_datetime(now - timedelta(days=i + 1))}</pubDate>"
        f"<description>{title}.</description></item>"
        for i, title in enumerate(TITLES)
    )
    return (f'<?xml version="1.0"?><rss version="2.0"><channel><title>Example News</title>'
            f"<link>https://{HOST}/</link><description>x</description>{items}</channel></rss>"
            ).encode()


def site(now: datetime):
    """The found site's home page and feed; no other host answers."""
    asked = []

    def handler(request):
        asked.append(str(request.url))
        if request.url.host != HOST:
            raise httpx.ConnectError("no such site")
        if request.url.path == "/":
            return httpx.Response(200, html=(
                '<html><head><title>Example</title><link rel="alternate" '
                'type="application/rss+xml" href="/feed.xml"></head></html>'
            ))
        if request.url.path == "/feed.xml":
            return httpx.Response(200, content=feed(now),
                                  headers={"Content-Type": "application/rss+xml"})
        return httpx.Response(404)

    return httpx.MockTransport(handler), asked


async def public(host: str) -> list[str]:
    return ["93.184.215.14"]


async def test_a_found_site_is_probed_for_a_day_then_collected(engine, tmp_path, monkeypatch,
                                                               respx_mock):
    call(engine, db.add_found, host=HOST, evidence={"url": f"https://{HOST}/a"}, now=NOW)
    transport, asked = site(NOW)
    gate = functools.partial(run_gate, engine=engine, config=CONFIG, transport=transport,
                             resolve=public)

    first = await gate(now=NOW)
    assert (first.homes, first.feeds_found, first.probes) == (1, 1, 0)
    row = candidate(engine)
    assert (row.state, row.feed_url, row.name) == (
        "candidate", f"https://{HOST}/feed.xml", "Example News"
    )

    at = NOW
    for n in range(1, GATE.probes_to_activate + 1):
        at += timedelta(hours=4)
        summary = await gate(now=at)
        assert (summary.probes, summary.healthy) == (1, 1), summary
        assert bool(summary.activated) is (n == GATE.probes_to_activate)
    assert summary.activated == ["found_news_example_org"]
    row = candidate(engine)
    assert row.state == "active" and row.last_result["on_beat"] == len(TITLES)

    # A probe counts the items and keeps none.
    with engine.connect() as conn:
        assert conn.execute(text("select count(*) from events")).scalar_one() == 0

    # The next NORMAL run collects it, through the guard.
    real = guard.fetch_guarded

    async def guarded(client, url, **kwargs):
        async with httpx.AsyncClient(transport=transport) as mocked:
            return await real(mocked, url, resolve=public, **kwargs)

    monkeypatch.setattr(pipeline_run, "fetch_guarded", guarded)
    registry = tmp_path / "sources.yaml"
    registry.write_text(
        "sources:\n  - id: fast_one\n    name: Fast\n    type: rss\n    region: au\n"
        "    category: advisory\n    class: feed\n    priority: 1\n    lane: fast\n"
        "    enabled: true\n    url: https://feeds.example.org/fast.xml\n    parser: rss\n"
        "    expected_frequency: daily\n    notes: null\n"
    )
    run = await pipeline_run.run_lane(Lane.NORMAL, once=True, engine=engine,
                                      registry_path=registry, now=at)
    assert (run.sources_ok, run.sources_failed) == (1, 0), run.errors
    assert run.new_events > 0
    with engine.connect() as conn:
        classes = conn.execute(text(
            "select distinct source_id, evidence_class from event_sources"
        )).all()
    assert classes == [("found_news_example_org", "COMMUNITY")]
    assert all(u.startswith(f"https://{HOST}/") for u in asked)


async def test_a_site_whose_name_points_inside_is_never_fetched(engine):
    call(engine, db.add_found, host=HOST, evidence={}, now=NOW)
    transport, asked = site(NOW)

    async def inside(host: str) -> list[str]:
        return ["192.168.128.39"]

    summary = await run_gate(engine=engine, config=CONFIG, now=NOW, transport=transport,
                             resolve=inside)
    assert (summary.homes, summary.feeds_found) == (1, 0) and asked == []
    row = candidate(engine)
    assert (row.state, row.failures) == ("discovered", 1)
    assert row.last_error.startswith("refused: ") and "private address" in row.last_error


async def test_a_feed_that_moves_to_another_site_fails_its_probe(engine):
    propose(engine, f"https://{HOST}/moved")

    def handler(request):
        if request.url.host == HOST:
            return httpx.Response(301, headers={"Location": "https://elsewhere.example.net/feed"})
        return httpx.Response(200, content=feed(NOW))

    summary = await run_gate(engine=engine, config=CONFIG, now=NOW,
                             transport=httpx.MockTransport(handler), resolve=public)
    assert (summary.probes, summary.healthy) == (1, 0)
    assert "another site" in candidate(engine).last_error
