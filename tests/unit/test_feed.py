"""Tests for feed parsing."""

from pathlib import Path

import pytest

from worker.collectors.feed import parse_feed
from worker.models import SourceConfig, RawItem


@pytest.fixture
def fixture_bytes():
    """Load fixture bytes from tests/fixtures/feeds/"""
    def _load(name: str) -> bytes:
        path = Path(__file__).parent.parent / "fixtures" / "feeds" / name
        if not path.exists():
            raise FileNotFoundError(f"Fixture not found: {path}")
        return path.read_bytes()
    return _load


@pytest.fixture
def acsc_src():
    return SourceConfig(
        id="acsc-1",
        name="ACSC Alerts",
        type="advisory",
        region="au",
        category="security",
        source_class="government",
        priority=1,
        lane="fast",
        enabled=True,
        url="https://www.cyber.gov.au/acsc/view-all-advisories",
        parser="rss",
        expected_frequency="daily"
    )


@pytest.fixture
def cisa_src():
    return SourceConfig(
        id="cisa-1",
        name="CISA Advisories",
        type="advisory",
        region="us",
        category="security",
        source_class="government",
        priority=1,
        lane="fast",
        enabled=True,
        url="https://www.cisa.gov/news-events/alerts",
        parser="atom",
        expected_frequency="daily"
    )


@pytest.fixture
def generic_src():
    return SourceConfig(
        id="test-1",
        name="Test Source",
        type="advisory",
        region="us",
        category="security",
        source_class="vendor",
        priority=5,
        lane="normal",
        enabled=True,
        url="https://example.com/feed",
        parser="rss",
        expected_frequency="weekly"
    )


def test_parse_feed_extracts_items_from_acsc(acsc_src, fixture_bytes):
    items = parse_feed(acsc_src, fixture_bytes("acsc_alerts.xml"))
    assert len(items) == 7
    assert all(isinstance(i, RawItem) for i in items)
    assert all(i.url.startswith("https://") for i in items)


def test_parse_feed_prefers_guid_when_present(cisa_src, fixture_bytes):
    items = parse_feed(cisa_src, fixture_bytes("cisa_advisories.xml"))
    assert len(items) > 0
    assert items[0].guid is not None


def test_parse_feed_tolerates_malformed_xml(generic_src, fixture_bytes):
    items = parse_feed(generic_src, fixture_bytes("malformed.xml"))
    assert items == []


def test_parse_feed_on_empty_feed_returns_empty_list(generic_src, fixture_bytes):
    items = parse_feed(generic_src, fixture_bytes("empty.xml"))
    assert items == []


def test_payload_hash_is_stable_across_calls(generic_src, fixture_bytes):
    B = fixture_bytes("acsc_alerts.xml")
    a = parse_feed(generic_src, B)[0].payload_hash
    b = parse_feed(generic_src, B)[0].payload_hash
    assert a == b
