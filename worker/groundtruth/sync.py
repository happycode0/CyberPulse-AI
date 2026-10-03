"""One ground-truth pass: read the registers, write what they say, rescore what moved.

This is to `worker/groundtruth/` what `worker/pipeline/run.py` is to the collectors — the part that
wires pure parsers to a database and a clock. It is deliberately a separate pass from a lane run,
because the registers answer on a different clock than the feeds do: EPSS republishes once a day,
KEV on CISA's working days, and a CVE's CNA record may gain a CVSS metric weeks after the advisory
that made us care about it. Polling them at the hourly FAST cadence would be noise.

The failure model is the lane's, applied per register (PLAN.md §11): KEV failing must not stop EPSS,
and neither failing may stop the CVSS batch. Each register is read and written in its own
transaction, so a register that returns a usable answer is recorded even when another is down. The
one thing that is never done on failure is writing a default — `GroundTruthError` means every answer
that register would have given stays `unknown`, which is the whole reason the registers raise instead
of returning empty.

Two slower registers ride along. OSV is asked, a batch at a time, which packages each CVE affects
and which versions fix it, with GitHub's reviewed advisories read through OSV's mirror of them. The
MITRE ATT&CK and ATLAS indexes are read every pass, and a catalogue is downloaded only when a release
appears that is not loaded yet; technique suggestions choose from what is loaded (worker/ai/mitre.py).

Severity and rescoring come last, because they are the consequences rather than the inputs: `urgency`
is computed from the severity band and the KEV bonus, so a sync that moved either has left stored
scores describing a world that no longer exists.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TypeVar

import httpx
from sqlalchemy import Engine

from worker.db.advisories import cves_due_for_advisories, record_advisories, record_advisory_check
from worker.db.groundtruth import (
    KevResult,
    ScoreResult,
    apply_kev,
    cves_due_for_cvss,
    events_for_cves,
    record_cvss,
    record_cvss_check,
    record_epss,
    set_event_severity,
)
from worker.db.material import record_register_changes
from worker.db.mitre import load_catalogue, loaded_versions
from worker.db.session import get_engine
from worker.groundtruth.cvss import resolve_cvss
from worker.groundtruth.epss import EpssSnapshot
from worker.groundtruth.errors import GroundTruthError
from worker.groundtruth.kev import KevCatalogue
from worker.groundtruth.mitre import Catalogue
from worker.groundtruth.registers import (
    AdvisoryLookup,
    RecordLookup,
    fetch_advisories,
    fetch_cve_record,
    fetch_epss,
    fetch_kev,
    fetch_mitre_catalogue,
    fetch_mitre_release,
)
from worker.models import CvssScore
from worker.pipeline.material import fix_published, kev_listing
from worker.pipeline.run import DEFAULT_SCORING_PATH, rescore
from worker.pipeline.score import ScoringConfig

logger = logging.getLogger(__name__)

T = TypeVar("T")

# How many CVEs to look CVSS up for in one pass. At four passes a day this backfills a few thousand
# CVEs in under a week and then idles at whatever `RECHECK_HOURS` asks for. The number is a courtesy
# budget, not a performance limit: the register is a free public service and the honest way to use it
# is slowly.
DEFAULT_CVSS_BATCH = 400

# CVEs to read OSV advisories for in one pass. Each costs one request for OSV's own record and one
# per GitHub advisory it names (at most three), so this is up to 800 requests to a free service.
DEFAULT_ADVISORY_BATCH = 200

# The matrices whose catalogues are kept. ATLAS is MITRE's matrix for attacks on AI systems.
MITRE_MATRICES = ("enterprise", "atlas")

# Four at a time. The register publishes no rate limit, so this is chosen to stay obviously below any
# plausible one rather than to go fast — the backfill has days to finish and nothing is waiting on it.
MAX_CONCURRENT_RECORD_FETCHES = 4

# `record_cvss_check` writes one row per CVE and `record_cvss` another, so a whole batch in one
# transaction would hold a write lock for its duration. Chunking bounds that, and bounds how much a
# mid-batch failure throws away.
WRITE_CHUNK = 100

# Which `CvssTally` counter each outcome increments. A mapping rather than a chain of ifs so that
# adding an outcome to `cve_cvss_checks` fails loudly here with a KeyError instead of being counted
# as whatever the final `else` happened to be.
_TALLY_FIELD = {
    "scored": "scored",
    "unscored": "unscored",
    "absent": "absent",
    "error": "errored",
}


@dataclass
class CvssTally:
    """Outcomes of one CVSS batch, by what the register said.

    `recorded` counts rows actually written to `cve_scores`, which is smaller than `scored` in the
    steady state: most re-checks confirm a score that has not moved, and confirming is not an event.
    """

    scored: int = 0
    unscored: int = 0
    absent: int = 0
    errored: int = 0
    recorded: int = 0
    backed_off: bool = False


@dataclass
class AdvisoryTally:
    """Outcomes of one advisory batch. `recorded` counts CVEs whose advisories changed."""

    found: int = 0
    absent: int = 0
    errored: int = 0
    recorded: int = 0
    backed_off: bool = False
    # Events given a NEW_PATCH because an advisory gained a fixed version: their freshness moved.
    patched_events: set[str] = field(default_factory=set, repr=False)


@dataclass
class SyncSummary:
    """What one pass did. `None` for a register means it could not be read at all."""

    kev: KevResult | None = None
    epss: ScoreResult | None = None
    cvss: CvssTally = field(default_factory=CvssTally)
    advisories: AdvisoryTally = field(default_factory=AdvisoryTally)
    # MITRE releases loaded in this pass, such as 'ATT&CK v19.2'. Usually none.
    mitre_loaded: list[str] = field(default_factory=list)
    severity_changed: int = 0
    rescored: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def changed_anything(self) -> bool:
        """Whether any published value moved, and so whether a republish has anything to say.

        Counts writes, not reads: a pass that confirmed 5,000 unchanged EPSS scores and re-checked
        400 CVEs that still have no CVSS changed nothing a reader could see. The scheduler uses this
        to decide whether to republish, and in the steady state the answer is usually no — the FAST
        lane republishes after every run regardless, so the only thing at stake is whether a sync
        makes the site wait for it.
        """
        kev_moved = bool(self.kev and (self.kev.updated or self.kev.delisted))
        epss_moved = bool(self.epss and self.epss.recorded)
        return bool(
            kev_moved
            or epss_moved
            or self.cvss.recorded
            or self.advisories.recorded
            or self.severity_changed
        )


async def sync_groundtruth(
    *,
    engine: Engine | None = None,
    now: datetime | None = None,
    cvss_batch: int = DEFAULT_CVSS_BATCH,
    advisory_batch: int = DEFAULT_ADVISORY_BATCH,
    mitre: bool = True,
    scoring_path: Path | None = None,
) -> SyncSummary:
    """Read every register once and write what changed.

    Parameters
    ----------
    engine, now, scoring_path
        Default to the process database, the wall clock and `config/scoring.yaml`; present so tests
        can substitute them, exactly as `run_lane` does.
    cvss_batch : int
        How many CVEs to resolve CVSS for. 0 skips the per-record register entirely, which is what a
        caller wants when it only needs the bulk registers refreshed.
    advisory_batch : int
        How many CVEs to read OSV advisories for. 0 skips OSV.
    mitre : bool
        Whether to check for a new ATT&CK or ATLAS release, and load it if there is one.

    Returns
    -------
    SyncSummary
        Per-register outcomes. Never raises for a register failure: the message lands in `errors`
        and the pass continues, because a register being down is a normal Tuesday and the other two
        still have something to say.
    """
    engine = engine or get_engine()
    moment = now if now is not None else datetime.now(UTC)
    summary = SyncSummary()

    async with httpx.AsyncClient(follow_redirects=True) as client:
        touched: set[str] = set()

        try:
            catalogue = await fetch_kev(client)
        except GroundTruthError as exc:
            # Not fatal and not a default. Every CVE keeps whatever KEV status was last confirmed,
            # which is the honest state: the last reading still stands, and the next sync will
            # correct it. Writing `listed=false` here instead would report a known-exploited CVE as
            # unexploited on the strength of a failed download.
            logger.error("KEV register unavailable, listings left as they stand: %s", exc)
            summary.errors.append(f"kev: {exc}")
        else:
            summary.kev = await asyncio.to_thread(_write_kev, engine, catalogue)
            touched |= set(await asyncio.to_thread(_events_for, engine, summary.kev.changed_cves))

        try:
            snapshot = await fetch_epss(client)
        except GroundTruthError as exc:
            logger.error("EPSS register unavailable, scores left as they stand: %s", exc)
            summary.errors.append(f"epss: {exc}")
        else:
            summary.epss = await asyncio.to_thread(_write_epss, engine, snapshot)

        if cvss_batch > 0:
            due = await asyncio.to_thread(_due_for_cvss, engine, moment, cvss_batch)
            logger.info("resolving CVSS for %d of up to %d due CVEs", len(due), cvss_batch)
            lookups, backed_off = await _resolve_records(client, due)
            summary.cvss = await asyncio.to_thread(_write_cvss, engine, lookups)
            summary.cvss.backed_off = backed_off
            if backed_off:
                summary.errors.append("cvss: register asked us to back off; batch cut short")

        if advisory_batch > 0:
            due = await asyncio.to_thread(_due_for_advisories, engine, moment, advisory_batch)
            logger.info("reading advisories for %d of up to %d due CVEs", len(due), advisory_batch)
            found, backed_off = await _resolve_advisories(client, due)
            summary.advisories = await asyncio.to_thread(_write_advisories, engine, found, moment)
            summary.advisories.backed_off = backed_off
            touched |= summary.advisories.patched_events
            if backed_off:
                summary.errors.append("advisories: OSV asked us to back off; batch cut short")

        if mitre:
            summary.mitre_loaded, errors = await _sync_mitre(client, engine)
            summary.errors.extend(errors)

    changed_events = await asyncio.to_thread(_write_severity, engine)
    summary.severity_changed = len(changed_events)
    touched |= set(changed_events)

    if touched:
        scoring = ScoringConfig.load(scoring_path or DEFAULT_SCORING_PATH)
        errors = await asyncio.to_thread(rescore, engine, scoring, touched, now=moment)
        summary.errors.extend(errors)
        summary.rescored = len(touched)

    logger.info(
        "ground-truth sync: kev=%s epss=%s cvss=%s advisories=%s mitre_loaded=%s "
        "severity_changed=%d rescored=%d errors=%d",
        summary.kev,
        summary.epss,
        summary.cvss,
        summary.advisories,
        summary.mitre_loaded,
        summary.severity_changed,
        summary.rescored,
        len(summary.errors),
    )
    return summary


async def _drain(
    ids: list[str],
    lookup: Callable[[str], Awaitable[T | None]],
    on_raise: Callable[[Exception], T],
    register: str,
) -> tuple[list[tuple[str, T]], bool]:
    """Look every id up, a few at a time, stopping early if asked to back off.

    A queue and a fixed pool rather than `asyncio.gather` over every id, because the backing-off has
    to be able to cancel work that has not started yet. With `gather` all several hundred requests
    are already in flight by the time the first 429 arrives, and the response to being rate-limited
    would be to finish rate-limiting ourselves.

    `lookup` returns None for "throttled". A throttled id is not recorded at all: the check tables
    record what the register said about a CVE, and "we were throttled" is a fact about us.
    """
    queue: asyncio.Queue[str] = asyncio.Queue()
    for item in ids:
        queue.put_nowait(item)

    results: list[tuple[str, T]] = []
    backoff = asyncio.Event()

    async def worker() -> None:
        while not backoff.is_set():
            try:
                item = queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            try:
                result = await lookup(item)
            except Exception as exc:
                # The lookups are written not to raise, so this is the belt to their braces:
                # one malformed id must not take the rest of the batch with it.
                logger.exception("%s lookup for %s failed", register, item)
                result = on_raise(exc)
            if result is None:
                logger.warning(
                    "the %s register throttled us at %s; stopping the batch", register, item
                )
                backoff.set()
                return
            results.append((item, result))

    await asyncio.gather(*(worker() for _ in range(MAX_CONCURRENT_RECORD_FETCHES)))
    return results, backoff.is_set()


async def _resolve_records(
    client: httpx.AsyncClient, cve_ids: list[str]
) -> tuple[list[tuple[str, RecordLookup]], bool]:
    """Look every CVE's record up for its CVSS."""

    async def lookup(cve_id: str) -> RecordLookup | None:
        found = await fetch_cve_record(client, cve_id)
        return None if found.retry_after else found

    return await _drain(
        cve_ids,
        lookup,
        lambda exc: RecordLookup("error", detail=f"lookup raised: {exc!r}"),
        "CVE record",
    )


