"""One ground-truth pass: the failure model, the batching, and what gets rescored.

The SQL these tests stand in front of is covered against a real Postgres in
tests/integration/test_groundtruth_db.py. What is tested here is the orchestration — which is where
the failure model lives, and the failure model is the part that has to hold when a register is down
rather than when everything works.
"""

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from worker.db.groundtruth import KevResult, ScoreResult
from worker.groundtruth import sync as sync_module
from worker.groundtruth.epss import EpssSnapshot
from worker.groundtruth.errors import GroundTruthError
from worker.groundtruth.kev import KevCatalogue
from worker.groundtruth.registers import RecordLookup
from worker.groundtruth.sync import CvssTally, SyncSummary, classify_lookup, sync_groundtruth
from worker.models import EpssScore, KevEntry

NOW = datetime(2026, 10, 2, 2, 25, tzinfo=UTC)

CATALOGUE = KevCatalogue(
    version="2026.09.30",
    released=NOW,
    declared_count=1,
    entries={"CVE-2024-3400": KevEntry(listed=True)},
)
SNAPSHOT = EpssSnapshot(
    model_version="v2026.06.15",
    score_date=NOW,
    scores={"CVE-2024-3400": EpssScore(score=0.94, status="known")},
)


def record_with_score(score: float) -> dict:
    return {
        "containers": {
            "cna": {"metrics": [{"cvssV3_1": {"baseScore": score, "vectorString": "CVSS:3.1/X"}}]}
        }
    }


@pytest.fixture
def wiring(monkeypatch):
    """Replace every network and database touch with a recorder.

    `state` is both the script and the transcript: set `kev_error` to make KEV fail, read `calls` to
    see what the pass decided to do about it.
    """
    state = SimpleNamespace(
        kev_error=None,
        epss_error=None,
        due=[],
        lookups={},
        kev_result=KevResult(unchanged=1),
        epss_result=ScoreResult(unchanged=1),
        cvss_tally=CvssTally(),
        events_for={},
        severity_changed=[],
        calls=[],
        rescored=[],
        batch_limits=[],
    )

    async def fetch_kev(client):
        state.calls.append("fetch_kev")
        if state.kev_error:
            raise GroundTruthError(state.kev_error)
        return CATALOGUE

    async def fetch_epss(client):
        state.calls.append("fetch_epss")
        if state.epss_error:
            raise GroundTruthError(state.epss_error)
        return SNAPSHOT

    async def fetch_cve_record(client, cve_id):
        state.calls.append(f"lookup:{cve_id}")
        return state.lookups.get(cve_id, RecordLookup("absent", detail="HTTP 404"))

    def write_kev(engine, catalogue):
        state.calls.append("write_kev")
        return state.kev_result

    def write_epss(engine, snapshot):
        state.calls.append("write_epss")
        return state.epss_result

    def events_for(engine, cve_ids):
        return sorted({e for c in cve_ids for e in state.events_for.get(c, [])})

    def due_for_cvss(engine, now, limit):
        state.batch_limits.append(limit)
        return state.due[:limit]

    def write_cvss(engine, lookups):
        state.calls.append(f"write_cvss:{len(lookups)}")
        return state.cvss_tally

    def write_severity(engine):
        state.calls.append("write_severity")
        return state.severity_changed

    def rescore(engine, config, touched, *, now):
        state.rescored.append(sorted(touched))
        return []

    for name, impl in {
        "fetch_kev": fetch_kev,
        "fetch_epss": fetch_epss,
        "fetch_cve_record": fetch_cve_record,
        "_write_kev": write_kev,
        "_write_epss": write_epss,
        "_events_for": events_for,
        "_due_for_cvss": due_for_cvss,
        "_write_cvss": write_cvss,
        "_write_severity": write_severity,
        "rescore": rescore,
    }.items():
        monkeypatch.setattr(sync_module, name, impl)
    monkeypatch.setattr(sync_module, "get_engine", lambda: SimpleNamespace())
    return state


