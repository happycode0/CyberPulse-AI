"""Tests for JSON API collector (KEV and MSRC parsers)."""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from worker.collectors.json_api import MSRC_RELEASES_KEPT, parse_json_api
from worker.models import SourceConfig, RawItem


@pytest.fixture
def fixture_bytes():
    """Load fixture bytes from tests/fixtures/"""
    def _load(name: str) -> bytes:
        path = Path(__file__).parent.parent / "fixtures" / name
        if not path.exists():
            raise FileNotFoundError(f"Fixture not found: {path}")
        return path.read_bytes()
    return _load


@pytest.fixture
def kev_src():
    return SourceConfig(
        id="cisa-kev-1",
        name="CISA Known Exploited Vulnerabilities",
        type="vulnerability",
        region="us",
        category="security",
        source_class="government",
        priority=1,
        lane="fast",
        enabled=True,
        url="https://www.cisa.gov/known-exploited-vulnerabilities-catalog",
        parser="kev",
        expected_frequency="daily"
    )


def test_kev_parser_extracts_vulnerabilities(kev_src, fixture_bytes):
    items = parse_json_api(kev_src, fixture_bytes("kev_sample.json"))
    assert len(items) == 3 and all(i.title for i in items)


def test_kev_item_url_points_to_cisa_catalog(kev_src, fixture_bytes):
    assert "cisa.gov" in parse_json_api(kev_src, fixture_bytes("kev_sample.json"))[0].url


def test_kev_guid_is_the_cve_id(kev_src, fixture_bytes):
    assert parse_json_api(kev_src, fixture_bytes("kev_sample.json"))[0].guid == "CVE-2026-88772"


def test_unknown_parser_name_raises(kev_src, fixture_bytes):
    with pytest.raises(KeyError, match="no JSON parser named"):
        bad_src = kev_src.model_copy(update={"parser": "nonexistent"})
        parse_json_api(bad_src, b"{}")


def test_invalid_json_returns_empty_list(kev_src):
    assert parse_json_api(kev_src, b"{not json") == []


def test_missing_expected_key_returns_empty_list(kev_src):
    assert parse_json_api(kev_src, b'{"unexpected": 1}') == []


def test_kev_entries_get_distinct_urls_and_titles_carrying_the_cve(kev_src, fixture_bytes):
    """A shared URL would make the resolver treat every entry after the first as a duplicate;
    generic catalog names would merge different CVEs added on the same day."""
    items = parse_json_api(kev_src, fixture_bytes("kev_sample.json"))
    assert len({i.url for i in items}) == len(items) == 3
    assert all(i.guid in i.url and i.guid in i.title for i in items)


def test_the_kev_parser_name_used_by_the_committed_registry_is_registered(kev_src, fixture_bytes):
    src = kev_src.model_copy(update={"parser": "json_kev"})
    assert len(parse_json_api(src, fixture_bytes("kev_sample.json"))) == 3


def msrc_src():
    from worker.pipeline.run import DEFAULT_REGISTRY_PATH
    from worker.sources.registry import load_registry

    return next(s for s in load_registry(DEFAULT_REGISTRY_PATH) if s.id == "msrc_cvrf")


def test_msrc_releases_are_named_for_microsoft_and_link_to_their_release_note(fixture):
    items = parse_json_api(msrc_src(), fixture("msrc_cvrf_updates.json").read_bytes())
    assert [(i.guid, i.title) for i in items] == [
        ("2026-Oct", "Microsoft October 2026 Early Security Updates"),
        ("2026-Sep", "Microsoft September 2026 Security Updates"),
        ("2026-May", "Microsoft May 2026 Security Updates"),
        ("2016-Apr", "Microsoft April 2016 Security Updates"),
    ]
    october = items[0]
    assert october.url == "https://msrc.microsoft.com/update-guide/releaseNote/2026-Oct"
    assert october.published == datetime(2026, 10, 2, 7, tzinfo=UTC)
    assert october.raw_summary is None


def test_only_the_newest_msrc_releases_are_kept():
    releases = [
        {"ID": f"2025-{n:02d}", "DocumentTitle": f"Release {n}",
         "InitialReleaseDate": f"2025-{n:02d}-10T07:00:00Z"}
        for n in range(1, 13)
    ] + [{"ID": "2024-Dec", "DocumentTitle": "Old", "InitialReleaseDate": "2024-12-10T07:00:00Z"}]
    body = json.dumps({"value": list(reversed(releases))}).encode()
    items = parse_json_api(msrc_src(), body)
    assert len(items) == MSRC_RELEASES_KEPT == 12
    assert "2024-Dec" not in {i.guid for i in items}


def test_a_revised_msrc_release_keeps_its_guid_and_changes_its_hash():
    def one(title):
        body = json.dumps({"value": [
            {"ID": "2026-Oct", "DocumentTitle": title, "InitialReleaseDate": "2026-10-02T07:00:00Z"}
        ]}).encode()
        [item] = parse_json_api(msrc_src(), body)
        return item

    early, final = one("October 2026 Early Security Updates"), one("October 2026 Security Updates")
    assert early.guid == final.guid and early.payload_hash != final.payload_hash


def test_msrc_entries_missing_an_id_title_or_date_are_skipped():
    body = json.dumps({"value": [
        {"DocumentTitle": "No id", "InitialReleaseDate": "2026-10-02T07:00:00Z"},
        {"ID": "2026-Sep", "InitialReleaseDate": "2026-09-08T07:00:00Z"},
        {"ID": "2026-Aug", "DocumentTitle": "No date"},
        "not an object",
        {"ID": "2026-Jul", "DocumentTitle": "July 2026 Security Updates",
         "InitialReleaseDate": "2026-07-14T07:00:00Z"},
    ]}).encode()
    assert [i.guid for i in parse_json_api(msrc_src(), body)] == ["2026-Jul"]
    assert parse_json_api(msrc_src(), b'{"value": null}') == []