async def _resolve_advisories(
    client: httpx.AsyncClient, cve_ids: list[str]
) -> tuple[list[tuple[str, AdvisoryLookup]], bool]:
    return await _drain(
        cve_ids,
        lambda cve_id: fetch_advisories(client, cve_id),
        lambda exc: AdvisoryLookup("error", detail=f"lookup raised: {exc!r}"),
        "OSV",
    )


async def _sync_mitre(client: httpx.AsyncClient, engine: Engine) -> tuple[list[str], list[str]]:
    """Load any ATT&CK or ATLAS release that is newer than what is loaded.

    Returns the releases loaded and the errors met. A matrix that cannot be read keeps the
    catalogue already loaded, which suggestions go on using.
    """
    loaded: list[str] = []
    errors: list[str] = []
    for matrix in MITRE_MATRICES:
        try:
            release = await fetch_mitre_release(client, matrix)
            if release.version in await asyncio.to_thread(_loaded_mitre, engine, matrix):
                continue
            logger.info("MITRE %s: %s is new, downloading it", matrix, release.version)
            catalogue = await fetch_mitre_catalogue(client, release)
        except GroundTruthError as exc:
            logger.error("MITRE %s unavailable, the loaded catalogue stands: %s", matrix, exc)
            errors.append(f"mitre {matrix}: {exc}")
            continue
        count = await asyncio.to_thread(_load_mitre, engine, catalogue)
        logger.info("MITRE %s: loaded %s with %d techniques", matrix, release.version, count)
        loaded.append(release.version)
    return loaded, errors


