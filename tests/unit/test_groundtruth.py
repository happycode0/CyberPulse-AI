"""The ground-truth registers: KEV, EPSS and the CVSS chain.

Every fixture here is cut down from a real payload fetched while writing the modules, so the
shapes (a `metrics` list of version-keyed dicts, a `#model_version:...` comment line ahead of the
CSV header, `adp` as a list) are the live ones rather than what the docs imply.

The theme running through the file is the honest-data rule from PLAN.md §2: an absent fact is
`unknown`. Several of these tests exist only to pin down the difference between "we looked and the
answer is no" and "we could not look", because every one of those distinctions has a plausible
implementation that collapses them into a zero.
"""

import gzip
import json
from datetime import date

import pytest

from worker.groundtruth import (
    GroundTruthError,
    parse_epss_snapshot,
    parse_kev_catalogue,
    resolve_cvss,
)
from worker.groundtruth.ids import normalise_cve_id
from worker.groundtruth.severity import severity_for_score, severity_from_cvss
from worker.models import CveRef, CvssScore, Severity, SeveritySource

# --------------------------------------------------------------------------------------------
# Fixtures, shaped after the live payloads
# --------------------------------------------------------------------------------------------

KEV_PAYLOAD = {
    "title": "CISA Catalog of Known Exploited Vulnerabilities",
    "catalogVersion": "2026.09.30",
    "dateReleased": "2026-09-30T16:59:23.0688Z",
    "count": 3,
    "vulnerabilities": [
        {
            "cveID": "CVE-2024-3400",
            "vendorProject": "Palo Alto Networks",
            "product": "PAN-OS",
            "vulnerabilityName": "Command Injection Vulnerability",
            "dateAdded": "2024-04-12",
            "shortDescription": "Palo Alto Networks PAN-OS contains a command injection flaw.",
            "requiredAction": "Apply mitigations per vendor instructions.",
            "dueDate": "2024-04-19",
            "knownRansomwareCampaignUse": "Known",
            "notes": "https://security.paloaltonetworks.com/CVE-2024-3400",
            "cwes": ["CWE-77"],
        },
        {
            "cveID": "CVE-2021-44228",
            "vendorProject": "Apache",
            "product": "Log4j2",
            "vulnerabilityName": "Remote Code Execution Vulnerability",
            "dateAdded": "2021-12-10",
            "shortDescription": "Apache Log4j2 contains a JNDI injection flaw.",
            "requiredAction": "Apply updates per vendor instructions.",
            "dueDate": "2021-12-24",
            "knownRansomwareCampaignUse": "Known",
            "notes": "",
            "cwes": ["CWE-917"],
        },
        {
            "cveID": "CVE-2023-4966",
            "vendorProject": "Citrix",
            "product": "NetScaler ADC",
            "vulnerabilityName": "Buffer Overflow Vulnerability",
            # A date CISA has mistyped. The entry must survive it: the CVE is still listed.
            "dateAdded": "not a date",
            "shortDescription": "Citrix NetScaler contains a sensitive information disclosure.",
            "requiredAction": "Apply updates per vendor instructions.",
            "dueDate": "",
            "knownRansomwareCampaignUse": "Known",
            "notes": "",
            "cwes": ["CWE-119"],
        },
    ],
}

EPSS_CSV = (
    "#model_version:v2026.06.15,score_date:2026-09-30T12:00:21Z\n"
    "cve,epss,percentile\n"
    "CVE-1999-0001,0.03351,0.88271\n"
    "CVE-2024-3400,0.94270,0.99912\n"
    "CVE-2021-44228,0.00000,0.00100\n"
)


def _cve_record(cna_metrics=None, adp=None) -> dict:
    """A cvelistV5 record with only the parts the resolver reads."""
    record: dict = {
        "dataType": "CVE_RECORD",
        "dataVersion": "5.1",
        "cveMetadata": {"cveId": "CVE-2024-3400", "state": "PUBLISHED"},
        "containers": {},
    }
    if cna_metrics is not None:
        record["containers"]["cna"] = {"title": "Command Injection", "metrics": cna_metrics}
    if adp is not None:
        record["containers"]["adp"] = adp
    return record


