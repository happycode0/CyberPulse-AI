"""JSON API parser with pluggable parser registry (KEV parser for Stage 1)."""

import hashlib
import json
from datetime import datetime
from typing import Callable
from zoneinfo import ZoneInfo

from worker.collectors.dates import parse_date
from worker.models import RawItem, SourceConfig

UTC = ZoneInfo("UTC")


def kev_parser(source: SourceConfig, data: dict) -> list[RawItem]:
    """
    Parse CISA Known Exploited Vulnerabilities catalog.
    
    Expects top-level keys: catalogVersion, dateReleased, vulnerabilities
    Each vulnerability in the array should have:
        cveID, vendorProject, product, vulnerabilityName, dateAdded, 
        shortDescription, requiredAction, dueDate
    
    Args:
        source: SourceConfig for this source
        data: Parsed JSON dict
    
    Returns:
        List of RawItem objects with guid=cveID, title from vendor/product/name,
        url pointing to CISA catalog, published=dateAdded
    """
    items: list[RawItem] = []
    
    # Expect top-level vulnerabilities key
    vulnerabilities = data.get("vulnerabilities")
    if not isinstance(vulnerabilities, list):
        return []
    
    now = datetime.now(UTC)
    
    for vuln in vulnerabilities:
        try:
            # Extract required fields
            cve_id = vuln.get("cveID", "").strip()
            vendor = vuln.get("vendorProject", "").strip()
            product = vuln.get("product", "").strip()
            name = vuln.get("vulnerabilityName", "").strip()
            short_desc = vuln.get("shortDescription", "").strip()
            date_added_str = vuln.get("dateAdded", "")
            
            # Skip if missing critical fields
            if not cve_id or not vendor or not product or not name:
                continue
            
            # Build title: "{vendorProject} {product}: {vulnerabilityName}"
            title = f"{vendor} {product}: {name}"
            
            # URL: standardized CISA KEV catalog URL
            url = "https://www.cisa.gov/known-exploited-vulnerabilities-catalog"
            
            # Parse dateAdded as published date
            published = parse_date(date_added_str) if date_added_str else None
            
            # Compute payload_hash: sha256 of canonical representation
            canonical = f"{cve_id}|{title}|{short_desc}".encode("utf-8")
            payload_hash = hashlib.sha256(canonical).hexdigest()
            
            item = RawItem(
                source_id=source.id,
                url=url,
                guid=cve_id,
                title=title,
                raw_summary=short_desc if short_desc else None,
                published=published,
                fetched_at=now,
                payload_hash=payload_hash
            )
            items.append(item)
        except Exception:
            # Skip malformed entries
            continue
    
    return items


# Registry of parser name -> callable
JSON_PARSERS: dict[str, Callable[[SourceConfig, dict], list[RawItem]]] = {
    "kev": kev_parser,
}


def parse_json_api(source: SourceConfig, body: bytes) -> list[RawItem]:
    """
    Parse JSON API response using registered parser.
    
    - Looks up parser by source.parser name in JSON_PARSERS registry
    - Raises KeyError if parser not registered (configuration error)
    - Returns empty list on malformed JSON or missing expected keys
    - Returns empty list if JSON is valid but has no parseable content
    
    Args:
        source: SourceConfig specifying parser name
        body: Raw JSON bytes
    
    Returns:
        List of RawItem objects, or empty list on parse/content error
    
    Raises:
        KeyError: If parser name not in registry
    """
    # Raise KeyError if parser not registered
    if source.parser not in JSON_PARSERS:
        raise KeyError(f"no JSON parser named '{source.parser}'")
    
    # Parse JSON, return empty list on error
    try:
        data = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return []
    
    # Call the parser
    parser = JSON_PARSERS[source.parser]
    try:
        return parser(source, data)
    except Exception:
        return []
