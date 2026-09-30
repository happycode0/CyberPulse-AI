"""Normalisation of RawItem to NormalisedItem: canonical URLs, titles, CVE extraction, and tokenisation."""

import hashlib
import re
import string
from datetime import datetime
from typing import Sequence
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse

from worker.models import RawItem, NormalisedItem

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
    "the", "to", "was", "will", "with", "the", "this", "but", "have",
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
    
    # Reconstruct query string
    query = urlencode(filtered_params, doseq=True) if filtered_params else ""
    
    # Reconstruct URL without fragment
    return urlunparse((scheme, netloc, path, parsed.params, query, ""))


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
    Extract CVE IDs from text using pattern CVE-\d{4}-\d{1,}.
    - Case-insensitive matching
    - Returns sorted unique list in uppercase
    """
    # Pattern: CVE-YYYY-N+ (1 or more digits after year)
    pattern = r"CVE-(\d{4})-(\d{1,})"
    matches = re.findall(pattern, text, re.IGNORECASE)
    
    # Format as CVE-YYYY-NNNN and deduplicate
    cve_ids = {f"CVE-{year}-{number}" for year, number in matches}
    
    # Return sorted list
    return sorted(cve_ids)


def tokenise(title: str) -> frozenset[str]:
    """
    Tokenise a title by:
    - Splitting on whitespace
    - Lowercasing
    - Removing tokens shorter than 3 characters
    - Removing stopwords
    """
    # Split and lowercase
    tokens = title.lower().split()
    
    # Filter: length >= 3 and not in stopwords
    filtered = {t for t in tokens if len(t) >= 3 and t not in STOPWORDS}
    
    return frozenset(filtered)


def normalise(item: RawItem, *, now: datetime) -> NormalisedItem:
    """
    Normalise a RawItem to NormalisedItem:
    - Canonical and hashed URLs
    - Normalised and hashed titles
    - Extracted CVEs
    - Tokenised title
    - Handle missing/future published dates
    """
    # Canonical URL and hash
    canonical = canonical_url(item.url)
    url_hash = hashlib.sha256(canonical.encode()).hexdigest()
    
    # Normalised title and hash
    norm_title = normalise_title(item.title)
    title_hash = hashlib.sha256(norm_title.encode()).hexdigest()
    
    # Extract CVEs
    cves = extract_cves(item.title)
    if item.raw_summary:
        cves_from_summary = extract_cves(item.raw_summary)
        # Merge, dedup, sort
        cves = sorted(set(cves) | set(cves_from_summary))
    
    # Tokenise title
    tokens = tokenise(item.title)
    
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
        title=item.title,
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
