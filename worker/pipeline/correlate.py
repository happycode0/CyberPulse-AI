"""Consolidation: find stored events that are one story (PLAN.md §9, Stage 3).

Ingest decides one item at a time, against the events already stored. A story whose first two
reports share no headline words, or whose CVEs only appear in a later report, starts two events
that nothing at ingest will join again. This pass looks at the recent events side by side and
asks `same_story` of each plausible pair: those sharing a CVE, and those within 72 hours of each
other that share a headline word.

The pairs that match are joined into groups. A group's winner is the event to keep: one already
enriched (its summary was paid for), then the most prominent (the one on the site now), then
the oldest, then the lowest id. If the winner's title is a generic notice, the group's first
real headline replaces it.

Everything here is pure; worker/db/merge.py loads the records and carries a plan out.
"""

from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from itertools import combinations

from worker.pipeline.resolve import (
    PROXIMITY,
    STORY_RUNGS,
    StoryKeys,
    TokenWeights,
    is_generic_title,
    same_story,
    weighted_overlap,
)

# `--check-duplicates` counts a pair as a possible duplicate at this weighted overlap, below
# the merge threshold, so the report shows what the pass leaves alone as well as what it joins.
POSSIBLE_DUPLICATE_MIN = 0.20


@dataclass(frozen=True)
class StoryRecord:
    event_id: str
    title: str
    first_seen: datetime
    enriched: bool
    prominence: float | None
    keys: StoryKeys


@dataclass(frozen=True)
class MergeGroup:
    winner: str
    losers: tuple[str, ...]
    # The winner's new title, when its own is a generic notice; None keeps it.
    title: str | None
    # The rungs that joined the group, strongest first (for the log).
    methods: tuple[str, ...]


def candidate_pairs(records: Sequence[StoryRecord]) -> set[tuple[int, int]]:
    """Index pairs (i < j) worth asking about: a shared CVE, or 72 h apart at most with a
    headline word in common. Every pair `same_story` could join is among them."""
    pairs: set[tuple[int, int]] = set()
    by_cve: dict[str, list[int]] = defaultdict(list)
    for i, r in enumerate(records):
        for cve in r.keys.cves:
            by_cve[cve].append(i)
    for members in by_cve.values():
        pairs.update(combinations(members, 2))

    order = sorted(range(len(records)), key=lambda i: records[i].keys.anchor)
    for n, i in enumerate(order):
        a = records[i].keys
        for j in order[n + 1 :]:
            b = records[j].keys
            if b.anchor - a.anchor > PROXIMITY:
                break
            if a.tokens & b.tokens:
                pairs.add((min(i, j), max(i, j)))
    return pairs


def _rank(r: StoryRecord) -> tuple[bool, float, datetime, str]:
    return (not r.enriched, -(r.prominence or 0.0), r.first_seen, r.event_id)


def plan_merges(records: Sequence[StoryRecord], weights: TokenWeights) -> list[MergeGroup]:
    """The groups of two or more records that are one story, ordered by winner id."""
    edges: list[tuple[int, float, int, int, str]] = []
    for i, j in candidate_pairs(records):
        verdict = same_story(records[i].keys, records[j].keys, weights)
        if verdict is not None:
            method, score = verdict
            edges.append((STORY_RUNGS.index(method), -score, i, j, method))
    edges.sort()

    parent = list(range(len(records)))

    def root(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    methods: dict[int, list[str]] = defaultdict(list)
    for _, _, i, j, method in edges:
        ri, rj = root(i), root(j)
        if ri != rj:
            parent[rj] = ri
            methods[ri].extend(methods.pop(rj, []))
        methods[ri].append(method)

    members: dict[int, list[StoryRecord]] = defaultdict(list)
    for i, r in enumerate(records):
        members[root(i)].append(r)

    groups = []
    for top, rs in members.items():
        if len(rs) < 2:
            continue
        ranked = sorted(rs, key=_rank)
        winner = ranked[0]
        title = None
        if is_generic_title(winner.title):
            title = next((r.title for r in ranked if not is_generic_title(r.title)), None)
        used = sorted(set(methods[top]), key=STORY_RUNGS.index)
        groups.append(
            MergeGroup(
                winner=winner.event_id,
                losers=tuple(r.event_id for r in ranked[1:]),
                title=title,
                methods=tuple(used),
            )
        )
    return sorted(groups, key=lambda g: g.winner)


def after_merges(records: Sequence[StoryRecord], groups: Sequence[MergeGroup]) -> list[StoryRecord]:
    """The records as they would be once `groups` are merged: each winner carrying its group's
    keys, the losers gone. For `--check-duplicates`, which reports the rate both ways."""
    by_id = {r.event_id: r for r in records}
    losers = {e for g in groups for e in g.losers}
    joined: dict[str, StoryRecord] = {}
    for g in groups:
        members = [by_id[g.winner], *(by_id[e] for e in g.losers)]
        keys = [m.keys for m in members]
        winner = by_id[g.winner]
        title = g.title or winner.title
        joined[g.winner] = replace(
            winner,
            title=title,
            first_seen=min(m.first_seen for m in members),
            keys=StoryKeys(
                title_hashes=frozenset().union(*(k.title_hashes for k in keys)),
                token_sets=tuple(sorted({t for k in keys for t in k.token_sets}, key=sorted)),
                tokens=frozenset().union(*(k.tokens for k in keys)),
                cves=frozenset().union(*(k.cves for k in keys)),
                anchor=min(k.anchor for k in keys),
                generic=is_generic_title(title),
                registers=frozenset().union(*(k.registers for k in keys)),
                outlets=frozenset().union(*(k.outlets for k in keys)),
            ),
        )
    return [joined.get(r.event_id, r) for r in records if r.event_id not in losers]


@dataclass(frozen=True)
class DuplicateReport:
    live: int
    in_pairs: int  # live events in at least one possible-duplicate pair
    pairs: list[tuple[str, str, str, float]]  # (event, event, why, score)

    @property
    def rate(self) -> float:
        return self.in_pairs / self.live if self.live else 0.0


def possible_duplicates(
    records: Sequence[StoryRecord], live: Iterable[str], weights: TokenWeights
) -> DuplicateReport:
    """Pairs of live events that may be one story: `same_story` says so, they share a CVE, or
    their headlines overlap (weighted) at `POSSIBLE_DUPLICATE_MIN` within 72 h. One register's
    two items are never counted: they are two items by definition.

    This is the measure for the duplicate rate, deliberately looser than the merge rules: a
    shared CVE alone counts, though two stories may well name one CVE.
    """
    live_ids = set(live)
    shown = [r for r in records if r.event_id in live_ids]
    pairs: list[tuple[str, str, str, float]] = []
    for i, j in sorted(candidate_pairs(shown)):
        a, b = shown[i], shown[j]
        if a.keys.registers & b.keys.registers:
            continue
        verdict = same_story(a.keys, b.keys, weights)
        if verdict is None and a.keys.cves & b.keys.cves:
            verdict = ("shared CVE", 0.0)
        if verdict is None and abs(a.keys.anchor - b.keys.anchor) <= PROXIMITY:
            score = max(
                (
                    weighted_overlap(x, y, weights)
                    for x in a.keys.token_sets
                    for y in b.keys.token_sets
                ),
                default=0.0,
            )
            if score >= POSSIBLE_DUPLICATE_MIN:
                verdict = ("similar headline", score)
        if verdict is not None:
            pairs.append((a.event_id, b.event_id, *verdict))
    in_pairs = {e for p in pairs for e in p[:2]}
    return DuplicateReport(live=len(shown), in_pairs=len(in_pairs), pairs=pairs)