def _write_kev(engine: Engine, catalogue: KevCatalogue) -> KevResult:
    """The listings, and what a new one changes on the events naming its CVE (whose ids are
    among `changed_cves`, so they are rescored with the rest)."""
    with engine.begin() as conn:
        result = apply_kev(conn, catalogue)
        record_register_changes(conn, [kev_listing(c, d) for c, d in result.newly_listed])
        return result


def _write_epss(engine: Engine, snapshot: EpssSnapshot) -> ScoreResult:
    with engine.begin() as conn:
        return record_epss(conn, snapshot.scores)


def _events_for(engine: Engine, cve_ids: tuple[str, ...]) -> list[str]:
    with engine.connect() as conn:
        return events_for_cves(conn, cve_ids)


def _due_for_cvss(engine: Engine, now: datetime, limit: int) -> list[str]:
    with engine.connect() as conn:
        return cves_due_for_cvss(conn, now=now, limit=limit)


def classify_lookup(lookup: RecordLookup) -> tuple[str, CvssScore | None]:
    """Turn one record lookup into the outcome to record and the score to store, if any.

    This is where the honest-data rule meets the one case that most invites breaking it. A record
    that exists and carries no CVSS metric in any container is 'unscored' with no score — not a 0.0,
    and not an error either, because the register answered perfectly well. `cve_scores` cannot hold
    that answer at all: its check constraint forbids a `cvss` row without a score, precisely so an
    unscored CVE can never be filed as harmless. `cve_cvss_checks` is where it goes instead.

    Returns
    -------
    tuple[str, CvssScore | None]
        A value for `cve_cvss_checks.outcome`, and the score to write to `cve_scores` when there is
        one. 'scored' is the only outcome that comes with a score, and the only one that does.
    """
    if lookup.outcome != "found":
        return lookup.outcome, None
    cvss = resolve_cvss(lookup.record)
    if cvss is None:
        return "unscored", None
    return "scored", cvss


