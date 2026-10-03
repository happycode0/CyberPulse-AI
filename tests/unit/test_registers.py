"""The network half of the ground-truth registers.

The parsers are covered exhaustively in test_groundtruth.py. What is tested here is everything the
parsers cannot see: that a failed download raises instead of producing an empty register, that a 404
from the per-record register is treated as an answer while a 503 is not, that no conditional-request
validators are sent, and that the raw bytes are filed as provenance.
"""

import gzip
import json

import httpx
import pytest

from worker.groundtruth.errors import GroundTruthError
from worker.groundtruth.registers import (
    CVE_RECORD_URL,
    _register_source,
    fetch_cve_record,
    fetch_epss,
    fetch_kev,
)
from worker.groundtruth.epss import EPSS_SNAPSHOT_URL
from worker.groundtruth.kev import KEV_CATALOGUE_URL

KEV_BODY = json.dumps(
    {
        "catalogVersion": "2026.09.30",
        "dateReleased": "2026-09-30T12:00:00.000Z",
        "count": 2,
        "vulnerabilities": [
            {"cveID": "CVE-2024-3400", "dateAdded": "2024-04-12", "dueDate": "2024-04-19"},
            {"cveID": "CVE-2023-1234", "dateAdded": "2023-02-01"},
        ],
    }
).encode()

EPSS_CSV = (
    b"#model_version:v2026.06.15,score_date:2026-09-30T12:00:21Z\n"
    b"cve,epss,percentile\n"
    b"CVE-2024-3400,0.94121,0.99912\n"
    b"CVE-2023-1234,0.00042,0.10000\n"
)

CVE_RECORD = {
    "cveMetadata": {"cveId": "CVE-2024-3400"},
    "containers": {
        "cna": {
            "metrics": [
                {
                    "cvssV3_1": {
                        "baseScore": 10.0,
                        "vectorString": "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:C/C:H/I:H/A:H",
                    }
                }
            ]
        }
    },
}


@pytest.fixture
def cached(monkeypatch):
    """Record what was filed as provenance instead of writing to the real cache directory."""
    calls: list[tuple[str, bytes]] = []
    monkeypatch.setattr(
        "worker.groundtruth.registers.cache_raw",
        lambda source_id, body: calls.append((source_id, body)),
    )
    return calls


@pytest.fixture
def client():
    return httpx.AsyncClient()


async def test_the_kev_catalogue_is_downloaded_and_parsed(respx_mock, client, cached):
    respx_mock.get(KEV_CATALOGUE_URL).respond(200, content=KEV_BODY)
    catalogue = await fetch_kev(client)
    assert catalogue.version == "2026.09.30"
    assert catalogue.lookup("CVE-2024-3400").listed is True
    assert catalogue.is_complete is True


async def test_the_epss_snapshot_is_downloaded_and_ungzipped(respx_mock, client, cached):
    respx_mock.get(EPSS_SNAPSHOT_URL).respond(200, content=gzip.compress(EPSS_CSV))
    snapshot = await fetch_epss(client)
    assert snapshot.model_version == "v2026.06.15"
    assert snapshot.lookup("CVE-2024-3400").score == pytest.approx(0.94121)


async def test_a_register_that_will_not_download_raises_instead_of_answering_nothing(
    respx_mock, client, cached
):
    # The whole point of GroundTruthError. An empty KEV catalogue would answer "not exploited" to
    # every lookup, which is the one wrong answer a failed download must not be allowed to produce.
    respx_mock.get(KEV_CATALOGUE_URL).respond(503)
    with pytest.raises(GroundTruthError, match="kev register unavailable"):
        await fetch_kev(client)
    assert cached == []


async def test_a_register_that_404s_also_raises(respx_mock, client, cached):
    # A bulk register has no "absent" outcome: if the catalogue URL is gone, we know nothing about
    # any CVE, which is a different thing from knowing that one CVE is unlisted.
    respx_mock.get(EPSS_SNAPSHOT_URL).respond(404)
    with pytest.raises(GroundTruthError, match="epss register unavailable"):
        await fetch_epss(client)


