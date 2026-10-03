"""Downloading the registers. The parsing lives next door; this is only the network half.

Three registers, two shapes. KEV and EPSS are bulk: one download answers for every CVE at once.
The CVSS chain of PLAN.md §2.5 is per-record, so it is one request per CVE and the only one of the
three that needs rationing.

Two decisions here are deliberate and easy to "fix" into bugs:

**No conditional requests.** `worker/collectors/http.fetch` will happily send If-None-Match, and
`source_fetch_state` exists to make that cheap — but a register is applied to a *moving set of CVEs*,
not to the payload it came in. A 304 from KEV means the catalogue has not changed; it does not mean
there is nothing to do, because `cves` has grown since the last sync and those new CVEs have never
been checked against it. Re-downloading 1.7 MB of KEV and 2 MB of gzipped EPSS once every few hours
costs less than the class of bug where newly collected CVEs are never annotated.

**The raw bodies are cached.** `cache_raw` writes them content-addressed by sha256, the same
provenance trail Stage 1 keeps for collector payloads. A published severity or KEV badge is a claim
about the world, and the file it was derived from is the only thing that can show it was read rather
than invented.
"""

import json
import logging
from dataclasses import dataclass

import httpx

from worker.collectors.http import FetchResult, FetchStatus, cache_raw, fetch
from worker.groundtruth.epss import EPSS_SNAPSHOT_URL, EpssSnapshot, parse_epss_snapshot
from worker.groundtruth.errors import GroundTruthError
from worker.groundtruth.kev import KEV_CATALOGUE_URL, KevCatalogue, parse_kev_catalogue
from worker.models import Lane, SourceConfig

logger = logging.getLogger(__name__)

# One CVE record from the CVE Services API. This is the CNA's own record plus every ADP container
# attached to it — the CISA Vulnrichment enrichment arrives as one of those containers — which is
# exactly the shape `resolve_cvss` walks, and it is the authoritative rung of §2.5's chain rather
# than a mirror of it.
CVE_RECORD_URL = "https://cveawg.mitre.org/api/cve/{cve_id}"

# Generous, because these are the two largest downloads the worker makes and both are served from
# behind CDNs that occasionally take their time. A register that times out is retried on the next
# sync; one that is cut off halfway produces a short catalogue, which is the failure `is_complete`
# exists to notice.
BULK_TIMEOUT_SECONDS = 120.0

# A single CVE record is a few kilobytes, so anything slower than this is the register struggling
# rather than the payload being large, and the batch is better off moving on to the next CVE.
RECORD_TIMEOUT_SECONDS = 20.0

_REGISTER_IDS = {
    "kev": "register_cisa_kev",
    "epss": "register_epss",
    "cve_record": "register_cve_record",
}


def _register_source(register: str, url: str) -> SourceConfig:
    """A `SourceConfig` standing for a register, built only so `fetch` can be reused.

    `fetch` reads `source.url` and nothing else, but it is typed against `SourceConfig`, and the
    retry, size-cap, User-Agent and Accept-Encoding behaviour it carries — including the CISA
    Accept-Encoding workaround, which KEV needs because it is served from the same host — is worth
    far more than the awkwardness of this object.

    These never reach `source_registry`. A register is not a source: it produces no events, has no
    lifecycle state, and must not appear in `source-health.json`, where it would be counted among
    the feeds whose freshness the site reports. `type` and `parser` are both "register" so that a
    reader who finds one of these in a log cannot mistake it for a collector source — there is no
    parser by that name, and nothing dispatches on it.
    """
    return SourceConfig(
        id=_REGISTER_IDS[register],
        name=f"ground-truth register: {register}",
        type="register",
        region="global",
        category="advisory",
        source_class="AUTHORITATIVE",
        priority=1,
        lane=Lane.NORMAL,
        enabled=True,
        url=url,
        parser="register",
        expected_frequency="daily",
    )


async def _get(
    client: httpx.AsyncClient, register: str, url: str, *, timeout: float, retries: int = 3
) -> FetchResult:
    return await fetch(
        client,
        _register_source(register, url),
        timeout=timeout,
        max_retries=retries,
    )


