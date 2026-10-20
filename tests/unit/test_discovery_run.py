"""The nightly search (worker/discovery/run.py) with Tavily and the database stood in for, and
the gate's choices that need neither: which candidates a pass probes, and how activation makes
room.

The whole gate pass, which needs both the database and the web, is in
tests/integration/test_discovery_db.py.
"""

import json
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from worker.db import discovery as store  # `db` is the search's fixture below
from worker.discovery import run
from worker.discovery.gate import DiscoveryConfig, source_id_for
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


# --- SERAPH's gate: which candidates a pass probes -----------------------------------------------

GATE = DiscoveryConfig.load().gate
READY = GATE.probes_to_activate


def candidate(n: int, *, passes=0, failures=0, hours_ago=None, state=None) -> store.Candidate:
    return store.Candidate(
        id=n, host=f"c{n}.example.org", feed_url=f"https://c{n}.example.org/feed", name=None,
        found_by="tachikoma", reason=None, proposed_at=None, evidence=[],
        state=state or ("testing" if passes else "candidate"), passes=passes, failures=failures,
        last_probe_at=NOW - timedelta(hours=hours_ago) if hours_ago is not None else None,
        last_result=None, last_error=None, source_id=None, created_at=NOW, updated_at=NOW,
    )


def pick(candidates, limit=GATE.probes_per_pass):
    return [c.id for c in store.pick_probes(candidates, limit=limit, now=NOW, gate=GATE)]


def test_a_ready_candidate_waiting_for_room_is_probed_once_a_day_not_every_pass():
    waiting = candidate(1, passes=READY + 14, hours_ago=4)
    testing = candidate(2, passes=2, hours_ago=4)
    assert pick([waiting, testing]) == [2]
    # A day on, less the hour a pass may come early, it is due again.
    day = (timedelta(hours=GATE.ready_reprobe_hours) - store.PROBE_EARLY) / timedelta(hours=1)
    assert pick([candidate(1, passes=READY, hours_ago=day)]) == [1]
    assert pick([candidate(1, passes=READY, hours_ago=day - 1)]) == []


def test_new_candidates_get_their_probes_before_those_already_ready():
    """2026-10-20: ten ready candidates took all ten probes of every pass, so fifteen found days
    before had never been probed."""
    ready = [candidate(n, passes=READY + 14 + n, hours_ago=30) for n in range(1, 11)]
    new = [candidate(n) for n in range(11, 26)]
    assert pick(ready + new) == list(range(11, 21))
    # With probes to spare, the ready ones still get their daily one, furthest through first.
    assert pick(ready + new[:5]) == [11, 12, 13, 14, 15, 10, 9, 8, 7, 6]


def test_a_candidate_never_probed_comes_first_then_those_furthest_through():
    candidates = [
        candidate(1, passes=1, hours_ago=8),
        candidate(2, passes=4, hours_ago=4),
        candidate(3, failures=1, hours_ago=4),
        candidate(4),
        candidate(5, passes=4, hours_ago=12),
    ]
    assert pick(candidates) == [4, 5, 2, 1, 3]


def test_a_candidate_probed_within_the_gap_or_no_longer_open_is_not_probed():
    assert pick([candidate(1, passes=2, hours_ago=2),
                 candidate(2, passes=3, hours_ago=10, state="active"),
                 candidate(3, state="discovered")]) == []


# --- SERAPH's gate: making room for what is ready ------------------------------------------------


class Engine:
    @contextmanager
    def begin(self):
        yield "conn"


@pytest.fixture
def gate_db(monkeypatch):
    """The activation pass's database steps, stood in for; what they were asked."""
    state = {"ready": [], "active": GATE.max_active, "weak": [], "taken": set(),
             "asked_to_retire": [], "activated": []}

    def retire_weak(conn, *, need, prune, now):
        state["asked_to_retire"].append(need)
        return [store.Retired(s, "retired for a ready candidate: ...")
                for s in state["weak"][: min(need, prune.per_pass)]]

    def activate(conn, candidate, source, *, now):
        state["activated"].append(source.id)
        return True

    monkeypatch.setattr(store, "ready", lambda conn, gate: state["ready"])
    monkeypatch.setattr(store, "active_discovered", lambda conn: state["active"])
    monkeypatch.setattr(store, "source_id_taken", lambda conn, sid: sid in state["taken"])
    monkeypatch.setattr(store, "retire_weak", retire_weak)
    monkeypatch.setattr(store, "activate", activate)
    return state


def test_a_full_cap_retires_the_weakest_and_the_ready_take_their_place_in_the_same_pass(gate_db):
    gate_db["ready"] = [candidate(1, passes=40), candidate(2, passes=30)]
    gate_db["weak"] = ["found_weak_a", "found_weak_b", "found_weak_c"]
    activated, waiting, pruned = run._activate_ready(Engine(), GATE, NOW)
    assert gate_db["asked_to_retire"] == [2]
    assert pruned == ["found_weak_a", "found_weak_b"]
    assert activated == [source_id_for("c1.example.org"), source_id_for("c2.example.org")]
    assert waiting == 0


def test_no_more_room_is_made_than_the_pass_allows(gate_db):
    gate_db["ready"] = [candidate(n, passes=40 - n) for n in range(1, 5)]
    gate_db["weak"] = ["found_a", "found_b", "found_c", "found_d"]
    activated, waiting, pruned = run._activate_ready(Engine(), GATE, NOW)
    assert len(pruned) == len(activated) == GATE.prune.per_pass
    assert waiting == 4 - GATE.prune.per_pass


def test_nothing_is_retired_while_there_is_room(gate_db):
    gate_db["active"] = GATE.max_active - 2
    gate_db["ready"] = [candidate(1, passes=READY), candidate(2, passes=READY)]
    activated, waiting, pruned = run._activate_ready(Engine(), GATE, NOW)
    assert gate_db["asked_to_retire"] == [] and pruned == []
    assert len(activated) == 2 and waiting == 0


def test_with_no_weak_source_a_ready_candidate_waits(gate_db):
    gate_db["ready"] = [candidate(1, passes=READY)]
    assert run._activate_ready(Engine(), GATE, NOW) == ([], 1, [])
    assert gate_db["activated"] == []


def test_nothing_is_retired_for_a_candidate_whose_source_id_is_taken(gate_db):
    gate_db["ready"] = [candidate(1, passes=READY)]
    gate_db["taken"] = {source_id_for("c1.example.org")}
    gate_db["weak"] = ["found_a"]
    assert run._activate_ready(Engine(), GATE, NOW) == ([], 0, [])
    assert gate_db["asked_to_retire"] == []
