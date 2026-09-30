"""Tests for JSON API collector (KEV parser)."""

from pathlib import Path

import pytest

from worker.collectors.json_api import parse_json_api
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
