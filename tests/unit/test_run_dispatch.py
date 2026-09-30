
import pytest

from worker.collectors.json_api import JSON_PARSERS
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
    else:
        assert source.type in ("rss", "atom")


def test_unsupported_type_raises_a_clear_error():
    web = next(s for s in SOURCES if s.type == "web_page")
    with pytest.raises(ValueError, match="unsupported source type 'web_page'"):
        parse_body(web, b"<html/>")