async def test_the_raw_bytes_are_filed_as_provenance(respx_mock, client, cached):
    respx_mock.get(KEV_CATALOGUE_URL).respond(200, content=KEV_BODY)
    await fetch_kev(client)
    assert cached == [("register_cisa_kev", KEV_BODY)]


async def test_a_cache_write_failure_does_not_discard_the_answer(respx_mock, client, monkeypatch):
    # A full disk must not become a severity blackout: the register answered, and the warning is the
    # signal that the receipt for these bytes is missing.
    def boom(source_id, body):
        raise OSError("read-only file system")

    monkeypatch.setattr("worker.groundtruth.registers.cache_raw", boom)
    respx_mock.get(KEV_CATALOGUE_URL).respond(200, content=KEV_BODY)
    assert (await fetch_kev(client)).lookup("CVE-2024-3400").listed is True


async def test_no_conditional_validators_are_sent(respx_mock, client, cached):
    """A 304 would mean "the catalogue has not changed", not "there is nothing to do".

    `cves` grows between syncs, and those new CVEs have never been checked against the catalogue. A
    register is applied to a moving set of CVEs rather than to the payload it arrived in, so saving
    the download would quietly leave every newly collected CVE unannotated.
    """
    route = respx_mock.get(KEV_CATALOGUE_URL).respond(200, content=KEV_BODY)
    await fetch_kev(client)
    headers = route.calls.last.request.headers
    assert "if-none-match" not in headers
    assert "if-modified-since" not in headers


async def test_a_record_the_register_does_not_have_is_absent_not_an_error(
    respx_mock, client, cached
):
    respx_mock.get(CVE_RECORD_URL.format(cve_id="CVE-2024-0001")).respond(404)
    lookup = await fetch_cve_record(client, "CVE-2024-0001")
    assert (lookup.outcome, lookup.retry_after) == ("absent", False)


async def test_an_unreachable_register_is_an_error_not_an_absence(respx_mock, client, cached):
    # The distinction that `FetchResult.status_code` exists for. 'absent' earns a 72-hour recheck
    # because nothing is likely to change; a register that was down earns a 6-hour one, because the
    # CVE may well have had a score all along.
    respx_mock.get(CVE_RECORD_URL.format(cve_id="CVE-2024-0002")).respond(503)
    lookup = await fetch_cve_record(client, "CVE-2024-0002")
    assert lookup.outcome == "error"


async def test_being_throttled_asks_the_caller_to_stop(respx_mock, client, cached):
    respx_mock.get(CVE_RECORD_URL.format(cve_id="CVE-2024-0003")).respond(429)
    lookup = await fetch_cve_record(client, "CVE-2024-0003")
    assert (lookup.outcome, lookup.retry_after) == ("error", True)


async def test_a_found_record_is_handed_back_parsed(respx_mock, client, cached):
    respx_mock.get(CVE_RECORD_URL.format(cve_id="CVE-2024-3400")).respond(200, json=CVE_RECORD)
    lookup = await fetch_cve_record(client, "CVE-2024-3400")
    assert lookup.outcome == "found"
    assert lookup.record == CVE_RECORD


async def test_a_record_that_is_not_json_is_an_error_not_a_crash(respx_mock, client, cached):
    respx_mock.get(CVE_RECORD_URL.format(cve_id="CVE-2024-0004")).respond(
        200, content=b"<html>maintenance</html>"
    )
    lookup = await fetch_cve_record(client, "CVE-2024-0004")
    assert lookup.outcome == "error" and "not valid JSON" in lookup.detail


async def test_a_register_cannot_be_mistaken_for_a_collector_source():
    """Registers produce no events and must never reach `source_registry`.

    `type` and `parser` are both "register" — names nothing dispatches on — so a reader who finds one
    of these ids in a log cannot take it for a feed whose freshness the site reports.
    """
    source = _register_source("kev", KEV_CATALOGUE_URL)
    assert source.type == "register" and source.parser == "register"
    assert source.id.startswith("register_")
