"""Source registry: loading and filtering feed sources."""

from pathlib import Path

import yaml

from worker.models import Lane, PublisherConfig, SourceConfig


def load_registry(path: Path) -> list[SourceConfig]:
    """Load source registry from YAML file.
    
    Parses a YAML file containing a 'sources' key with a list of source configurations.
    Each source is validated against the SourceConfig schema.
    
    Args:
        path: Path to the YAML file.
        
    Returns:
        List of validated SourceConfig objects.
        
    Raises:
        ValueError: If duplicate source IDs are found, a source sets `lifecycle_state`
            (which is database-owned and overlaid at runtime), or a source names a
            `publisher` the registry's `publishers` does not list.
    """
    with open(path, "r") as f:
        data = yaml.safe_load(f)
    
    publishers = _parse_publishers(data)
    sources_list = data.get("sources", [])
    seen_ids = set()
    sources = []
    
    for source_data in sources_list:
        if "lifecycle_state" in source_data:
            raise ValueError(
                f"lifecycle_state must not be set in the static registry "
                f"(source {source_data.get('id')!r}): it is tracked in the database"
            )
        source = SourceConfig(**source_data)
        
        if source.id in seen_ids:
            raise ValueError(f"duplicate source id: {source.id}")
        if source.publisher is not None and source.publisher not in publishers:
            raise ValueError(f"source {source.id!r}: unknown publisher {source.publisher!r}")
        
        seen_ids.add(source.id)
        sources.append(source)
    
    return sources


def load_publishers(path: Path) -> dict[str, PublisherConfig]:
    """The registry's `publishers`: organisations behind more than one feed, by id."""
    with open(path) as f:
        return _parse_publishers(yaml.safe_load(f))


def _parse_publishers(data: dict) -> dict[str, PublisherConfig]:
    return {pid: PublisherConfig(**p) for pid, p in (data.get("publishers") or {}).items()}


def sources_for_lane(sources: list[SourceConfig], lane: Lane) -> list[SourceConfig]:
    """Filter sources by lane, excluding disabled sources.
    
    Args:
        sources: List of source configurations.
        lane: The lane to filter by (FAST, NORMAL, or DEEP).
        
    Returns:
        List of enabled sources in the specified lane.
    """
    return [s for s in sources if s.lane == lane and s.enabled]
