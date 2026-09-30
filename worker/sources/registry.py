"""Source registry: loading and filtering feed sources."""

from pathlib import Path

import yaml

from worker.models import Lane, SourceConfig


def load_registry(path: Path) -> list[SourceConfig]:
    """Load source registry from YAML file.
    
    Parses a YAML file containing a 'sources' key with a list of source configurations.
    Each source is validated against the SourceConfig schema.
    
    Args:
        path: Path to the YAML file.
        
    Returns:
        List of validated SourceConfig objects.
        
    Raises:
        ValueError: If duplicate source IDs are found.
    """
    with open(path, "r") as f:
        data = yaml.safe_load(f)
    
    sources_list = data.get("sources", [])
    seen_ids = set()
    sources = []
    
    for source_data in sources_list:
        source = SourceConfig(**source_data)
        
        if source.id in seen_ids:
            raise ValueError(f"duplicate source id: {source.id}")
        
        seen_ids.add(source.id)
        sources.append(source)
    
    return sources


def sources_for_lane(sources: list[SourceConfig], lane: Lane) -> list[SourceConfig]:
    """Filter sources by lane, excluding disabled sources.
    
    Args:
        sources: List of source configurations.
        lane: The lane to filter by (FAST, NORMAL, or DEEP).
        
    Returns:
        List of enabled sources in the specified lane.
    """
    return [s for s in sources if s.lane == lane and s.enabled]
