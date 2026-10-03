"""Normalisation of RawItem to NormalisedItem: canonical URLs, titles, CVE extraction, and tokenisation."""

import hashlib
import html
import re
import string
from datetime import datetime
from typing import Sequence
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse

from worker.models import RawItem, NormalisedItem, CVE_ID_PATTERN

# Tracking parameters to strip from URLs
TRACKING_PARAMS = {
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_content",
    "utm_term",
    "fbclid",
    "gclid",
    "mc_cid",
    "mc_eid",
    "ref",
}

# Stopwords to exclude from tokenisation
STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "by", "for", "from",
    "has", "he", "in", "is", "it", "its", "of", "on", "or", "that",
    "the", "to", "was", "will", "with", "this", "but", "have",
    "had", "do", "does", "did", "can", "could", "would", "should",
}


def canonical_url(url: str) -> str:
    """
    Normalise a URL by:
    - Lowercasing scheme and host only (preserve path case)
    - Removing fragment
    - Removing trailing slash from path
    - Removing tracking parameters
    """
    parsed = urlparse(url)
    
    # Lowercase scheme and host, preserve rest
    scheme = parsed.scheme.lower() if parsed.scheme else ""
    netloc = parsed.netloc.lower() if parsed.netloc else ""
    path = parsed.path
    
    # Remove trailing slash
    if path and path != "/" and path.endswith("/"):
        path = path.rstrip("/")
    
    # Parse query parameters and filter out tracking params
    params = parse_qs(parsed.query, keep_blank_values=True)
    filtered_params = {k: v for k, v in params.items() if k not in TRACKING_PARAMS}
    
    # Reconstruct query string. Note: urlencode may reorder parameters relative to input,
    # which is fine since deduplication happens via URL hash, not byte-sequence equality.
    query = urlencode(filtered_params, doseq=True) if filtered_params else ""
    
    # Reconstruct URL without fragment
    return urlunparse((scheme, netloc, path, parsed.params, query, ""))


_TAG = re.compile(r"<[^>]*>")


def clean_title(title: str) -> str:
    """The headline as plain text: tags stripped, entities decoded, whitespace collapsed.

    Some feeds put markup in the title (DTA's is a whole `<a href=...>` element) or escape it
    twice (`&amp;amp;`, which a feed parser turns into a literal `&amp;`). A title that is
    nothing but markup is kept as it came, rather than becoming empty.
    """
    text = " ".join(html.unescape(_TAG.sub(" ", title)).split())
    return text or title.strip()


def normalise_title(title: str) -> str:
    """
    Normalise a title by:
    - Casefolding
    - Removing punctuation
    - Collapsing whitespace
    """
    # Casefold for case-insensitive comparison
    text = title.casefold()
    
    # Remove punctuation
    text = text.translate(str.maketrans("", "", string.punctuation))
    
    # Collapse whitespace
    text = " ".join(text.split())
    
    return text


def extract_cves(text: str) -> list[str]:
    r"""
    Extract CVE IDs from text using pattern derived from CVE_ID_PATTERN.
    - Case-insensitive matching
    - Requires at least 4 digits in sequence number (per CVE_ID_PATTERN and DB constraint)
    - Returns sorted unique list in uppercase
    """
    # Derive pattern from CVE_ID_PATTERN by removing anchors (^ and $)
    # CVE_ID_PATTERN is "^CVE-\d{4}-\d{4,}$"; strip anchors for free-text matching
    pattern = CVE_ID_PATTERN[1:-1]
    matches = re.findall(pattern, text, re.IGNORECASE)
    
    # findall returns whole matched substrings (e.g., "cve-2026-0001"); uppercase and dedupe
    cve_ids = {match.upper() for match in matches}
    
    # Return sorted list
    return sorted(cve_ids)


def tokenise(title: str) -> frozenset[str]:
    """
    Tokenise a title by:
    - Normalising it as normalise_title does (casefold, strip punctuation, collapse whitespace)
    - Splitting on whitespace
    - Removing tokens shorter than 3 characters
    - Removing stopwords

    Idempotent on an already normalised title, so an item and a stored event title
    tokenise identically regardless of punctuation.
    """
    filtered = {
        t for t in normalise_title(title).split() if len(t) >= 3 and t not in STOPWORDS
    }
    return frozenset(filtered)


def normalise(item: RawItem, *, now: datetime) -> NormalisedItem:
    """
    Normalise a RawItem to NormalisedItem:
    - Canonical and hashed URLs
    - Plain-text, normalised and hashed titles
    - Extracted CVEs
    - Tokenised title
    - Handle missing/future published dates
    """
    # Canonical URL and hash
    canonical = canonical_url(item.url)
    url_hash = hashlib.sha256(canonical.encode()).hexdigest()
    
    # Plain-text, normalised and hashed titles
    title = clean_title(item.title)
    norm_title = normalise_title(title)
    title_hash = hashlib.sha256(norm_title.encode()).hexdigest()
    
    # Extract CVEs
    cves = extract_cves(title)
    if item.raw_summary:
        cves_from_summary = extract_cves(item.raw_summary)
        # Merge, dedup, sort
        cves = sorted(set(cves) | set(cves_from_summary))
    
    # Tokenise title
    tokens = tokenise(title)
    
    # Handle published date: missing or future → fetched_at with estimated flag
    published = item.published
    published_is_estimated = False
    
    if published is None:
        published = item.fetched_at
        published_is_estimated = True
    elif published > now:
        published = now
        published_is_estimated = True
    
    return NormalisedItem(
        source_id=item.source_id,
        url=item.url,
        canonical_url=canonical,
        guid=item.guid,
        title=title,
        normalised_title=norm_title,
        summary=item.raw_summary,
        published=published,
        fetched_at=item.fetched_at,
        cves=cves,
        url_hash=url_hash,
        title_hash=title_hash,
        tokens=tokens,
        published_is_estimated=published_is_estimated,
    )
