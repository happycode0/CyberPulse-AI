"""Deterministic event resolution: is a normalised item a new event, an update, or a duplicate?

`resolve` walks an identity ladder in cost order and stops at the first confident
answer:

1. `url_hash`       same canonical URL already recorded            -> DUPLICATE
2. `guid`           same feed GUID from the same source            -> DUPLICATE
3. `canonical_url`  same URL ignoring scheme / `www.`              -> DUPLICATE
4. `title_hash`     same normalised title, published <= 72 h       -> UPDATE_EXISTING
5. `cve_set`        the same CVEs, or a few of the event's, <= 30 d -> UPDATE_EXISTING
6. `cve+tokens`     shared CVE and token overlap >= 0.5            -> UPDATE_EXISTING
7. `tokens+date`    token overlap >= 0.75 and published <= 72 h    -> UPDATE_EXISTING

Anything left that looks similar but not similar enough is AMBIGUOUS (Stage 1 treats it
as a new event and flags it for Stage 2 adjudication); a shared CVE with unrelated titles
is RELATED_BUT_DISTINCT; otherwise NEW_EVENT.

Recurring titles ("Microsoft Patch Tuesday ...", "SANS weekly roundup") are the
false-merge hazard, at monthly *and* weekly cadence. Bare title text is therefore only
trusted over a short horizon: the `title_hash` rung matches within `PROXIMITY` (72 h),
which catches same-day and next-day re-publishes but not the next weekly instalment.
Longer-range "same developing story" matching needs corroboration beyond the title
(shared CVEs, shared CVE plus overlap, or high overlap plus date proximity). The proximity
is measured from the event's *origin* (its earliest known timestamp), never its
`last_seen`: anchoring on `last_seen` would let a recurring title chain onto an event that
each new instalment keeps alive, merging every period into one event forever.

Some headlines say nothing about their story at all: "CISA Adds Two Known Exploited
Vulnerabilities to Catalog" is the same words on a different pair of CVEs each time.
`GENERIC_TITLES` lists them, and their words never count towards a match; such a notice
joins a story only through its CVEs.

`cve_set` is the rung for stories told in different words. Two reports naming exactly the
same CVEs within 30 days are one story. A report naming a few of an event's CVEs (at most
3 of its at most 10) is that story too, if they share a word of headline, or if the report
is a generic notice. Neither applies when both already carry a report from the same
register (an AUTHORITATIVE or VENDOR source): one register's two advisories are two
advisories, however many CVEs they share.

A borderline (0.45 <= overlap < 0.75) title within `AMBIGUOUS_WINDOW` (14 days) is
flagged AMBIGUOUS rather than guessed; it never merges.

`same_story` asks the same questions of two stored events, for the consolidation pass
(worker/pipeline/correlate.py), except that two naming only different CVEs never join;
plus one more: `weighted`, headline overlap weighted by
how rare each word is, at 0.30 or more within 72 h. Rare words carry a story: "KillSec"
and "Medibank" say more than "ransomware" and "attack". It needs `WEIGHTED_MIN_HEADLINES`
headlines to weigh words by; on fewer, every word looks rare and it stays off. Nor does it
join two events only one outlet reported: that is the outlet's house style, not a story.
"""

import hashlib
import math
import re
from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from typing import ClassVar, Self

from worker.models import Event, EvidenceClass, NormalisedItem
from worker.pipeline.normalise import canonical_url, normalise_title, tokenise

PROXIMITY = timedelta(hours=72)
AMBIGUOUS_WINDOW = timedelta(days=14)
CVE_WINDOW = timedelta(days=30)

CVE_TOKEN_MIN = 0.5
HIGH_OVERLAP = 0.75
AMBIGUOUS_MIN = 0.45
WEIGHTED_MIN = 0.30
# Below this many headlines a word's weight says little (every word looks rare), so the
# weighted rung waits for the corpus to grow.
WEIGHTED_MIN_HEADLINES = 100

# `cve_set`'s subset case: a report naming at most this many CVEs ...
MAX_SUBSET_CVES = 3
# ... joins an event naming at most this many. Past that it is a roundup, not a story.
MAX_EVENT_CVES = 10

# Sources that publish one advisory per thing: two of theirs are two things.
REGISTER_CLASSES = frozenset({EvidenceClass.AUTHORITATIVE, EvidenceClass.VENDOR})

# Headlines that are the same words whatever the story. Matched against the start of the
# title, case-insensitively, so a normalised title (no punctuation) matches too.
GENERIC_TITLES = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"^CISA Adds \w+ Known Exploited Vulnerabilit(?:y|ies) to (?:the )?Catalog\b",
        r"^CISA Releases \w+ Industrial Control Systems? Advisor(?:y|ies)\b",
        r"^ISC Stormcast For\b",
        r"^Smashing Security podcast\b",
    )
)