def _metric(key: str, score, vector: str | None = "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:C/C:H/I:H/A:H") -> dict:
    block: dict = {"version": key[-3:].replace("_", "."), "baseScore": score}
    if vector is not None:
        block["vectorString"] = vector
    # "format" and "scenarios" sit alongside the version key in the real payload; they are here so
    # the resolver is tested against a dict it has to pick a key out of, not a single-key one.
    return {key: block, "format": "CVSS", "scenarios": [{"lang": "en", "value": "GENERAL"}]}


def _adp(short_name: str, metrics=None) -> dict:
    entry: dict = {"providerMetadata": {"shortName": short_name, "orgId": "0000"}}
    if metrics is not None:
        entry["metrics"] = metrics
    return entry


# --------------------------------------------------------------------------------------------
# KEV
# --------------------------------------------------------------------------------------------


def test_kev_catalogue_parses_the_live_payload_shape():
    cat = parse_kev_catalogue(json.dumps(KEV_PAYLOAD).encode())
    assert cat.version == "2026.09.30"
    assert cat.declared_count == 3
    assert len(cat.entries) == 3
    assert cat.skipped == 0
    # dateReleased carries a four-digit fraction and a Z, which is the combination that defeats a
    # naive strptime; parse_date has to come back with a real UTC timestamp.
    assert cat.released is not None
    assert (cat.released.year, cat.released.month, cat.released.day) == (2026, 9, 30)
    assert cat.released.utcoffset().total_seconds() == 0


def test_kev_entry_carries_the_listing_dates():
    cat = parse_kev_catalogue(json.dumps(KEV_PAYLOAD).encode())
    entry = cat.lookup("CVE-2024-3400")
    assert entry.listed is True
    assert entry.date_added == date(2024, 4, 12)
    assert entry.due_date == date(2024, 4, 19)


def test_a_kev_entry_with_an_unparseable_date_is_still_listed():
    """Losing the entry would trade the strong claim for the weak one.

    CVE-2023-4966's dateAdded is deliberately garbage in the fixture. Dropping the row would make
    an actively exploited CVE report as not exploited; keeping it with date_added=None says only
    that the date is unknown, which is the part that is actually unknown.
    """
    cat = parse_kev_catalogue(json.dumps(KEV_PAYLOAD).encode())
    entry = cat.lookup("CVE-2023-4966")
    assert entry.listed is True
    assert entry.date_added is None
    assert entry.due_date is None


def test_a_cve_absent_from_the_catalogue_is_reported_as_not_listed():
    """A miss in KEV is knowledge, because the whole catalogue was read."""
    cat = parse_kev_catalogue(json.dumps(KEV_PAYLOAD).encode())
    entry = cat.lookup("CVE-2015-0001")
    assert entry.listed is False
    assert entry.date_added is None


def test_kev_lookup_is_case_and_whitespace_insensitive():
    cat = parse_kev_catalogue(json.dumps(KEV_PAYLOAD).encode())
    assert cat.lookup("  cve-2024-3400 ").listed is True


def test_kev_lookup_of_a_non_cve_string_does_not_raise():
    cat = parse_kev_catalogue(json.dumps(KEV_PAYLOAD).encode())
    assert cat.lookup("not-a-cve").listed is False


def test_kev_lookup_returns_a_fresh_entry_each_time():
    """A shared default would let one caller's mutation become every later caller's answer."""
    cat = parse_kev_catalogue(json.dumps(KEV_PAYLOAD).encode())
    first = cat.lookup("CVE-2015-0001")
    first.listed = True
    assert cat.lookup("CVE-2015-0001").listed is False