async def run(**kwargs) -> SyncSummary:
    return await sync_groundtruth(now=NOW, cvss_batch=0, **kwargs)


# --- the failure model: one register down must not silence the others -------------------------


async def test_all_three_registers_are_read_in_one_pass(wiring):
    wiring.due = ["CVE-2024-3400"]
    await sync_groundtruth(now=NOW, cvss_batch=10)
    assert wiring.calls[:2] == ["fetch_kev", "write_kev"]
    assert "fetch_epss" in wiring.calls and "lookup:CVE-2024-3400" in wiring.calls


async def test_a_failing_kev_register_does_not_stop_epss(wiring):
    wiring.kev_error = "connection reset"
    summary = await run()
    assert summary.kev is None
    assert summary.epss == wiring.epss_result
    assert summary.errors == ["kev: connection reset"]


async def test_a_failing_epss_register_does_not_undo_kev(wiring):
    wiring.epss_error = "HTTP 503"
    summary = await run()
    assert summary.kev == wiring.kev_result
    assert summary.epss is None


async def test_a_failing_register_writes_nothing_for_itself(wiring):
    """The honest-data rule as a negative. A register that could not be read must not write.

    Writing `listed=false` on a failed KEV download would report a known-exploited CVE as
    unexploited, which is exactly the inference the whole register design exists to prevent.
    """
    wiring.kev_error = "timeout"
    await run()
    assert "write_kev" not in wiring.calls


async def test_both_registers_down_is_reported_and_not_an_exception(wiring):
    wiring.kev_error = "down"
    wiring.epss_error = "also down"
    summary = await run()
    assert len(summary.errors) == 2
    assert summary.changed_anything is False


# --- the CVSS batch ---------------------------------------------------------------------------


async def test_the_batch_size_is_what_is_asked_of_the_register(wiring):
    wiring.due = [f"CVE-2024-{n:04d}" for n in range(50)]
    await sync_groundtruth(now=NOW, cvss_batch=7)
    assert wiring.batch_limits == [7]
    assert sum(c.startswith("lookup:") for c in wiring.calls) == 7


async def test_a_zero_batch_skips_the_per_record_register_entirely(wiring):
    wiring.due = ["CVE-2024-3400"]
    await run()
    assert wiring.batch_limits == []
    assert not any(c.startswith("lookup:") for c in wiring.calls)


async def test_being_throttled_stops_the_batch_and_the_throttled_cve_is_not_recorded(wiring):
    """A 429 says nothing about the CVE, so nothing about the CVE may be written.

    `cve_cvss_checks` records what the register said about a CVE. "We were throttled" is a fact about
    us, and filing it as that CVE's outcome would push its next look-up six hours away for a reason
    that had nothing to do with it.
    """
    wiring.due = [f"CVE-2024-{n:04d}" for n in range(20)]
    wiring.lookups = {"CVE-2024-0000": RecordLookup("error", detail="HTTP 429", retry_after=True)}
    summary = await sync_groundtruth(now=NOW, cvss_batch=20)
    assert summary.cvss.backed_off is True
    assert any("back off" in e for e in summary.errors)
    written = [c for c in wiring.calls if c.startswith("write_cvss:")]
    # Whatever the concurrent workers managed before the event was set, the throttled id itself is
    # not among them — and the batch stopped well short of all twenty.
    assert written and int(written[0].split(":")[1]) < 20


async def test_a_lookup_that_raises_becomes_that_cves_error_and_not_the_pass(wiring, monkeypatch):
    async def explode(client, cve_id):
        raise RuntimeError("socket exploded")

    monkeypatch.setattr(sync_module, "fetch_cve_record", explode)
    wiring.due = ["CVE-2024-3400", "CVE-2024-3401"]
    summary = await sync_groundtruth(now=NOW, cvss_batch=2)
    assert summary.cvss is wiring.cvss_tally  # the write still happened
    assert "write_cvss:2" in wiring.calls


