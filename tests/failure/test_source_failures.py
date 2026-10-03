"""§11 "One source fails": others continue, health is recorded, the degradation counter
increments. And "Tavily down": discovery is skipped, collection is unaffected.

Every source goes through the real `_collect_safe` (fetch, raw cache, parse), over an
httpx.MockTransport. Nothing reaches the network.
"""

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr

from worker import scheduler
from worker.cadence import FAST_INTERVAL
from worker.collectors import http as http_mod
from worker.collectors.http import FetchStatus
from worker.db.jobs import JobRun
from worker.db.sources import FetchState
from worker.discovery import run as discovery_run
from worker.discovery.gate import DiscoveryConfig
from worker.models import HealthStatus, LifecycleState, SourceConfig, SourceHealth
from worker.pipeline import run as pipeline_run
from worker.pipeline.health import assess, next_lifecycle_state
from worker.settings import Settings
from worker.watchdog.checks import (
    FAILING_AFTER,
    Jobs,
    SourceState,
    job_findings,
    source_findings,
)

from .conftest import KEY

FEEDS = Path(__file__).resolve().parents[1] / "fixtures" / "feeds"
NOW = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)


def source(sid: str, *, parser="rss", type="rss") -> SourceConfig:
    return SourceConfig(
        id=sid, name=sid.title(), type=type, region="AU", category="advisory",
        source_class="AUTHORITATIVE", priority=1, lane="fast", enabled=True,
        url=f"https://{sid}.example.org/feed", parser=parser, expected_frequency="daily",
    )  # fmt: skip


@pytest.fixture(autouse=True)
def no_backoff(monkeypatch):
    async def instant(duration):
        return None

    monkeypatch.setattr(http_mod, "_async_sleep", instant)


async def collect(handler, *sources):
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        gate = asyncio.Semaphore(4)
        return await asyncio.gather(
            *(
                pipeline_run._collect_safe(client, gate, s, FetchState(None, None), now=NOW)
                for s in sources
            )
        )


GOOD = (FEEDS / "acsc_alerts.xml").read_bytes()


async def test_a_feed_answering_5xx_is_retried_then_recorded_as_an_error():
    hits: list[str] = []

    def handler(request):
        hits.append(request.url.host)
        return httpx.Response(503)

    [got] = await collect(handler, source("down"))
    assert got.fetch.status is FetchStatus.ERROR and got.items == []
    assert "HTTP 503 after 3 attempts" in got.fetch.error and len(hits) == 3
    health = assess(got.source, got.fetch, got.items, now=NOW, history=[])
    assert health.status is HealthStatus.ERROR


async def test_a_feed_that_times_out_is_recorded_as_a_timeout():
    def handler(request):
        raise httpx.ReadTimeout("read timed out", request=request)

    [got] = await collect(handler, source("slow"))
    assert got.fetch.status is FetchStatus.TIMEOUT
    assert assess(got.source, got.fetch, [], now=NOW, history=[]).status is HealthStatus.TIMEOUT


async def test_a_feed_that_hangs_past_its_deadline_is_cut_off(monkeypatch):
    monkeypatch.setattr(pipeline_run, "SOURCE_DEADLINE_SECONDS", 0.05)

    async def handler(request):
        await asyncio.sleep(5)
        return httpx.Response(200, content=GOOD)

    [got] = await collect(handler, source("hung"))
    assert got.fetch.status is FetchStatus.TIMEOUT and "no response within" in got.fetch.error


async def test_a_feed_answering_garbage_yields_nothing_and_its_body_is_kept(tmp_path):
    def handler(request):
        if request.url.host.startswith("junk"):
            return httpx.Response(200, content=(FEEDS / "malformed.xml").read_bytes())
        return httpx.Response(200, content=b'["not", "a", "kev", "catalogue"]')

    junk, wrong = await collect(handler, source("junk"), source("wrong", parser="kev", type="json"))
    assert junk.items == []
    assert assess(junk.source, junk.fetch, [], now=NOW, history=[]).status is HealthStatus.EMPTY
    assert wrong.fetch.status is FetchStatus.ERROR and "parse failed" in wrong.fetch.error
    # The raw cache holds both bodies, so a parser fix can be checked against what came back.
    assert {p.parts[-3] for p in (tmp_path / "raw").rglob("*.raw")} == {"junk", "wrong"}