def is_generic_title(title: str) -> bool:
    return any(p.search(title) for p in GENERIC_TITLES)


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


@dataclass(frozen=True)
class TokenWeights:
    """How much each headline word says: the inverse of how many headlines use it.

    `documents` headlines were counted, and `document_frequency` says how many of them used
    each word. A word every headline uses weighs nothing; one never seen weighs the most.
    """

    document_frequency: Mapping[str, int]
    documents: int
    _cache: dict[str, float] = field(default_factory=dict, compare=False, repr=False)

    @classmethod
    def from_titles(cls, titles: Iterable[str]) -> Self:
        df: Counter[str] = Counter()
        n = 0
        for title in titles:
            n += 1
            df.update(tokenise(title))
        return cls(df, n)

    def weight(self, token: str) -> float:
        if (w := self._cache.get(token)) is None:
            df = self.document_frequency.get(token, 0)
            w = self._cache[token] = math.log((self.documents + 1) / (df + 1))
        return w


def weighted_overlap(a: frozenset[str], b: frozenset[str], weights: TokenWeights) -> float:
    """Jaccard similarity with each word counted at its weight; 0.0 when either is empty."""
    if not a or not b:
        return 0.0
    total = sum(weights.weight(t) for t in a | b)
    return sum(weights.weight(t) for t in a & b) / total if total else 0.0


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _url_key(url: str) -> str:
    """Canonical URL with the scheme and a leading `www.` dropped."""
    key = url.split("://", 1)[-1]
    return key.removeprefix("www.")


@dataclass(frozen=True)
class StoryKeys:
    """What says two records tell the same story, as opposed to being the same article.

    Titles, hashes and tokens come from the non-generic headlines only. `generic` is whether
    the record's own title is one, `registers` the register sources (`REGISTER_CLASSES`)
    that reported it, and `outlets` every source that did (stored events only).
    """

    title_hashes: frozenset[str]
    token_sets: tuple[frozenset[str], ...]
    tokens: frozenset[str]
    cves: frozenset[str]
    anchor: datetime
    generic: bool
    registers: frozenset[str]
    outlets: frozenset[str] = frozenset()


def story_keys(
    title: str,
    source_titles: Iterable[str],
    cves: Iterable[str],
    anchor: datetime,
    registers: Iterable[str] = (),
    outlets: Iterable[str] = (),
) -> StoryKeys:
    titles = [t for t in (title, *source_titles) if t and not is_generic_title(t)]
    normalised = sorted({n for t in titles if (n := normalise_title(t))})
    token_sets = tuple(tokenise(n) for n in normalised)
    return StoryKeys(
        title_hashes=frozenset(_sha256(n) for n in normalised),
        token_sets=token_sets,
        tokens=frozenset().union(*token_sets),
        cves=frozenset(cves),
        anchor=anchor,
        generic=is_generic_title(title),
        registers=frozenset(registers),
        outlets=frozenset(outlets),
    )


@dataclass(frozen=True)
class _Keys:
    url_hashes: frozenset[str]
    url_keys: frozenset[str]
    guids: frozenset[tuple[str, str]]
    story: StoryKeys


def _keys(event: Event) -> _Keys:
    canonical_urls = {canonical_url(s.url) for s in event.sources}
    url_hashes = {_sha256(u) for u in canonical_urls}
    guids: frozenset[tuple[str, str]] = frozenset()
    titles: list[str] = []
    if isinstance(event, EventCandidate):
        canonical_urls |= event.source_canonical_urls
        url_hashes |= event.source_url_hashes
        guids = event.source_guids
        titles.extend(event.source_titles)

    # The origin is the earliest timestamp we know: a backfilled source may pre-date
    # `first_seen`. Never `last_seen` (see module docstring).
    anchor = min([event.first_seen, *(s.published for s in event.sources if s.published)])
    registers = {s.source_id for s in event.sources if s.evidence_class in REGISTER_CLASSES}
    return _Keys(
        url_hashes=frozenset(url_hashes),
        url_keys=frozenset(_url_key(u) for u in canonical_urls),
        guids=guids,
        story=story_keys(event.title, titles, (c.id for c in event.cves), anchor, registers),
    )


def _within(when: datetime, anchor: datetime, limit: timedelta) -> bool:
    return abs(when - anchor) <= limit


def _overlap(a: StoryKeys, b: StoryKeys) -> float:
    return max((token_overlap(x, y) for x in a.token_sets for y in b.token_sets), default=0.0)


# ─── The rungs: each a score when it matches, else None ─────────────────────────────────────


def _title_hash(a: StoryKeys, b: StoryKeys) -> float | None:
    if a.title_hashes & b.title_hashes and _within(a.anchor, b.anchor, PROXIMITY):
        return 1.0
    return None