def _body_or_raise(register: str, result: FetchResult) -> bytes:
    """The response body, or a `GroundTruthError` naming what went wrong instead.

    Raising rather than returning None is the register contract from `worker/groundtruth/__init__`:
    a download that failed says nothing at all about any CVE, so the caller must be forced to leave
    every answer `unknown` rather than being handed an empty catalogue it could mistake for "nothing
    is exploited".
    """
    if result.status is not FetchStatus.OK or result.body is None:
        raise GroundTruthError(
            f"{register} register unavailable: {result.status.value}"
            f"{f' ({result.error})' if result.error else ''}"
        )
    try:
        cache_raw(_REGISTER_IDS[register], result.body)
    except OSError as exc:
        # Not fatal, and deliberately louder than the equivalent in the collector: the register
        # still answered, and refusing the answer because we could not file the receipt would turn
        # a disk problem into a severity blackout. The warning is the signal that provenance for
        # these bytes is missing.
        logger.warning("raw cache write failed for the %s register: %s", register, exc)
    return result.body


async def fetch_kev(client: httpx.AsyncClient) -> KevCatalogue:
    """Download and parse the KEV catalogue.

    Raises
    ------
    GroundTruthError
        If the catalogue could not be downloaded or could not be parsed.
    """
    result = await _get(client, "kev", KEV_CATALOGUE_URL, timeout=BULK_TIMEOUT_SECONDS)
    catalogue = parse_kev_catalogue(_body_or_raise("kev", result))
    logger.info(
        "KEV catalogue %s released %s: %d entries, %d skipped, complete=%s",
        catalogue.version,
        catalogue.released,
        len(catalogue.entries),
        catalogue.skipped,
        catalogue.is_complete,
    )
    return catalogue


async def fetch_epss(client: httpx.AsyncClient) -> EpssSnapshot:
    """Download and parse today's EPSS snapshot.

    Raises
    ------
    GroundTruthError
        If the snapshot could not be downloaded or could not be parsed.
    """
    result = await _get(client, "epss", EPSS_SNAPSHOT_URL, timeout=BULK_TIMEOUT_SECONDS)
    snapshot = parse_epss_snapshot(_body_or_raise("epss", result))
    logger.info(
        "EPSS snapshot %s scored %s: %d scores, %d skipped",
        snapshot.model_version,
        snapshot.score_date,
        len(snapshot.scores),
        snapshot.skipped,
    )
    return snapshot


@dataclass(frozen=True)
class RecordLookup:
    """One CVE record lookup, separating "no such record" from "could not ask".

    Attributes
    ----------
    outcome : str
        'found', 'absent' (the register has no record under this id) or 'error'. These are three of
        the four values `cve_cvss_checks.outcome` accepts; the fourth, 'unscored', is not decided
        here because it depends on what `resolve_cvss` finds inside a record that was found.
    record : object | None
        The parsed JSON record when found. Passed straight to `resolve_cvss`, which does its own
        shape checking — so it is handed the payload rather than a type this module invents and
        would have to keep in step with a register it does not control.
    detail : str | None
        Why, for the outcomes that have a reason. Stored in `cve_cvss_checks.detail`.
    retry_after : bool
        True when the register asked us to back off (HTTP 429). The caller should stop the batch
        rather than convert the rest of it into several hundred recorded errors — each of which
        would claim a lookup was attempted and failed on its own merits.
    """

    outcome: str
    record: object | None = None
    detail: str | None = None
    retry_after: bool = False


async def fetch_cve_record(client: httpx.AsyncClient, cve_id: str) -> RecordLookup:
    """Look one CVE up in the per-record register.

    Unlike the bulk registers this does not raise. A failed lookup concerns one CVE, and the sync
    has somewhere honest to put it — `cve_cvss_checks` with outcome 'error', which `RECHECK_HOURS`
    retries soonest. Raising would abandon the rest of the batch over one bad id.

    A 404 is the one HTTP failure that is an *answer*: the register is reachable and has no record
    under that id, which happens for ids that were reserved and withdrawn. It gets outcome 'absent'
    and a longer recheck interval than an error, because nothing is likely to change.
    """
    result = await _get(
        client,
        "cve_record",
        CVE_RECORD_URL.format(cve_id=cve_id),
        timeout=RECORD_TIMEOUT_SECONDS,
    )

    if result.status is FetchStatus.OK and result.body is not None:
        try:
            cache_raw(_REGISTER_IDS["cve_record"], result.body)
        except OSError as exc:
            logger.warning("raw cache write failed for %s: %s", cve_id, exc)
        try:
            return RecordLookup("found", record=json.loads(result.body))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            return RecordLookup("error", detail=f"record is not valid JSON: {exc}")

    if result.status_code == 404:
        return RecordLookup("absent", detail="HTTP 404")

    return RecordLookup(
        "error",
        detail=result.error or result.status.value,
        retry_after=result.status_code == 429,
    )
