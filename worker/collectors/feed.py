"""RSS/Atom feed parser with lenient date handling."""

import hashlib
from datetime import datetime
from typing import Any

import feedparser
from zoneinfo import ZoneInfo

from worker.collectors.dates import parse_date
from worker.models import RawItem, SourceConfig

UTC = ZoneInfo("UTC")


def parse_feed(source: SourceConfig, body: bytes) -> list[RawItem]:
    """
    Parse RSS/Atom feed, extracting items as RawItem objects.
    
    - Tolerates malformed XML via feedparser's bozo mode
    - Returns empty list on fatal parse error
    - Prefers id/guid for item identity; falls back to link
    - Sets payload_hash as sha256 of canonicalised entry
    - Always returns timezone-aware published dates (or None if unparseable)
    
    Args:
        source: SourceConfig for this feed
        body: Raw feed bytes
    
    Returns:
        List of RawItem objects, or empty list on error
    """
    if not body:
        return []
    
    try:
        feed = feedparser.parse(body)
    except Exception:
        return []
    
    # feedparser.bozo is True if the feed is malformed but partially parseable
    # bozo_exception is the exception, if any
    # We tolerate bozo feeds if any entries were extracted
    
    items: list[RawItem] = []
    entries = feed.get("entries", [])
    
    for entry in entries:
        try:
            # Extract identity: prefer id/guid, fallback to link
            item_id = entry.get("id") or entry.get("guid") or entry.get("link")
            if not item_id:
                # Skip entries with no identity
                continue
            
            # Extract URL: prefer link, fallback to id
            url = entry.get("link") or entry.get("id") or item_id
            
            # Extract title
            title = entry.get("title", "").strip()
            if not title:
                continue
            
            # Extract summary
            summary = entry.get("summary") or entry.get("description")
            if summary:
                summary = summary.strip()
            
            # Parse published date
            published_str = (
                entry.get("published") or 
                entry.get("pubDate") or 
                entry.get("updated")
            )
            published = parse_date(published_str) if published_str else None
            
            # Current fetch time (all items in this batch fetched now)
            now = datetime.now(UTC)
            
            # Compute payload_hash: sha256 of canonicalised entry
            # Use a deterministic representation: title + url + summary
            canonical = f"{title}|{url}|{summary or ''}".encode("utf-8")
            payload_hash = hashlib.sha256(canonical).hexdigest()
            
            item = RawItem(
                source_id=source.id,
                url=url,
                guid=item_id if item_id != url else None,  # Only set guid if different from url
                title=title,
                raw_summary=summary,
                published=published,
                fetched_at=now,
                payload_hash=payload_hash
            )
            items.append(item)
        except Exception:
            # Skip malformed entries
            continue
    
    return items