def _cve_set(a: StoryKeys, b: StoryKeys) -> float | None:
    if not a.cves or not b.cves or a.registers & b.registers:
        return None
    if not _within(a.anchor, b.anchor, CVE_WINDOW):
        return None
    if a.cves == b.cves:
        return 1.0
    small, large = (a, b) if len(a.cves) < len(b.cves) else (b, a)
    if not small.cves < large.cves or large.generic:
        return None
    if len(small.cves) > MAX_SUBSET_CVES or len(large.cves) > MAX_EVENT_CVES:
        return None
    if not small.generic and not any(not t.isdigit() for t in small.tokens & large.tokens):
        return None
    return len(small.cves) / len(large.cves)


def _cve_tokens(a: StoryKeys, b: StoryKeys) -> float | None:
    if not a.cves & b.cves:
        return None
    score = _overlap(a, b)
    return score if score >= CVE_TOKEN_MIN else None


def _tokens_date(a: StoryKeys, b: StoryKeys) -> float | None:
    score = _overlap(a, b)
    return score if score >= HIGH_OVERLAP and _within(a.anchor, b.anchor, PROXIMITY) else None


def _weighted(a: StoryKeys, b: StoryKeys, weights: TokenWeights) -> float | None:
    if weights.documents < WEIGHTED_MIN_HEADLINES:
        return None
    # One outlet's two articles share its house style ("Critical X Under Active
    # Exploitation"); weighted words alone do not make them one story.
    if a.registers & b.registers or len(a.outlets | b.outlets) == 1:
        return None
    if not _within(a.anchor, b.anchor, PROXIMITY) or not a.tokens & b.tokens:
        return None
    score = max(
        (weighted_overlap(x, y, weights) for x in a.token_sets for y in b.token_sets),
        default=0.0,
    )
    return score if score >= WEIGHTED_MIN else None


# The rungs that merge at ingest, in order.
MERGE_RUNGS: tuple[tuple[str, Callable[[StoryKeys, StoryKeys], float | None]], ...] = (
    ("title_hash", _title_hash),
    ("cve_set", _cve_set),
    ("cve+tokens", _cve_tokens),
    ("tokens+date", _tokens_date),
)
# Every rung `same_story` uses, in order; the position is the rung's priority.
STORY_RUNGS = (*(m for m, _ in MERGE_RUNGS), "weighted")


def same_story(a: StoryKeys, b: StoryKeys, weights: TokenWeights) -> tuple[str, float] | None:
    """The first rung on which two stored records are one story, and its score; else None.

    Two stored events that both name CVEs, none of them shared, are two stories whatever
    their words: "The July 2026 Security Update Review" and "The July 2026 Apple Security
    Update Review" are Microsoft's month and Apple's.
    """
    if a.cves and b.cves and not a.cves & b.cves:
        return None
    for method, rung in MERGE_RUNGS:
        if (score := rung(a, b)) is not None:
            return method, score
    if (score := _weighted(a, b, weights)) is not None:
        return "weighted", score
    return None


def _best(hits: list[tuple[Event, float]]) -> tuple[Event, float]:
    """Highest score; ties go to the oldest event, then the lowest id (deterministic)."""
    return min(hits, key=lambda h: (-h[1], h[0].first_seen, h[0].event_id))


def resolve(
    item: NormalisedItem,
    candidates: list[Event],
    *,
    evidence_class: EvidenceClass | None = None,
) -> Resolution:
    """`evidence_class` is the item's source's; a register's item never joins an event that
    register already reported on its CVEs alone."""
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

    registers = [item.source_id] if evidence_class in REGISTER_CLASSES else []
    mine = story_keys(item.title, (), item.cves, when, registers)
    for method, rung in MERGE_RUNGS:
        hits = [(c, s) for c, k in keyed if (s := rung(mine, k.story)) is not None]
        if hits:
            event, score = _best(hits)
            return Resolution(Decision.UPDATE_EXISTING, event.event_id, method, score)

    scored = [(c, k.story, _overlap(mine, k.story)) for c, k in keyed]

    # Similar, but not enough to merge on: a judgement call for Stage 2. Overlap at or
    # above HIGH_OVERLAP that failed the 72 h proximity test is a recurrence, not a doubt.
    hits = [
        (c, s)
        for c, k, s in scored
        if AMBIGUOUS_MIN <= s < HIGH_OVERLAP and _within(when, k.anchor, AMBIGUOUS_WINDOW)
    ]
    if hits:
        event, score = _best(hits)
        return Resolution(Decision.AMBIGUOUS, event.event_id, "tokens", score)

    hits = [(c, s) for c, k, s in scored if mine.cves & k.cves]
    if hits:
        event, score = _best(hits)
        return Resolution(Decision.RELATED_BUT_DISTINCT, event.event_id, "cve", score)

    return Resolution(Decision.NEW_EVENT, None, "none", 0.0)
