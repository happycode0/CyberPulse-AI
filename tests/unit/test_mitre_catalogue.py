"""The MITRE ATT&CK and ATLAS catalogues: finding the current release, and reading it whole.

The fixtures are small copies of the real shapes (attack-stix-data's index.json and STIX bundle,
atlas-data's dist/manifest.yaml and release file), padded with generated techniques to clear the
completeness floors.
"""

import json
from datetime import date

import httpx
import pytest
import yaml

from worker.groundtruth import registers
from worker.groundtruth.errors import GroundTruthError
from worker.groundtruth.mitre import (
    ATLAS_BASE_URL,
    ATLAS_MANIFEST_URL,
    ATTACK_INDEX_URL,
    MIN_ATLAS_TECHNIQUES,
    MIN_ATTACK_TECHNIQUES,
    Release,
    latest_atlas_release,
    latest_attack_release,
    parse_atlas,
    parse_attack_bundle,
)
from worker.groundtruth.registers import fetch_mitre_catalogue, fetch_mitre_release

BUNDLE_URL = (
    "https://raw.githubusercontent.com/mitre-attack/attack-stix-data/master/"
    "enterprise-attack/enterprise-attack-19.2.json"
)
INDEX = {
    "collections": [
        {"name": "Mobile ATT&CK", "versions": [{"version": "19.2", "url": "https://x/mobile"}]},
        {
            "name": "Enterprise ATT&CK",
            "versions": [
                {"version": "19.2", "url": BUNDLE_URL, "modified": "2026-09-15T12:00:00.000Z"},
                {"version": "19.1", "url": "https://x/19.1"},
            ],
        },
    ]
}
ATTACK = Release("enterprise", "ATT&CK v19.2", BUNDLE_URL, None)


def pattern(tid: str, name: str, *phases: str, **extra) -> dict:
    return {
        "type": "attack-pattern",
        "name": name,
        "external_references": [
            {"source_name": "capec", "external_id": "CAPEC-1"},
            {"source_name": "mitre-attack", "external_id": tid},
        ],
        "kill_chain_phases": [{"kill_chain_name": "mitre-attack", "phase_name": p} for p in phases],
        **extra,
    }


def bundle(version: str = "19.2", padding: int = MIN_ATTACK_TECHNIQUES) -> bytes:
    objects = [
        {"type": "x-mitre-collection", "x_mitre_version": version},
        pattern("T1190", "Exploit Public-Facing Application", "initial-access"),
        pattern("T1059.001", "PowerShell", "execution"),
        pattern("T1027", "Obfuscated Files or Information", "stealth", "defense-impairment"),
        pattern("T1000", "Revoked", "execution", revoked=True),
        pattern("T1001", "Deprecated", "execution", x_mitre_deprecated=True),
        {"type": "intrusion-set", "name": "APT0"},
    ]
    objects += [pattern(f"T9{n:03d}", f"Padding {n}", "impact") for n in range(padding)]
    return json.dumps({"type": "bundle", "objects": objects}).encode()


MANIFEST = [
    {
        "release": "2026.09",
        "release-date": date(2026, 9, 1),
        "versions": [
            {"format-version": "7.0", "path": "v7/ATLAS-2026.09.yaml"},
            {"format-version": "6.0", "path": "v6/ATLAS-latest.yaml"},
            {"format-version": "6.0", "path": "v6/ATLAS-2026.09.yaml"},
        ],
    },
    {
        "release": "2026.06",
        "versions": [{"format-version": "6.0", "path": "v6/ATLAS-2026.06.yaml"}],
    },
]
ATLAS = Release("atlas", "ATLAS 2026.09", ATLAS_BASE_URL + "v6/ATLAS-2026.09.yaml", None)


def atlas_file(version: str = "2026.09", padding: int = MIN_ATLAS_TECHNIQUES) -> bytes:
    techniques = {
        "AML.T0051": {"name": "LLM Prompt Injection"},
        "AML.T0051.000": {"name": "Direct"},
        "AML.T0054": {"name": "LLM Jailbreak"},
        "AML.T0099": {"name": "Gone", "deprecated": True},
    }
    techniques |= {f"AML.T9{n:03d}": {"name": f"Padding {n}"} for n in range(padding)}
    data = {
        "format-version": "6.0",
        "collection": {"version": version},
        "tactics": {"AML.TA0005": {"name": "Execution"}},
        "techniques": techniques,
        "relationships": {
            "AML.T0051": {"achieves": [{"source": "AML.T0051", "target": "AML.TA0005"}]},
            "AML.T0054": {
                "achieves": [
                    {"source": "AML.T0054", "target": "AML.TA0012"},
                    {"source": "AML.T0054", "target": "AML.TA0007"},
                ],
                "employs": [{"source": "AML.T0054", "target": "AML.M0000"}],
            },
        },
    }
    return yaml.safe_dump(data).encode()


# --- ATT&CK ------------------------------------------------------------------------------------


def test_the_newest_enterprise_release_is_the_one_listed_first():
    release = latest_attack_release(json.dumps(INDEX).encode())
    assert (release.matrix, release.version, release.url) == (
        "enterprise",
        "ATT&CK v19.2",
        BUNDLE_URL,
    )
    assert release.released_at is not None and release.released_at.year == 2026


def test_an_index_without_enterprise_raises():
    with pytest.raises(GroundTruthError, match="Enterprise"):
        latest_attack_release(json.dumps({"collections": INDEX["collections"][:1]}).encode())


