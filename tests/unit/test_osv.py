"""OSV and GitHub advisories: the parser, and the CVE-then-GHSA lookup in front of it."""

import json

import httpx
import pytest

from worker.groundtruth.errors import GroundTruthError
from worker.groundtruth.osv import (
    MAX_GHSA_PER_CVE,
    OSV_RECORD_URL,
    ghsa_aliases,
    is_ghsa,
    names_cve,
    parse_osv_record,
)
from worker.groundtruth.registers import fetch_advisories

CVE = "CVE-2021-44228"
GHSA = "GHSA-jfh8-c2jp-5v3q"

# OSV's own record of a CVE converted from NVD: a GIT range and no package, which a reader
# cannot install, so it is not recorded as an advisory.
CVE_RECORD = {
    "id": CVE,
    "aliases": [GHSA],
    "related": ["SUSE-SU-2021:4096-1"],
    "summary": "Remote code execution in Log4j",
    "modified": "2026-09-01T10:00:00.123456789Z",
    "published": "2021-12-10T10:15:09Z",
    "affected": [
        {"ranges": [{"type": "GIT", "events": [{"introduced": "6b788fac"}, {"fixed": "38513a7d"}]}]}
    ],
}

GHSA_RECORD = {
    "id": GHSA,
    "aliases": [CVE],
    "summary": "Remote code   injection in Log4j",
    "published": "2021-12-10T00:40:56Z",
    "modified": "2025-10-22T19:33:21.811512Z",
    "database_specific": {"severity": "CRITICAL", "github_reviewed": True},
    "affected": [
        {
            "package": {"ecosystem": "Maven", "name": "org.apache.logging.log4j:log4j-core"},
            "ranges": [
                {
                    "type": "ECOSYSTEM",
                    "events": [{"introduced": "2.13.0"}, {"fixed": "2.15.0"}],
                },
            ],
        },
        {
            "package": {"ecosystem": "Maven", "name": "org.apache.logging.log4j:log4j-core"},
            "ranges": [
                {
                    "type": "ECOSYSTEM",
                    "events": [{"introduced": "2.4"}, {"fixed": "2.12.2"}, {"fixed": "2.15.0"}],
                },
            ],
        },
        {"package": {"ecosystem": "Maven", "name": "org.xbib.elasticsearch:log4j"}},
    ],
}


def body(record: dict) -> bytes:
    return json.dumps(record).encode()


# --- the parser --------------------------------------------------------------------------------


def test_a_github_advisory_keeps_its_packages_fixes_and_reviewed_rating():
    advisory = parse_osv_record(body(GHSA_RECORD))
    assert (advisory.id, advisory.source) == (GHSA, "ghsa")
    assert (advisory.severity, advisory.reviewed) == ("critical", True)
    core, xbib = advisory.packages
    # The two ranges for one package merge, and a repeated fixed version is listed once.
    assert core.as_json() == {
        "ecosystem": "Maven",
        "name": "org.apache.logging.log4j:log4j-core",
        "fixed": ["2.15.0", "2.12.2"],
    }
    # No range at all: affected with no fix published, which is not "unaffected".
    assert xbib.fixed == ()
    assert advisory.has_fix is True
    assert advisory.url == f"https://github.com/advisories/{GHSA}"
    assert advisory.summary == "Remote code injection in Log4j"


def test_moderate_is_published_as_medium():
    record = {**GHSA_RECORD, "database_specific": {"severity": "MODERATE"}}
    assert parse_osv_record(body(record)).severity == "medium"


def test_osvs_own_record_has_no_github_rating_and_no_installable_package():
    record = parse_osv_record(body(CVE_RECORD))
    assert (record.source, record.severity, record.reviewed) == ("osv", None, False)
    assert record.packages == ()
    assert record.url == f"https://osv.dev/vulnerability/{CVE}"


def test_nanosecond_timestamps_are_read():
    record = parse_osv_record(body(CVE_RECORD))
    assert record.modified is not None and record.modified.microsecond == 123456
    assert record.modified.tzinfo is not None


def test_a_withdrawn_advisory_says_so():
    assert parse_osv_record(body({**GHSA_RECORD, "withdrawn": "2026-01-01T00:00:00Z"})).withdrawn


@pytest.mark.parametrize("raw", [b"<html>", b"[]", b'{"summary": "no id"}'])
def test_a_record_that_is_not_one_raises(raw):
    with pytest.raises(GroundTruthError):
        parse_osv_record(raw)