def test_kev_entries_without_a_usable_id_are_skipped_and_counted():
    payload = {
        "catalogVersion": "2026.09.30",
        "count": 4,
        "vulnerabilities": [
            KEV_PAYLOAD["vulnerabilities"][0],
            {"vendorProject": "Acme", "product": "Thing"},  # no cveID at all
            {"cveID": "GHSA-xxxx-yyyy-zzzz"},  # a real id, but not a CVE id
            "not even an object",
        ],
    }
    cat = parse_kev_catalogue(json.dumps(payload).encode())
    assert set(cat.entries) == {"CVE-2024-3400"}
    assert cat.skipped == 3


def test_a_truncated_kev_catalogue_is_detectable():
    """Valid JSON with a short array is the failure that otherwise passes for a healthy register."""
    full = parse_kev_catalogue(json.dumps(KEV_PAYLOAD).encode())
    assert full.is_complete is True

    truncated = dict(KEV_PAYLOAD, count=1730)
    assert parse_kev_catalogue(json.dumps(truncated).encode()).is_complete is False


def test_a_kev_catalogue_without_a_count_is_not_called_incomplete():
    payload = {k: v for k, v in KEV_PAYLOAD.items() if k != "count"}
    cat = parse_kev_catalogue(json.dumps(payload).encode())
    assert cat.declared_count is None
    assert cat.is_complete is True


@pytest.mark.parametrize(
    "body",
    [
        b"",
        b"not json at all",
        b"[]",
        b'{"catalogVersion": "2026.09.30"}',
        b'{"vulnerabilities": "not a list"}',
        b'{"vulnerabilities": []}',
        b'{"vulnerabilities": [{"vendorProject": "no id here"}]}',
    ],
    ids=["empty", "garbage", "array", "no-array", "wrong-type", "zero-entries", "all-unusable"],
)
def test_an_unusable_kev_payload_raises_rather_than_reporting_nothing_exploited(body):
    """This is the whole reason the registers raise instead of returning an empty dict.

    An empty catalogue and an unread one are indistinguishable to a caller holding only a dict,
    and the consequence is not cosmetic: every lookup would answer listed=False, quietly marking
    ~1,730 actively exploited CVEs as not exploited. PLAN.md §2 forbids exactly that trade.
    """
    with pytest.raises(GroundTruthError):
        parse_kev_catalogue(body)


# --------------------------------------------------------------------------------------------
# EPSS
# --------------------------------------------------------------------------------------------


def test_epss_snapshot_parses_a_gzipped_body():
    snap = parse_epss_snapshot(gzip.compress(EPSS_CSV.encode()))
    assert snap.model_version == "v2026.06.15"
    assert len(snap.scores) == 3
    assert snap.lookup("CVE-2024-3400").score == pytest.approx(0.94270)


def test_epss_snapshot_parses_an_already_decompressed_body():
    """The .csv.gz is a payload layer, so whether it survives transport is not ours to predict.

    httpx decodes Content-Encoding transparently, and worker/collectors/http.py asks for gzip
    explicitly, so the body can arrive either way. Sniffing the magic number has to handle both.
    """
    snap = parse_epss_snapshot(EPSS_CSV.encode())
    assert len(snap.scores) == 3
    assert snap.lookup("CVE-2024-3400").score == pytest.approx(0.94270)


def test_epss_metadata_survives_the_colons_in_the_timestamp():
    """Splitting "score_date:2026-09-30T12:00:21Z" on every colon loses the time."""
    snap = parse_epss_snapshot(EPSS_CSV.encode())
    assert snap.score_date is not None
    assert (snap.score_date.hour, snap.score_date.minute, snap.score_date.second) == (12, 0, 21)


def test_epss_header_row_is_not_parsed_as_a_score():
    snap = parse_epss_snapshot(EPSS_CSV.encode())
    assert "CVE" not in snap.scores
    assert all(key.startswith("CVE-") for key in snap.scores)