def _write_cvss(engine: Engine, lookups: list[tuple[str, RecordLookup]]) -> CvssTally:
    tally = CvssTally()
    for start in range(0, len(lookups), WRITE_CHUNK):
        chunk = lookups[start : start + WRITE_CHUNK]
        with engine.begin() as conn:
            for cve_id, lookup in chunk:
                outcome, cvss = classify_lookup(lookup)
                detail = lookup.detail
                if cvss is not None:
                    if record_cvss(conn, cve_id, cvss):
                        tally.recorded += 1
                    detail = f"{cvss.score} from {cvss.source}"
                record_cvss_check(conn, cve_id, outcome, detail)
                setattr(tally, _TALLY_FIELD[outcome], getattr(tally, _TALLY_FIELD[outcome]) + 1)
    return tally


def _write_severity(engine: Engine) -> list[str]:
    with engine.begin() as conn:
        return set_event_severity(conn)


def _due_for_advisories(engine: Engine, now: datetime, limit: int) -> list[str]:
    with engine.connect() as conn:
        return cves_due_for_advisories(conn, now=now, limit=limit)


_ADVISORY_TALLY_FIELD = {"found": "found", "absent": "absent", "error": "errored"}


def _write_advisories(
    engine: Engine, lookups: list[tuple[str, AdvisoryLookup]], now: datetime
) -> AdvisoryTally:
    tally = AdvisoryTally()
    for start in range(0, len(lookups), WRITE_CHUNK):
        chunk = lookups[start : start + WRITE_CHUNK]
        with engine.begin() as conn:
            for cve_id, lookup in chunk:
                if lookup.advisories or lookup.complete:
                    change = record_advisories(
                        conn, cve_id, lookup.advisories, now=now, complete=lookup.complete
                    )
                    tally.recorded += change.changed
                    tally.patched_events |= record_register_changes(
                        conn, [fix_published(cve_id, a, now=now) for a in change.fixed]
                    )
                record_advisory_check(conn, cve_id, lookup.outcome, lookup.detail, now=now)
                name = _ADVISORY_TALLY_FIELD[lookup.outcome]
                setattr(tally, name, getattr(tally, name) + 1)
    return tally


def _loaded_mitre(engine: Engine, matrix: str) -> set[str]:
    with engine.connect() as conn:
        return loaded_versions(conn, matrix)


def _load_mitre(engine: Engine, catalogue: Catalogue) -> int:
    with engine.begin() as conn:
        return load_catalogue(conn, catalogue)
