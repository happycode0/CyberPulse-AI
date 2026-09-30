"""Deterministic event resolution: is a normalised item a new event, an update, or a duplicate?

`resolve` walks an identity ladder in cost order and stops at the first confident
answer:

1. `url_hash`       same canonical URL already recorded            -> DUPLICATE
2. `guid`           same feed GUID from the same source            -> DUPLICATE
3. `canonical_url`  same URL ignoring scheme / `www.`              -> DUPLICATE
4. `title_hash`     same normalised title within the window        -> UPDATE_EXISTING
5. `cve+tokens`     shared CVE and token overlap >= 0.5            -> UPDATE_EXISTING
6. `tokens+date`    token overlap >= 0.75 and published <= 72 h    -> UPDATE_EXISTING

Anything left that looks similar but not similar enough is AMBIGUOUS (Stage 1 treats it
as a new event and flags it for Stage 2 adjudication); a shared CVE with unrelated titles
is RELATED_BUT_DISTINCT; otherwise NEW_EVENT.

Recurring titles ("Microsoft Patch Tuesday ...", weekly digests) are the false-merge
hazard. Every path that relies on title text alone is gated on the item being published
within `WINDOW` of the event's *origin* (its earliest known timestamp), never of its
`last_seen`. Anchoring on `last_seen` would let a recurring title chain onto an event that
each new instalment keeps alive, merging every period into one event forever.
"""

import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import ClassVar

from worker.models import Event, NormalisedItem
from worker.pipeline.normalise import canonical_url, normalise_title, tokenise

WINDOW = timedelta(days=14)
PROXIMITY = timedelta(hours=72)

CVE_TOKEN_MIN = 0.5
HIGH_OVERLAP = 0.75
AMBIGUOUS_MIN = 0.45


class Decision(StrEnum):
    NEW_EVENT = "new_event"
    UPDATE_EXISTING = "update_existing"
    DUPLICATE = "duplicate"
    RELATED_BUT_DISTINCT = "related_but_distinct"
    AMBIGUOUS = "ambiguous"


@dataclass(frozen=True)
class Resolution:
    decision: Decision
    event_id: str | None
    method: str
    score: float


class EventCandidate(Event):
    """An `Event` carrying the per-source match keys the published shape does not have.

    `SourceRef` has no GUID or URL hash, so the database layer fills these in from
    `event_sources`. A plain `Event` is also accepted by `resolve`: its keys are then
    derived from what it does have (source URLs, title, CVEs), with no GUID matching.
    """

    INTERNAL_FIELDS: ClassVar[frozenset[str]] = frozenset(
        {"source_url_hashes", "source_canonical_urls", "source_guids", "source_titles"}
    )

    source_url_hashes: frozenset[str] = frozenset()
    source_canonical_urls: frozenset[str] = frozenset()
    # (source_id, guid): GUIDs are only unique within one feed.
    source_guids: frozenset[tuple[str, str]] = frozenset()
    # Titles of the individual source articles, alongside the event's own title.
    source_titles: tuple[str, ...] = ()


def token_overlap(a: frozenset[str], b: frozenset[str]) -> float:
    """Jaccard similarity of two token sets; 0.0 when either is empty."""
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _url_key(url: str) -> str:
    """Canonical URL with the scheme and a leading `www.` dropped."""
    key = url.split("://", 1)[-1]
    return key.removeprefix("www.")


@dataclass(frozen=True)
class _Keys:
    url_hashes: frozenset[str]
    url_keys: frozenset[str]
    guids: frozenset[tuple[str, str]]
    title_hashes: frozenset[str]
    token_sets: tuple[frozenset[str], ...]
    cves: frozenset[str]
    anchor: datetime


def _keys(event: Event) -> _Keys:
    canonical_urls = {canonical_url(s.url) for s in event.sources}
    url_hashes = {_sha256(u) for u in canonical_urls}
    guids: frozenset[tuple[str, str]] = frozenset()
    titles = [event.title]
    if isinstance(event, EventCandidate):
        canonical_urls |= event.source_canonical_urls
        url_hashes |= event.source_url_hashes
        guids = event.source_guids
        titles.extend(event.source_titles)

    normalised = {n for t in titles if (n := normalise_title(t))}
    # The origin is the earliest timestamp we know: a backfilled source may pre-date
    # `first_seen`. Never `last_seen` (see module docstring).
    anchor = min([event.first_seen, *(s.published for s in event.sources if s.published)])
    return _Keys(
        url_hashes=frozenset(url_hashes),
        url_keys=frozenset(_url_key(u) for u in canonical_urls),
        guids=guids,
        title_hashes=frozenset(_sha256(n) for n in normalised),
        token_sets=tuple(tokenise(n) for n in sorted(normalised)),
        cves=frozenset(c.id for c in event.cves),
        anchor=anchor,
    )


def _within(when: datetime, anchor: datetime, limit: timedelta) -> bool:
    return abs(when - anchor) <= limit


def _best(hits: list[tuple[Event, float]]) -> tuple[Event, float]:
    """Highest score; ties go to the oldest event, then the lowest id (deterministic)."""
    return min(hits, key=lambda h: (-h[1], h[0].first_seen, h[0].event_id))


def resolve(item: NormalisedItem, candidates: list[Event]) -> Resolution:
    keyed = [(c, _keys(c)) for c in candidates]
    when = item.published or item.fetched_at
    item_url_key = _url_key(item.canonical_url)

    for method, matches in (
        ("url_hash", lambda k: item.url_hash in k.url_hashes),
        ("guid", lambda k: item.guid is not None and (item.source_id, item.guid) in k.guids),
        ("canonical_url", lambda k: item_url_key in k.url_keys),
    ):
        hits = [(c, 1.0) for c, k in keyed if matches(k)]
        if hits:
            event, score = _best(hits)
            return Resolution(Decision.DUPLICATE, event.event_id, method, score)

    hits = [
        (c, 1.0)
        for c, k in keyed
        if item.title_hash in k.title_hashes and _within(when, k.anchor, WINDOW)
    ]
    if hits:
        event, score = _best(hits)
        return Resolution(Decision.UPDATE_EXISTING, event.event_id, "title_hash", score)

    scored = [
        (c, k, max((token_overlap(item.tokens, t) for t in k.token_sets), default=0.0))
        for c, k in keyed
    ]
    item_cves = set(item.cves)

    hits = [(c, s) for c, k, s in scored if item_cves & k.cves and s >= CVE_TOKEN_MIN]
    if hits:
        event, score = _best(hits)
        return Resolution(Decision.UPDATE_EXISTING, event.event_id, "cve+tokens", score)

    hits = [
        (c, s)
        for c, k, s in scored
        if s >= HIGH_OVERLAP and _within(when, k.anchor, PROXIMITY)
    ]
    if hits:
        event, score = _best(hits)
        return Resolution(Decision.UPDATE_EXISTING, event.event_id, "tokens+date", score)

    # Similar, but not enough to merge on: within the window, this is a judgement call
    # (includes very similar titles more than 72 h apart, e.g. a weekly digest).
    hits = [
        (c, s)
        for c, k, s in scored
        if s >= AMBIGUOUS_MIN and _within(when, k.anchor, WINDOW)
    ]
    if hits:
        event, score = _best(hits)
        return Resolution(Decision.AMBIGUOUS, event.event_id, "tokens", score)

    hits = [(c, s) for c, k, s in scored if item_cves & k.cves]
    if hits:
        event, score = _best(hits)
        return Resolution(Decision.RELATED_BUT_DISTINCT, event.event_id, "cve", score)

    return Resolution(Decision.NEW_EVENT, None, "none", 0.0)