def test_a_scored_epss_row_is_marked_known():
    snap = parse_epss_snapshot(EPSS_CSV.encode())
    scored = snap.lookup("CVE-1999-0001")
    assert scored.status == "known"
    assert scored.score == pytest.approx(0.03351)


def test_a_genuine_epss_zero_is_kept_as_zero_and_not_as_unknown():
    """0.00000 is a published probability; only an absent row is unknown."""
    snap = parse_epss_snapshot(EPSS_CSV.encode())
    scored = snap.lookup("CVE-2021-44228")
    assert scored.score == 0.0
    assert scored.status == "known"


def test_a_cve_epss_has_not_scored_is_unknown_not_zero():
    """The distinction EPSS cannot make and KEV can.

    EPSS models a subset of CVEs, so a miss means unscored — not safe, and not a probability of
    zero. The migration comment in 001_initial.sql requires exactly this shape: "an epss row with
    status 'unknown' and NULL score; an absent score is never stored as zero."
    """
    snap = parse_epss_snapshot(EPSS_CSV.encode())
    missing = snap.lookup("CVE-2015-0001")
    assert missing.score is None
    assert missing.status == "unknown"


def test_epss_rows_out_of_range_are_skipped_rather_than_losing_the_snapshot():
    """EpssScore is constrained to [0, 1], so one bad row must not raise mid-parse."""
    csv_text = (
        "#model_version:v1,score_date:2026-09-30T12:00:21Z\n"
        "cve,epss,percentile\n"
        "CVE-2024-3400,0.5,0.9\n"
        "CVE-2024-0002,1.5,0.9\n"
        "CVE-2024-0003,-0.2,0.9\n"
        "CVE-2024-0004,nan,0.9\n"
        "CVE-2024-0005,not-a-number,0.9\n"
        "GHSA-xxxx-yyyy-zzzz,0.4,0.9\n"
        "CVE-2024-0007\n"
    )
    snap = parse_epss_snapshot(csv_text.encode())
    assert set(snap.scores) == {"CVE-2024-3400"}
    assert snap.skipped == 6


def test_epss_lookup_returns_a_fresh_score_each_time():
    snap = parse_epss_snapshot(EPSS_CSV.encode())
    first = snap.lookup("CVE-2015-0001")
    first.status = "known"
    assert snap.lookup("CVE-2015-0001").status == "unknown"


@pytest.mark.parametrize(
    "body",
    [
        b"",
        b"#model_version:v1,score_date:2026-09-30T12:00:21Z\ncve,epss,percentile\n",
        b"cve,epss,percentile\nGHSA-xxxx-yyyy-zzzz,0.4,0.9\n",
        b"\x1f\x8b" + b"truncated gzip",
    ],
    ids=["empty", "header-only", "no-usable-rows", "broken-gzip"],
)
def test_an_unusable_epss_payload_raises_rather_than_scoring_everything_zero(body):
    with pytest.raises(GroundTruthError):
        parse_epss_snapshot(body)


# --------------------------------------------------------------------------------------------
# The CVSS chain
# --------------------------------------------------------------------------------------------


def test_the_cna_container_is_preferred_over_cisa_adp():
    record = _cve_record(
        cna_metrics=[_metric("cvssV3_1", 10.0)],
        adp=[_adp("CISA-ADP", [_metric("cvssV3_1", 7.5)])],
    )
    score = resolve_cvss(record)
    assert score is not None
    assert score.score == 10.0
    assert score.source == SeveritySource.CNA.value


def test_cisa_adp_is_used_when_the_cna_has_no_score():
    """The rung that makes the chain worth having: 223 of 300 sampled CVEs had no NVD score."""
    record = _cve_record(
        cna_metrics=[],
        adp=[_adp("CISA-ADP", [_metric("cvssV3_1", 7.5)])],
    )
    score = resolve_cvss(record)
    assert score is not None
    assert score.score == 7.5
    assert score.source == SeveritySource.CISA_ADP.value


