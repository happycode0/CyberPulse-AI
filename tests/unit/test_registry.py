"""Tests for the source registry."""

from pathlib import Path

import pytest

from worker.models import Lane, Standing
from worker.sources.registry import REGISTRY_PATH, load_registry, sources_for_lane


def test_registry_loads_and_every_source_is_valid():
    assert len(load_registry(Path("config/sources.yaml"))) >= 50


def test_source_ids_are_unique():
    ids = [s.id for s in load_registry(Path("config/sources.yaml"))]
    assert len(ids) == len(set(ids))


def test_duplicate_id_raises(fixture):
    with pytest.raises(ValueError, match="duplicate source id"):
        load_registry(fixture("sources_duplicate_id.yaml"))


def test_lifecycle_state_in_static_yaml_is_rejected(fixture):
    """Lifecycle state is tracked in the database; the git-tracked YAML must not carry it."""
    with pytest.raises(ValueError, match="lifecycle_state.*test_source_1"):
        load_registry(fixture("sources_lifecycle_state.yaml"))


def test_committed_registry_carries_no_lifecycle_state():
    assert all(s.lifecycle_state is None for s in load_registry(Path("config/sources.yaml")))


def test_fast_lane_contains_acsc_alerts_and_kev():
    fast = {s.id for s in sources_for_lane(load_registry(Path("config/sources.yaml")), Lane.FAST)}
    assert {"acsc_alerts", "cisa_kev"} <= fast


def test_disabled_sources_are_excluded_from_every_lane():
    reg = load_registry(Path("config/sources.yaml"))
    assert all(s.enabled for lane in Lane for s in sources_for_lane(reg, lane))


def test_known_blocked_sources_are_disabled_with_a_reason():
    by_id = {s.id: s for s in load_registry(Path("config/sources.yaml"))}
    for sid in ("securityweek", "x_security_search"):
        assert by_id[sid].enabled is False and by_id[sid].notes


def test_a_source_naming_an_unknown_publisher_is_rejected(tmp_path):
    path = tmp_path / "sources.yaml"
    path.write_text(
        "publishers:\n  cisa:\n    name: CISA\n"
        "sources:\n  - {id: a, name: A, publisher: nobody, type: rss, region: au, "
        "category: news, class: feed, priority: 1, lane: fast, enabled: true, "
        "url: 'https://example.test/feed', parser: rss, expected_frequency: daily}\n"
    )
    with pytest.raises(ValueError, match="'a': unknown publisher 'nobody'"):
        load_registry(path)


def test_every_shipped_source_has_a_description_and_a_standing():
    """The Sources page shows both, and reputation starts from the standing
    (docs/wiki/importance-and-reputation.md). Only a source the discovery gate adds goes
    without, and it lives in the database, not here."""
    for s in load_registry(REGISTRY_PATH):
        assert s.description, f"{s.id} has no description"
        assert isinstance(s.standing, Standing), f"{s.id} has no standing"


def test_the_publisher_reads_the_shipped_registry():
    assert REGISTRY_PATH == Path(__file__).resolve().parents[2] / "config" / "sources.yaml"


def test_the_standings_where_it_matters():
    by_id = {s.id: s.standing for s in load_registry(REGISTRY_PATH)}
    assert {by_id[i] for i in ("acsc_alerts", "cisa_kev", "asd", "oaic")} == {
        Standing.AUTHORITATIVE
    }
    assert by_id["abc_cyber"] is by_id["krebs"] is Standing.ESTABLISHED
    assert by_id["x_security_search"] is Standing.COMMUNITY


_SOURCE = (
    "  - {id: a, name: A, type: rss, region: au, category: news, class: feed, priority: 1, "
    "lane: fast, enabled: true, url: 'https://example.test/feed', parser: rss, "
    "expected_frequency: daily"
)


@pytest.mark.parametrize(
    ("extra", "error"),
    [
        (", standing: famous", "standing"),
        (", description: ''", "description must be one non-empty line"),
        (", description: \"two\\nlines\"", "description must be one non-empty line"),
    ],
)
def test_a_bad_standing_or_description_is_rejected(tmp_path, extra, error):
    path = tmp_path / "sources.yaml"
    path.write_text("sources:\n" + _SOURCE + extra + "}\n")
    with pytest.raises(ValueError, match=error):
        load_registry(path)


def test_standing_and_description_are_optional_to_the_model(tmp_path):
    path = tmp_path / "sources.yaml"
    path.write_text("sources:\n" + _SOURCE + "}\n")
    [source] = load_registry(path)
    assert source.description is None and source.standing is None