def test_ghsa_ids_are_recognised_by_githubs_alphabet():
    assert is_ghsa(GHSA)
    assert not is_ghsa("GHSA-aaaa-bbbb-cccc")  # a, b and the vowels are not in it
    assert not is_ghsa(CVE)


def test_only_github_aliases_are_followed_and_only_a_few():
    many = [f"GHSA-{n}{n}{n}{n}-2222-3333" for n in "23456"]
    record = parse_osv_record(body({**CVE_RECORD, "aliases": ["PYSEC-2021-1", *many]}))
    assert ghsa_aliases(record) == tuple(many[:MAX_GHSA_PER_CVE])


def test_an_advisory_must_name_the_cve_back():
    advisory = parse_osv_record(body(GHSA_RECORD))
    assert names_cve(advisory, CVE)
    assert not names_cve(advisory, "CVE-2021-45046")


# --- the lookup --------------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def no_cache(monkeypatch):
    monkeypatch.setattr("worker.groundtruth.registers.cache_raw", lambda source_id, body: None)


@pytest.fixture
def client():
    return httpx.AsyncClient()


def url(advisory_id: str) -> str:
    return OSV_RECORD_URL.format(id=advisory_id)


async def test_a_cve_is_answered_with_the_github_advisories_its_record_names(respx_mock, client):
    respx_mock.get(url(CVE)).respond(200, json=CVE_RECORD)
    respx_mock.get(url(GHSA)).respond(200, json=GHSA_RECORD)
    lookup = await fetch_advisories(client, CVE)
    assert (lookup.outcome, lookup.complete) == ("found", True)
    assert [a.id for a in lookup.advisories] == [GHSA]


async def test_a_cve_osv_has_never_heard_of_is_absent(respx_mock, client):
    respx_mock.get(url(CVE)).respond(404)
    lookup = await fetch_advisories(client, CVE)
    assert (lookup.outcome, lookup.advisories, lookup.complete) == ("absent", (), False)


async def test_an_advisory_that_does_not_name_the_cve_back_is_left_out(respx_mock, client):
    respx_mock.get(url(CVE)).respond(200, json=CVE_RECORD)
    respx_mock.get(url(GHSA)).respond(200, json={**GHSA_RECORD, "aliases": ["CVE-2099-0001"]})
    lookup = await fetch_advisories(client, CVE)
    assert (lookup.outcome, lookup.advisories) == ("found", ())


async def test_a_withdrawn_advisory_is_left_out(respx_mock, client):
    respx_mock.get(url(CVE)).respond(200, json=CVE_RECORD)
    respx_mock.get(url(GHSA)).respond(200, json={**GHSA_RECORD, "withdrawn": "2026-01-01"})
    assert (await fetch_advisories(client, CVE)).advisories == ()


async def test_an_unreadable_advisory_makes_the_answer_partial(respx_mock, client):
    # Partial answers are written but cannot remove anything, and the CVE is asked again soon.
    respx_mock.get(url(CVE)).respond(200, json=CVE_RECORD)
    respx_mock.get(url(GHSA)).respond(503)
    lookup = await fetch_advisories(client, CVE)
    assert (lookup.outcome, lookup.complete) == ("error", False)
    assert GHSA in lookup.detail


async def test_osvs_own_record_is_kept_when_it_names_a_package(respx_mock, client):
    record = {**CVE_RECORD, "aliases": [], "affected": GHSA_RECORD["affected"][:1]}
    respx_mock.get(url(CVE)).respond(200, json=record)
    lookup = await fetch_advisories(client, CVE)
    assert [(a.id, a.source) for a in lookup.advisories] == [(CVE, "osv")]


@pytest.mark.parametrize("throttled", [CVE, GHSA])
async def test_being_throttled_at_any_request_returns_nothing_to_record(
    respx_mock, client, throttled
):
    respx_mock.get(url(CVE)).respond(*((429,) if throttled == CVE else (200,)), json=CVE_RECORD)
    respx_mock.get(url(GHSA)).respond(429)
    assert await fetch_advisories(client, CVE) is None


async def test_a_record_for_another_id_is_an_error(respx_mock, client):
    respx_mock.get(url(CVE)).respond(
        200, json={**CVE_RECORD, "id": "CVE-2021-45046", "aliases": []}
    )
    lookup = await fetch_advisories(client, CVE)
    assert lookup.outcome == "error" and lookup.advisories == ()