def test_an_index_url_that_is_not_https_raises():
    index = json.loads(json.dumps(INDEX))
    index["collections"][1]["versions"][0]["url"] = "http://x/19.2"
    with pytest.raises(GroundTruthError, match="https"):
        latest_attack_release(json.dumps(index).encode())


def test_the_bundle_gives_live_techniques_with_their_tactics_and_parents():
    by_id = {t.id: t for t in parse_attack_bundle(bundle(), ATTACK).techniques}
    assert by_id["T1190"].tactics == ("initial-access",)
    assert by_id["T1027"].tactics == ("stealth", "defense-impairment")
    assert by_id["T1059.001"].parent_id == "T1059"
    assert by_id["T1190"].parent_id is None
    # Revoked and deprecated techniques can never be suggested.
    assert "T1000" not in by_id and "T1001" not in by_id


def test_a_bundle_for_another_release_is_refused():
    with pytest.raises(GroundTruthError, match="is not ATT&CK v19.2"):
        parse_attack_bundle(bundle(version="19.1"), ATTACK)


def test_a_short_bundle_is_refused_as_incomplete():
    with pytest.raises(GroundTruthError, match="too few"):
        parse_attack_bundle(bundle(padding=10), ATTACK)


# --- ATLAS -------------------------------------------------------------------------------------


def test_the_atlas_release_is_a_versioned_file_in_the_format_we_read():
    # Not v7, which this parser has not been checked against, and never ATLAS-latest.yaml.
    release = latest_atlas_release(yaml.safe_dump(MANIFEST).encode())
    assert release == Release(
        "atlas", "ATLAS 2026.09", ATLAS_BASE_URL + "v6/ATLAS-2026.09.yaml", release.released_at
    )
    assert release.released_at is not None and release.released_at.month == 9


@pytest.mark.parametrize("path", ["../escape.yaml", "/abs/ATLAS.yaml", "v6/ATLAS-latest.yaml"])
def test_a_manifest_path_that_could_point_anywhere_is_skipped(path):
    manifest = [{"release": "x", "versions": [{"format-version": "6.0", "path": path}]}]
    with pytest.raises(GroundTruthError):
        latest_atlas_release(yaml.safe_dump(manifest).encode())


def test_atlas_tactics_come_from_achieves_and_sub_techniques_inherit_them():
    by_id = {t.id: t for t in parse_atlas(atlas_file(), ATLAS).techniques}
    assert by_id["AML.T0051"].tactics == ("AML.TA0005",)
    assert by_id["AML.T0054"].tactics == ("AML.TA0012", "AML.TA0007")
    assert by_id["AML.T0051.000"].parent_id == "AML.T0051"
    assert by_id["AML.T0051.000"].tactics == ("AML.TA0005",)
    assert "AML.T0099" not in by_id


def test_an_atlas_file_for_another_release_is_refused():
    with pytest.raises(GroundTruthError, match="is not ATLAS 2026.09"):
        parse_atlas(atlas_file(version="2026.06"), ATLAS)


def test_a_short_atlas_file_is_refused():
    with pytest.raises(GroundTruthError, match="too few"):
        parse_atlas(atlas_file(padding=5), ATLAS)


# --- downloading -------------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def no_cache(monkeypatch):
    monkeypatch.setattr("worker.groundtruth.registers.cache_raw", lambda source_id, body: None)


@pytest.fixture
def client():
    return httpx.AsyncClient()


async def test_the_release_check_reads_only_the_index(respx_mock, client):
    respx_mock.get(ATTACK_INDEX_URL).respond(200, json=INDEX)
    respx_mock.get(ATLAS_MANIFEST_URL).respond(200, content=yaml.safe_dump(MANIFEST).encode())
    assert (await fetch_mitre_release(client, "enterprise")).version == "ATT&CK v19.2"
    assert (await fetch_mitre_release(client, "atlas")).version == "ATLAS 2026.09"
    assert not respx_mock.calls or all(
        str(c.request.url) in (ATTACK_INDEX_URL, ATLAS_MANIFEST_URL) for c in respx_mock.calls
    )


async def test_an_unreachable_index_raises(respx_mock, client):
    respx_mock.get(ATTACK_INDEX_URL).respond(503)
    with pytest.raises(GroundTruthError, match="attack index unavailable"):
        await fetch_mitre_release(client, "enterprise")


async def test_a_release_is_downloaded_and_parsed(respx_mock, client):
    respx_mock.get(BUNDLE_URL).respond(200, content=bundle())
    respx_mock.get(ATLAS.url).respond(200, content=atlas_file())
    attack = await fetch_mitre_catalogue(client, ATTACK)
    atlas = await fetch_mitre_catalogue(client, ATLAS)
    assert len(attack.techniques) == MIN_ATTACK_TECHNIQUES + 3
    assert len(atlas.techniques) == MIN_ATLAS_TECHNIQUES + 3


async def test_a_release_cut_off_at_the_size_cap_is_refused(respx_mock, client, monkeypatch):
    body = bundle()
    monkeypatch.setattr(registers, "MITRE_MAX_BYTES", len(body))
    respx_mock.get(BUNDLE_URL).respond(200, content=body)
    with pytest.raises(GroundTruthError):
        await fetch_mitre_catalogue(client, ATTACK)
