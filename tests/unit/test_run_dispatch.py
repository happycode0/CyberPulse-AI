
import pytest

from worker.collectors.json_api import JSON_PARSERS
from worker.collectors.web_page import WEB_PAGE_PARSERS
from worker.pipeline.health import STALE_AFTER_DAYS
from worker.pipeline.run import DEFAULT_REGISTRY_PATH, DEFAULT_SCORING_PATH, parse_body
from worker.pipeline.score import ScoringConfig
from worker.sources.registry import load_registry

SOURCES = load_registry(DEFAULT_REGISTRY_PATH)
ENABLED = [s for s in SOURCES if s.enabled]


def test_default_config_paths_point_at_the_committed_files():
    assert DEFAULT_REGISTRY_PATH.is_file() and DEFAULT_SCORING_PATH.is_file()
    ScoringConfig.load(DEFAULT_SCORING_PATH)


@pytest.mark.parametrize("source", ENABLED, ids=lambda s: s.id)
def test_every_enabled_committed_source_has_a_collector(source):
    """An enabled source the orchestrator cannot parse would fail on every run, forever."""
    if source.type == "json_api":
        assert source.parser in JSON_PARSERS
    elif source.type == "web_page":
        assert source.parser in WEB_PAGE_PARSERS
    else:
        assert source.type in ("rss", "atom")


@pytest.mark.parametrize("source", SOURCES, ids=lambda s: s.id)
def test_every_committed_frequency_has_a_staleness_threshold(source):
    """A misspelt frequency would silently get the daily threshold."""
    assert source.expected_frequency in STALE_AFTER_DAYS


def test_unsupported_type_raises_a_clear_error():
    sitemap = SOURCES[0].model_copy(update={"type": "sitemap"})
    with pytest.raises(ValueError, match="unsupported source type 'sitemap'"):
        parse_body(sitemap, b"<urlset/>")


def test_a_web_page_source_is_read_by_its_named_parser():
    asd = next(s for s in SOURCES if s.id == "asd")
    body = (
        b'<article class="node--type-news"><a href="/news/x"><h3>A statement</h3></a>'
        b'<time datetime="2026-06-22T12:00:00Z">22 June 2026</time></article>'
    )
    [item] = parse_body(asd, body)
    assert (item.title, item.url) == ("A statement", "https://www.asd.gov.au/news/x")
