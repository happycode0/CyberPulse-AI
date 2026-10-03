"""Downloading the registers. The parsing lives next door; this is only the network half.

Two shapes. KEV and EPSS are bulk: one download answers for every CVE at once, and so do the MITRE
catalogues, which are downloaded only when a new release appears. The CVSS chain of PLAN.md §2.5
and the OSV advisories are per-record, one request per CVE, and those are the ones rationed.

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

import asyncio
import json
import logging
from dataclasses import dataclass

import httpx

from worker.collectors.http import FetchResult, FetchStatus, cache_raw, fetch
from worker.groundtruth.epss import EPSS_SNAPSHOT_URL, EpssSnapshot, parse_epss_snapshot
from worker.groundtruth.errors import GroundTruthError
from worker.groundtruth.kev import KEV_CATALOGUE_URL, KevCatalogue, parse_kev_catalogue
from worker.groundtruth.mitre import (
    ATLAS_MANIFEST_URL,
    ATTACK_INDEX_URL,
    Catalogue,
    Release,
    latest_atlas_release,
    latest_attack_release,
    parse_atlas,
    parse_attack_bundle,
)
from worker.groundtruth.osv import (
    OSV_RECORD_URL,
    Advisory,
    ghsa_aliases,
    names_cve,
    parse_osv_record,
)
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

# The Enterprise ATT&CK bundle is 54 MB, well over the collectors' 32 MiB cap.
MITRE_MAX_BYTES = 128 * 1024 * 1024

_REGISTER_IDS = {
    "kev": "register_cisa_kev",
    "epss": "register_epss",
    "cve_record": "register_cve_record",
    "attack": "register_mitre_attack",
    "atlas": "register_mitre_atlas",
    "osv": "register_osv",
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
    client: httpx.AsyncClient,
    register: str,
    url: str,
    *,
    timeout: float,
    retries: int = 3,
    max_bytes: int = 32 * 1024 * 1024,
) -> FetchResult:
    return await fetch(
        client,
        _register_source(register, url),
        timeout=timeout,
        max_retries=retries,
        max_size_bytes=max_bytes,
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


# ─── MITRE ────────────────────────────────────────────────────────────────────────────────────────


async def fetch_mitre_release(client: httpx.AsyncClient, matrix: str) -> Release:
    """The newest published release of 'enterprise' or 'atlas'. The index and the manifest are
    small, so this is cheap enough to ask on every sync.

    Raises
    ------
    GroundTruthError
        If the index or manifest could not be downloaded or read.
    """
    register, url = (
        ("attack", ATTACK_INDEX_URL)
        if matrix == "enterprise"
        else (
            "atlas",
            ATLAS_MANIFEST_URL,
        )
    )
    result = await _get(client, register, url, timeout=RECORD_TIMEOUT_SECONDS)
    if result.status is not FetchStatus.OK or result.body is None:
        raise GroundTruthError(
            f"{register} index unavailable: {result.status.value}"
            f"{f' ({result.error})' if result.error else ''}"
        )
    body = result.body
    return latest_attack_release(body) if matrix == "enterprise" else latest_atlas_release(body)


async def fetch_mitre_catalogue(client: httpx.AsyncClient, release: Release) -> Catalogue:
    """Download and parse one release. Only called for a release not loaded yet.

    Raises
    ------
    GroundTruthError
        If the release could not be downloaded or did not parse as complete.
    """
    register = "attack" if release.matrix == "enterprise" else "atlas"
    result = await _get(
        client, register, release.url, timeout=BULK_TIMEOUT_SECONDS, max_bytes=MITRE_MAX_BYTES
    )
    body = _body_or_raise(register, result)
    if len(body) >= MITRE_MAX_BYTES:
        raise GroundTruthError(f"{release.version} is larger than {MITRE_MAX_BYTES} bytes")
    parse = parse_attack_bundle if release.matrix == "enterprise" else parse_atlas
    # Off the event loop: parsing the ATT&CK bundle takes seconds, and the lanes share the loop.
    catalogue = await asyncio.to_thread(parse, body, release)
    logger.info("%s: %d techniques", release.version, len(catalogue.techniques))
    return catalogue


# ─── OSV ──────────────────────────────────────────────────────────────────────────────────────────


async def fetch_osv_record(client: httpx.AsyncClient, advisory_id: str) -> RecordLookup:
    """One OSV record by id, a CVE or a GHSA. Like `fetch_cve_record`, it never raises: a 404
    is the answer "no record", and anything else is an error for that one id."""
    result = await _get(
        client,
        "osv",
        OSV_RECORD_URL.format(id=advisory_id),
        timeout=RECORD_TIMEOUT_SECONDS,
    )
    if result.status is FetchStatus.OK and result.body is not None:
        try:
            cache_raw(_REGISTER_IDS["osv"], result.body)
        except OSError as exc:
            logger.warning("raw cache write failed for OSV %s: %s", advisory_id, exc)
        return RecordLookup("found", record=result.body)
    if result.status_code == 404:
        return RecordLookup("absent", detail="HTTP 404")
    return RecordLookup(
        "error",
        detail=result.error or result.status.value,
        retry_after=result.status_code == 429,
    )


@dataclass(frozen=True)
class AdvisoryLookup:
    """What OSV says about one CVE: its own record of it, and the GitHub advisories it names.

    Attributes
    ----------
    outcome : str
        'found', 'absent' or 'error', the values `cve_advisory_checks.outcome` accepts. 'error'
        also covers a found record whose GitHub advisories could not all be read, so the CVE is
        asked about again soon rather than in three days.
    advisories : tuple[Advisory, ...]
        The advisories to record: OSV's own record only when it names a package, then each
        GitHub advisory that names the CVE back.
    complete : bool
        Whether every advisory the record names was read. Only a complete answer may remove an
        advisory recorded earlier.
    """

    outcome: str
    advisories: tuple[Advisory, ...] = ()
    detail: str | None = None
    complete: bool = False


async def fetch_advisories(client: httpx.AsyncClient, cve_id: str) -> AdvisoryLookup | None:
    """Look one CVE up in OSV, then each GitHub advisory its record names. Never raises.

    Returns None when OSV asked us to back off (HTTP 429), at any of the requests: the CVE is
    then not recorded at all, as in the CVSS batch.
    """
    first = await fetch_osv_record(client, cve_id)
    if first.retry_after:
        return None
    if first.outcome != "found":
        return AdvisoryLookup(first.outcome, detail=first.detail)
    try:
        record = parse_osv_record(first.record)  # type: ignore[arg-type]
    except GroundTruthError as exc:
        return AdvisoryLookup("error", detail=str(exc))
    if not names_cve(record, cve_id):
        return AdvisoryLookup("error", detail=f"OSV answered with {record.id}")

    found: list[Advisory] = [record] if record.packages and not record.withdrawn else []
    unread: list[str] = []
    for ghsa in ghsa_aliases(record):
        lookup = await fetch_osv_record(client, ghsa)
        if lookup.retry_after:
            return None
        if lookup.outcome == "absent":
            continue
        if lookup.outcome != "found":
            unread.append(ghsa)
            continue
        try:
            advisory = parse_osv_record(lookup.record)  # type: ignore[arg-type]
        except GroundTruthError:
            unread.append(ghsa)
            continue
        # The advisory must name the CVE back: an alias group that has drifted is not evidence.
        if advisory.id == ghsa and not advisory.withdrawn and names_cve(advisory, cve_id):
            found.append(advisory)
    if unread:
        return AdvisoryLookup(
            "error", tuple(found), detail=f"could not read {', '.join(unread)}", complete=False
        )
    return AdvisoryLookup("found", tuple(found), detail=f"{len(found)} advisories", complete=True)
