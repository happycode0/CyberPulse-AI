"""Tests for the source registry."""

from pathlib import Path

import pytest

from worker.models import Lane
from worker.sources.registry import load_registry, sources_for_lane


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