async def test_one_failing_source_does_not_stop_the_others():
    def handler(request):
        if request.url.host.startswith("broken"):
            raise httpx.ConnectError("refused", request=request)
        return httpx.Response(200, content=GOOD)

    results = await collect(handler, source("first"), source("broken"), source("last"))
    by_id = {r.source.id: r for r in results}
    assert by_id["broken"].fetch.status is FetchStatus.ERROR
    assert by_id["first"].items and by_id["last"].items
    assert by_id["first"].fetch.status is by_id["last"].fetch.status is FetchStatus.OK


def failed(n: int) -> list[SourceHealth]:
    """`n` failed checks, one FAST run apart."""
    return [
        SourceHealth(source_id="down", checked_at=NOW - FAST_INTERVAL * i,
                     status=HealthStatus.ERROR, error="HTTP 503 after 3 attempts")
        for i in range(n)
    ]  # fmt: skip


def watched(failures: int) -> SourceState:
    return SourceState(
        source_id="down", name="Down", lane="fast", priority=1, host="down.example.org",
        lifecycle="active", statuses=("error",) * failures, had_items=True,
        last_error="HTTP 503 after 3 attempts", last_checked=NOW,
    )  # fmt: skip


def test_five_failures_in_a_row_degrade_a_source_and_its_lane_s_count_opens_a_finding():
    active = source("down").model_copy(update={"lifecycle_state": LifecycleState.ACTIVE})
    assert next_lifecycle_state(active, failed(4)) is LifecycleState.ACTIVE
    assert next_lifecycle_state(active, failed(5)) is LifecycleState.DEGRADED

    # A fast source's finding does not wait for it to degrade: hourly, 5 checks is 5 hours.
    opens = FAILING_AFTER["fast"]
    assert opens <= 5
    assert source_findings([watched(opens - 1)]) == []
    [finding] = source_findings([watched(opens)])
    assert (finding.kind, finding.subject, finding.severity) == ("feed-failing", "down", "high")


# ─── Tavily down ──────────────────────────────────────────────────────────────────────────────────


def discovery_config() -> DiscoveryConfig:
    base = DiscoveryConfig.load().model_dump()
    base["search"].update({"queries": [f"q{n}" for n in range(4)], "credits_per_day": 30})
    return DiscoveryConfig.model_validate(base)


@pytest.mark.parametrize("answer", ["connect", 503])
async def test_tavily_down_skips_discovery_without_raising(monkeypatch, answer):
    asked: list[str] = []

    def handler(request):
        asked.append(request.url.host)
        if answer == "connect":
            raise httpx.ConnectError("refused", request=request)
        return httpx.Response(answer)

    monkeypatch.setattr(discovery_run, "_spent", lambda engine, since: 0)
    monkeypatch.setattr(discovery_run, "_registered", lambda engine: set())
    monkeypatch.setattr(discovery_run, "_store_search", lambda engine, **kw: 0)
    settings = Settings(
        _env_file=None, database_url="postgresql://x", tavily_api_key=SecretStr(KEY)
    )
    summary = await discovery_run.run_search(
        engine=object(), settings=settings, config=discovery_config(), now=NOW,
        transport=httpx.MockTransport(handler),
    )  # fmt: skip
    assert len(summary.errors) == 4 and len(asked) == 4
    assert summary.new_hosts == 0


async def test_a_discovery_pass_that_raises_is_recorded_and_the_schedule_goes_on(monkeypatch):
    recorded: list[JobRun] = []

    async def record(run):
        recorded.append(run)

    async def boom(**kwargs):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(scheduler, "_record", record)
    monkeypatch.setattr(scheduler, "run_search", boom)
    monkeypatch.setattr(scheduler, "get_engine", lambda: None)
    monkeypatch.setattr(scheduler, "get_settings", lambda: None)
    await scheduler._discovery_job()
    assert [(r.job, r.completed) for r in recorded] == [("discovery", False)]
    # Two days of that and the watchdog opens a medium job-failing incident.
    late = NOW + timedelta(hours=51)
    jobs = Jobs(latest={"discovery": recorded[0]}, completed={})
    found = [(f.kind, f.subject, f.severity) for f in job_findings(jobs, late)]
    assert ("job-failing", "discovery", "medium") in found


def test_collection_does_not_read_tavily():
    """A lane's sources are the registry and the stored discovered ones: no search runs."""
    source_text = Path(pipeline_run.__file__).read_text(encoding="utf-8")
    assert "tavily" not in source_text.lower() and "run_search" not in source_text
