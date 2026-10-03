"""The nightly search (worker/discovery/run.py) with Tavily and the database stood in for.

The gate pass, which needs both the database and the web, is in
tests/integration/test_discovery_db.py.
"""

import json
from datetime import UTC, datetime

import httpx
import pytest

from worker.discovery import run
from worker.discovery.gate import DiscoveryConfig
from worker.settings import Settings

NOW = datetime(2026, 10, 3, 17, 0, tzinfo=UTC)
KEY = "fake-tavily-TESTONLY"


def config(**search) -> DiscoveryConfig:
    base = DiscoveryConfig.load().model_dump()
    base["search"].update({"queries": ["q0", "q1", "q2", "q3"], "credits_per_day": 30,
                           "skip_hosts": ["youtube.com"], **search})
    return DiscoveryConfig.model_validate(base)


def settings(key: str | None = KEY) -> Settings:
    return Settings(_env_file=None, database_url="postgresql://x", tavily_api_key=key)


@pytest.fixture
def db(monkeypatch):
    """The three database steps run_search takes, recorded."""
    state = {"spent": 0, "registered": {"cisa.gov"}, "stored": [], "known": set()}

    def store(engine, *, query, found, error, evidence, now):
        state["stored"].append({"query": query, "error": error, "evidence": evidence})
        new = [h for h, _ in evidence if h not in state["known"]]
        state["known"] |= set(new)
        return len(set(new))

    monkeypatch.setattr(run, "_spent", lambda engine, since: state["spent"])
    monkeypatch.setattr(run, "_registered", lambda engine: state["registered"])
    monkeypatch.setattr(run, "_store_search", store)
    return state


def tavily(results_for, status=200):
    asked = []

    async def handler(request):
        query = json.loads(request.content)["query"]
        asked.append(query)
        return httpx.Response(status, json={"results": results_for(query), "usage": {"credits": 1}})

    return httpx.MockTransport(handler), asked


async def search(transport, **kwargs):
    return await run.run_search(
        engine=object(), settings=kwargs.pop("settings", settings()),
        config=kwargs.pop("config", config()), now=NOW, transport=transport, **kwargs,
    )


async def test_no_key_means_no_search(db):
    transport, asked = tavily(lambda q: [])
    summary = await search(transport, settings=settings(None))
    assert summary.skipped and "TAVILY_API_KEY" in summary.skipped
    assert asked == [] and db["stored"] == []


async def test_a_spent_allowance_means_no_search(db):
    db["spent"] = 30
    transport, asked = tavily(lambda q: [])
    summary = await search(transport)
    assert summary.skipped and asked == []


async def test_the_allowance_left_caps_the_searches_and_the_start_rotates_daily(db):
    db["spent"] = 28
    transport, asked = tavily(lambda q: [])
    summary = await search(transport)
    start = NOW.toordinal() % 4
    assert asked == [f"q{start}", f"q{(start + 1) % 4}"]
    assert summary.searches == 2 and summary.credits == 2


async def test_hits_on_platforms_and_registered_sites_are_not_found(db):
    def results(query):
        return [
            {"url": "https://www.youtube.com/watch?v=1", "title": "video"},
            {"url": "https://www.cisa.gov/news/1", "title": "registered"},
            {"url": "http://insecure.example.org/a", "title": "not https"},
            {"url": "https://news.example.org/story?utm_source=x", "title": "A story"},
        ]

    transport, _ = tavily(results)
    summary = await search(transport, config=config(queries=["only"]))
    [stored] = db["stored"]
    assert [host for host, _ in stored["evidence"]] == ["news.example.org"]
    evidence = stored["evidence"][0][1]
    assert evidence["url"] == "https://news.example.org/story"  # the query is dropped
    assert evidence["title"] == "A story" and evidence["query"] == "only"
    assert (summary.hits, summary.new_hosts) == (4, 1)


async def test_a_refused_key_stops_the_night_after_one_search(db):
    transport, asked = tavily(lambda q: [], status=401)
    summary = await search(transport)
    assert len(asked) == 1 and summary.searches == 1
    assert db["stored"][0]["error"] == "Tavily answered HTTP 401"
    assert KEY not in json.dumps(db["stored"]) and KEY not in " ".join(summary.errors)


async def test_a_server_error_is_recorded_and_the_next_query_tried(db):
    transport, asked = tavily(lambda q: [], status=502)
    summary = await search(transport)
    assert len(asked) == 4 and len(summary.errors) == 4
    assert summary.credits == 0