def test_nvd_is_the_last_rung_not_the_first():
    record = _cve_record(
        adp=[_adp("nvd", [_metric("cvssV3_1", 5.0)]), _adp("CISA-ADP", [_metric("cvssV3_1", 9.1)])],
    )
    score = resolve_cvss(record)
    assert score is not None
    assert score.source == SeveritySource.CISA_ADP.value
    assert score.score == 9.1


def test_the_chain_order_does_not_depend_on_the_order_of_the_adp_array():
    """`containers.adp` is a list, and its order is the record's business, not the chain's.

    In the record sampled while writing this, CISA-ADP happened to be adp[0]. Iterating the list
    and taking the first match would therefore have looked correct and silently let an NVD score
    outrank the CISA enrichment PLAN.md §2.5 ranks above it.
    """
    nvd_first = resolve_cvss(
        _cve_record(adp=[_adp("nvd", [_metric("cvssV3_1", 5.0)]), _adp("CISA-ADP", [_metric("cvssV3_1", 9.1)])])
    )
    cisa_first = resolve_cvss(
        _cve_record(adp=[_adp("CISA-ADP", [_metric("cvssV3_1", 9.1)]), _adp("nvd", [_metric("cvssV3_1", 5.0)])])
    )
    assert nvd_first is not None and cisa_first is not None
    assert nvd_first.source == cisa_first.source == SeveritySource.CISA_ADP.value
    assert nvd_first.score == cisa_first.score == 9.1


def test_nvd_is_used_when_it_is_the_only_container_with_a_score():
    record = _cve_record(cna_metrics=[], adp=[_adp("nvd", [_metric("cvssV3_1", 5.0)])])
    score = resolve_cvss(record)
    assert score is not None
    assert score.source == SeveritySource.NVD.value


def test_the_newest_cvss_version_in_a_container_wins():
    record = _cve_record(
        cna_metrics=[
            _metric("cvssV3_0", 6.0, vector="CVSS:3.0/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:N/A:N"),
            _metric("cvssV4_0", 9.3, vector="CVSS:4.0/AV:N/AC:L/AT:N/PR:N/UI:N/VC:H/VI:H/VA:H"),
            _metric("cvssV3_1", 8.1),
        ]
    )
    score = resolve_cvss(record)
    assert score is not None
    assert score.score == 9.3
    assert score.vector is not None and score.vector.startswith("CVSS:4.0/")


def test_the_vector_string_is_captured_and_an_absent_one_is_none():
    with_vector = resolve_cvss(_cve_record(cna_metrics=[_metric("cvssV3_1", 7.5)]))
    assert with_vector is not None and with_vector.vector is not None

    without = resolve_cvss(_cve_record(cna_metrics=[_metric("cvssV3_1", 7.5, vector=None)]))
    assert without is not None and without.vector is None

    blank = resolve_cvss(_cve_record(cna_metrics=[_metric("cvssV3_1", 7.5, vector="   ")]))
    assert blank is not None and blank.vector is None


def test_a_base_score_of_zero_is_a_real_score_not_a_missing_one():
    """CVSS 0.0 is severity NONE. A truthiness check would report it as unknown."""
    score = resolve_cvss(_cve_record(cna_metrics=[_metric("cvssV3_1", 0.0)]))
    assert score is not None
    assert score.score == 0.0
    assert score.source == SeveritySource.CNA.value


def test_an_unscored_cve_resolves_to_none_and_never_to_a_low_default():
    """The fabrication this module exists to prevent.

    A CVE with no base score anywhere has to come back as None so the models record `unknown`. The
    tempting default — 0.0, or "low" — would state that an unscored vulnerability is harmless,
    which on a critical one is the most damaging thing the pipeline could publish.
    """
    assert resolve_cvss(_cve_record(cna_metrics=[])) is None
    assert resolve_cvss(_cve_record(cna_metrics=[{"format": "CVSS", "scenarios": []}])) is None
    assert resolve_cvss(_cve_record(adp=[_adp("CISA-ADP")])) is None


