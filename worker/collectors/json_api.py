"""JSON API parser with pluggable parser registry (KEV parser for Stage 1)."""

import hashlib
import json
from datetime import datetime
from typing import Callable
from zoneinfo import ZoneInfo

from worker.collectors.dates import parse_date
from worker.models import RawItem, SourceConfig

UTC = ZoneInfo("UTC")

KEV_CATALOG_URL = "https://www.cisa.gov/known-exploited-vulnerabilities-catalog"


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
        List of RawItem objects with guid=cveID, title from cveID/vendor/product/name,
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
            
            # Lead with the CVE id: catalog names are often generic ("Windows Privilege
            # Escalation Vulnerability"), and without the id two different CVEs added on
            # the same day would share a title and be merged into one event. The id in the
            # title is also what links the entry to other outlets' reports of that CVE.
            title = f"{cve_id}: {vendor} {product} {name}"
            
            # URL: the CISA KEV catalog filtered to this CVE. It must be unique per entry:
            # a shared catalog URL would make the resolver's URL-identity rung treat every
            # entry after the first as a duplicate of it.
            url = f"{KEV_CATALOG_URL}?search_api_fulltext={cve_id}"
            
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


MSRC_RELEASE_NOTE_URL = "https://msrc.microsoft.com/update-guide/releaseNote/"

# The updates list goes back to 2016. A year of monthly releases is what a reader can still
# act on; older ones would only be stored to be archived on the same run.
MSRC_RELEASES_KEPT = 12


def msrc_cvrf_parser(source: SourceConfig, data: dict) -> list[RawItem]:
    """
    Parse Microsoft's security release list (api.msrc.microsoft.com/cvrf/v3.0/updates).

    Expects a top-level `value` list, one entry per release: ID ("2026-Oct"), DocumentTitle
    ("October 2026 Security Updates"), InitialReleaseDate, CurrentReleaseDate.

    The ID is the GUID, so a release that is revised, or loses "Early" from its title when the
    month's main release ships, stays the one event. Only the newest MSRC_RELEASES_KEPT
    releases are returned, by first release date.
    """
    releases = data.get("value")
    if not isinstance(releases, list):
        return []

    now = datetime.now(UTC)
    items: list[RawItem] = []
    for release in releases:
        if not isinstance(release, dict):
            continue
        release_id = str(release.get("ID") or "").strip()
        document_title = str(release.get("DocumentTitle") or "").strip()
        published = parse_date(release.get("InitialReleaseDate") or "")
        if not release_id or not document_title or published is None:
            continue

        # Name the vendor: "October 2026 Security Updates" says nothing about whose.
        title = (
            document_title
            if document_title.lower().startswith("microsoft")
            else f"Microsoft {document_title}"
        )
        canonical = f"{release_id}|{title}".encode()
        items.append(
            RawItem(
                source_id=source.id,
                url=f"{MSRC_RELEASE_NOTE_URL}{release_id}",
                guid=release_id,
                title=title,
                raw_summary=None,
                published=published,
                fetched_at=now,
                payload_hash=hashlib.sha256(canonical).hexdigest(),
            )
        )

    items.sort(key=lambda item: item.published, reverse=True)
    return items[:MSRC_RELEASES_KEPT]


# Registry of parser name -> callable
JSON_PARSERS: dict[str, Callable[[SourceConfig, dict], list[RawItem]]] = {
    "kev": kev_parser,
    # The name config/sources.yaml uses for the CISA KEV source.
    "json_kev": kev_parser,
    "msrc_cvrf": msrc_cvrf_parser,
}


def parse_json_api(source: SourceConfig, body: bytes) -> list[RawItem]:
    """
    Parse JSON API response using registered parser.
    
    - Looks up parser by source.parser name in JSON_PARSERS registry
    - Raises KeyError if parser not registered (configuration error)
    - Returns empty list on malformed JSON (data error — silent)
    - Lets parser exceptions propagate (real bugs must surface)
    
    Args:
        source: SourceConfig specifying parser name
        body: Raw JSON bytes
    
    Returns:
        List of RawItem objects, or empty list on malformed JSON
    
    Raises:
        KeyError: If parser name not in registry (configuration error)
        Exception: Any exception raised by the parser function (real bugs)
    """
    # Raise KeyError if parser not registered (configuration error)
    if source.parser not in JSON_PARSERS:
        raise KeyError(f"no JSON parser named '{source.parser}'")
    
    # Parse JSON; return empty list only on malformed JSON (data error)
    try:
        data = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return []
    
    # Call the parser; let its exceptions propagate (real bugs, configuration errors)
    parser = JSON_PARSERS[source.parser]
    return parser(source, data)