# --- what gets rescored ------------------------------------------------------------------------


async def test_events_whose_kev_status_moved_are_rescored(wiring):
    """KEV is an input to `urgency`, so a new listing leaves the stored score wrong.

    Naming them matters: `events_needing_score` skips events already scored below the rescore floor,
    so an event that just became known-exploited would otherwise keep its old low score.
    """
    wiring.kev_result = KevResult(updated=1, changed_cves=("CVE-2024-3400",))
    wiring.events_for = {"CVE-2024-3400": ["evt-1", "evt-2"]}
    await run()
    assert wiring.rescored == [["evt-1", "evt-2"]]


async def test_events_whose_severity_moved_are_rescored_too(wiring):
    wiring.severity_changed = ["evt-9"]
    await run()
    assert wiring.rescored == [["evt-9"]]


async def test_an_event_touched_by_both_is_rescored_once(wiring):
    wiring.kev_result = KevResult(updated=1, changed_cves=("CVE-2024-3400",))
    wiring.events_for = {"CVE-2024-3400": ["evt-1"]}
    wiring.severity_changed = ["evt-1"]
    await run()
    assert wiring.rescored == [["evt-1"]]


async def test_a_pass_that_changed_nothing_rescores_nothing(wiring):
    await run()
    assert wiring.rescored == []
    assert "write_severity" in wiring.calls  # still asked; just had nothing to say


async def test_severity_is_recomputed_even_when_every_register_failed(wiring):
    """Severity is derived from stored scores, not from this pass's downloads.

    A sync that could reach nothing still has yesterday's `cve_scores` to band, and a new event that
    arrived since the last sync mentioning an already-scored CVE should get its severity from it.
    """
    wiring.kev_error = wiring.epss_error = "down"
    wiring.severity_changed = ["evt-new"]
    summary = await run()
    assert summary.severity_changed == 1
    assert wiring.rescored == [["evt-new"]]


# --- classify_lookup: the one place a missing score could become a zero -----------------------


def test_a_record_with_a_score_is_scored():
    outcome, cvss = classify_lookup(RecordLookup("found", record=record_with_score(9.8)))
    assert outcome == "scored" and cvss.score == 9.8


def test_a_record_with_no_metric_is_unscored_and_carries_no_score():
    outcome, cvss = classify_lookup(RecordLookup("found", record={"containers": {"cna": {}}}))
    assert (outcome, cvss) == ("unscored", None)


def test_a_zero_score_is_a_score_and_not_an_absence():
    # 0.0 is a real CVSS measurement. Treating it as missing would throw away a reading, and
    # scoring.yaml already ranks `low: 1` below `unknown: 1.5` so the two stay distinguishable.
    outcome, cvss = classify_lookup(RecordLookup("found", record=record_with_score(0.0)))
    assert outcome == "scored" and cvss.score == 0.0


@pytest.mark.parametrize("outcome", ["absent", "error"])
def test_a_lookup_that_found_nothing_keeps_its_own_outcome(outcome):
    assert classify_lookup(RecordLookup(outcome, detail="why")) == (outcome, None)


# --- the summary is logged, so it has to stay a summary ----------------------------------------


def test_the_kev_result_does_not_spill_every_changed_cve_into_the_log():
    """The first live sync listed 1,731 CVEs and logged all of them, twice.

    `sync_groundtruth` and `worker.main` both log this dataclass whole, so a repr carrying
    `changed_cves` turned one summary line into roughly 140 KB. The counts say the same thing.
    """
    result = KevResult(updated=2, changed_cves=("CVE-2024-3400", "CVE-2023-1234"))
    assert "CVE-2024-3400" not in repr(result)
    assert "updated=2" in repr(result)
    assert result.changed_cves == ("CVE-2024-3400", "CVE-2023-1234")