def test_an_adp_that_is_not_part_of_the_chain_is_ignored():
    """A vendor ADP's score is not a CNA's and not CISA's; claiming either would be invented."""
    record = _cve_record(
        adp=[_adp("CVE", [_metric("cvssV3_1", 4.0)]), _adp("SomeVendor", [_metric("cvssV3_1", 3.0)])]
    )
    assert resolve_cvss(record) is None


def test_cisa_adp_ssvc_and_kev_entries_are_not_mistaken_for_scores():
    """The real CISA-ADP metrics list mixes `other` entries in with the CVSS ones."""
    record = _cve_record(
        adp=[
            _adp(
                "CISA-ADP",
                [
                    {"other": {"type": "ssvc", "content": {"options": [{"Exploitation": "active"}]}}},
                    {"other": {"type": "kev", "content": {"dateAdded": "2024-04-12"}}},
                ],
            )
        ]
    )
    assert resolve_cvss(record) is None


@pytest.mark.parametrize(
    "base",
    [None, "7.5", True, False, 11.0, -1.0, float("nan"), float("inf"), {}, []],
    ids=["none", "string", "true", "false", "too-high", "negative", "nan", "inf", "dict", "list"],
)
def test_a_base_score_that_is_not_a_number_in_range_is_not_used(base):
    """`True` is the sharp one: bool subclasses int, so it would arrive as a base score of 1.0."""
    assert resolve_cvss(_cve_record(cna_metrics=[_metric("cvssV3_1", base)])) is None


@pytest.mark.parametrize(
    "record",
    [None, {}, [], "a string", {"containers": None}, {"containers": {}}, {"containers": {"cna": None}},
     {"containers": {"adp": "not a list"}}, {"containers": {"cna": {"metrics": "not a list"}}},
     {"containers": {"adp": [None, 7, {"providerMetadata": None}, {"providerMetadata": {}}]}}],
    ids=["none", "empty", "list", "string", "null-containers", "no-containers", "null-cna",
         "adp-not-list", "metrics-not-list", "malformed-adps"],
)
def test_a_malformed_record_resolves_to_unknown_without_raising(record):
    """This runs over thousands of records per sync; one bad record must not end the run."""
    assert resolve_cvss(record) is None


# --------------------------------------------------------------------------------------------
# The registers together
# --------------------------------------------------------------------------------------------


def test_the_registers_compose_into_a_cveref_without_inventing_anything():
    """The end state: three registers, one CveRef, and no fabricated field.

    CVE-2024-3400 is in all three fixtures. CVE-2015-0001 is in none of them, and the point of the
    second half is that it still produces a valid CveRef — one that says unknown three times over
    rather than reporting an unexploited, unscored, zero-severity vulnerability.
    """
    kev = parse_kev_catalogue(json.dumps(KEV_PAYLOAD).encode())
    epss = parse_epss_snapshot(EPSS_CSV.encode())
    record = _cve_record(cna_metrics=[_metric("cvssV3_1", 10.0)])

    known = CveRef(
        id="CVE-2024-3400",
        cvss=resolve_cvss(record),
        epss=epss.lookup("CVE-2024-3400"),
        kev=kev.lookup("CVE-2024-3400"),
    )
    assert known.cvss is not None and known.cvss.score == 10.0
    assert known.cvss.source == SeveritySource.CNA.value
    assert known.epss.score == pytest.approx(0.94270) and known.epss.status == "known"
    assert known.kev.listed is True and known.kev.date_added == date(2024, 4, 12)

    unknown = CveRef(
        id="CVE-2015-0001",
        cvss=resolve_cvss(_cve_record(cna_metrics=[])),
        epss=epss.lookup("CVE-2015-0001"),
        kev=kev.lookup("CVE-2015-0001"),
    )
    assert unknown.cvss is None, "an unscored CVE must not carry a number"
    assert unknown.epss.score is None and unknown.epss.status == "unknown"
    assert unknown.kev.listed is False


def test_register_keys_are_always_valid_cveref_ids():
    """A key that CveRef would reject is a key nothing can ever use."""
    kev = parse_kev_catalogue(json.dumps(KEV_PAYLOAD).encode())
    epss = parse_epss_snapshot(EPSS_CSV.encode())
    for key in list(kev.entries) + list(epss.scores):
        CveRef(id=key)  # raises ValidationError if the key is not a canonical CVE id


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("CVE-2024-3400", "CVE-2024-3400"),
        ("cve-2024-3400", "CVE-2024-3400"),
        ("  CVE-2024-3400\n", "CVE-2024-3400"),
        ("CVE-2024-34000000", "CVE-2024-34000000"),
        ("GHSA-xxxx-yyyy-zzzz", None),
        ("CVE-24-3400", None),
        ("CVE-2024-340", None),
        ("", None),
        (None, None),
        (2024, None),
    ],
)
def test_cve_ids_are_normalised_consistently(raw, expected):
    """Two registers spelling an id differently is the same as one of them not holding it."""
    assert normalise_cve_id(raw) == expected


# --------------------------------------------------------------------------------------------
# Severity bands
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("score", "expected"),
    [
        (10.0, Severity.CRITICAL),
        (9.8, Severity.CRITICAL),
        (9.0, Severity.CRITICAL),
        (8.9, Severity.HIGH),
        (7.0, Severity.HIGH),
        (6.9, Severity.MEDIUM),
        (4.0, Severity.MEDIUM),
        (3.9, Severity.LOW),
        (0.1, Severity.LOW),
    ],
)
def test_the_cvss_v3_1_bands_are_applied_at_their_published_boundaries(score, expected):
    assert severity_for_score(score) == expected


def test_no_score_is_unknown_and_not_low():
    """The whole point: `unknown` and `low` are different claims about a vulnerability."""
    assert severity_for_score(None) is Severity.UNKNOWN


def test_a_zero_score_is_low_rather_than_unknown():
    """0.0 is a measurement. CVSS calls it NONE and Severity has no such member, so it bands as
    LOW — scoring.yaml weights low below unknown, so known-harmless still ranks under unrated."""
    assert severity_for_score(0.0) is Severity.LOW


@pytest.mark.parametrize("score", [-0.1, 10.1, 99.0, -50.0])
def test_a_score_outside_the_scale_is_unknown_rather_than_clamped(score):
    """Clamping would present a malformed field as a confident CRITICAL."""
    assert severity_for_score(score) is Severity.UNKNOWN


def test_a_banded_score_carries_the_rung_it_came_from():
    cvss = CvssScore(score=9.8, vector="CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H", source="cna")
    assert severity_from_cvss(cvss) == (Severity.CRITICAL, SeveritySource.CNA)


def test_cisa_adp_is_recorded_as_its_own_provenance_not_as_nvd():
    cvss = CvssScore(score=7.5, source="cisa_adp")
    assert severity_from_cvss(cvss) == (Severity.HIGH, SeveritySource.CISA_ADP)


def test_an_absent_score_yields_unknown_severity_and_unknown_provenance():
    assert severity_from_cvss(None) == (Severity.UNKNOWN, SeveritySource.UNKNOWN)


def test_an_unrecognised_provider_keeps_the_band_but_not_a_guessed_provenance():
    """The band came from a real score, so it stands; the provider did not, so it does not."""
    cvss = CvssScore(score=9.1, source="some-vendor-adp")
    assert severity_from_cvss(cvss) == (Severity.CRITICAL, SeveritySource.UNKNOWN)


def test_every_band_is_reachable_and_they_partition_the_scale():
    """A gap or an overlap in the bands would be invisible in per-value tests."""
    bands = {severity_for_score(round(x * 0.1, 1)) for x in range(0, 101)}
    assert bands == {Severity.LOW, Severity.MEDIUM, Severity.HIGH, Severity.CRITICAL}
