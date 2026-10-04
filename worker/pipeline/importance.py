"""Event importance: how much a published event matters, as a whole number 0-100 and a tier
(docs/wiki/importance-and-reputation.md).

Six parts, added up and capped at 100, each from the event's facts as published:

- **Standing** (up to 25): the best reputation among its sources (worker/pipeline/reputation.py),
  a quarter of it.
- **Australia** (up to 25): a step from its Australian relevance (some, relevant, strong);
  reported in Australia lifts it one step, to at least relevant.
- **Public sector** (up to 15): a government body, or a critical sector, is the target of a
  threat; less if it is only involved.
- **Harm** (up to 25): its severity, or that it is being exploited, or that it is an incident
  whose severity no register has scored; on the AI desk alone, its significance.
- **Convergence** (5): it is on both desks, cyber and AI.
- **Corroboration** (up to 10): independent sources (worker/pipeline/lineage.py) reporting it.

`key` is `KEY_AT` and over, `notable` is `NOTABLE_AT` and over, the rest `routine`; an event on
neither desk is `routine` whatever it adds up to. The reasons are the largest parts in a few
words each, largest first. Severity and AI significance may be the model's estimate; nothing
here calls a model, and an event the model never read is scored on what is known.

Computed at publish time (worker/publish/build.py) and never stored, so changing a weight here
re-rates every published event at the next publish. Bump `IMPORTANCE_VERSION` when one moves.
"""

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass

from worker.models import (
    Beat,
    Event,
    Importance,
    ImportanceTier,
    Severity,
    SeveritySource,
    Standing,
)
from worker.pipeline.reputation import DEFAULT_STANDING, STANDING_BASE
from worker.pipeline.score import independent_confirmations

IMPORTANCE_VERSION = "1"

KEY_AT = 60
NOTABLE_AT = 40
MAX_REASONS = 5

STANDING_SHARE = 0.25  # of the best reputation: 25 at most

# Australian relevance in steps; reported in Australia lifts an event one step, to at least
# AU_RELEVANT. An Australian outlet alone is relevance 0.30 (config/scoring.yaml), so a world
# story it carries is "relevant", and an Australian story it carries is "strong".
AU_STRONG = 25
AU_STRONG_RELEVANCE = 0.75
AU_RELEVANT = 15
AU_RELEVANT_RELEVANCE = 0.5
AU_SOME = 8  # any relevance over 0
_AU_STEPS = (0, AU_SOME, AU_RELEVANT, AU_STRONG)

PUBLIC_TARGET = 15  # and a threat: an attack, an incident, malware or a flaw
PUBLIC_INVOLVED = 8  # named without one: a policy, a contract, an appointment

HARM_BY_SEVERITY = {Severity.CRITICAL: 25, Severity.HIGH: 18, Severity.MEDIUM: 8}
HARM_EXPLOITED = 20  # at least this for a KEV-listed CVE, or a zero-day or exploitation story
HARM_INCIDENT = 15  # an incident that no register has given a severity
HARM_BY_SIGNIFICANCE = {"major": 22, "notable": 12}  # the AI desk's scale, beat ai only

CONVERGENCE = 5

# Calibrated on the live page of 2026-10-04: at 5 and 10, a zero-day three outlets reported
# fell just short of key.
CORROBORATION_TWO = 6
CORROBORATION_MORE = 12  # three or more independent sources

# Triage found the event on neither desk (Beat.OTHER): whatever it adds up to, it is routine.
OFF_DESK_CAP = NOTABLE_AT - 1

# Categories that describe harm already done to someone (worker/ai/tasks.py CATEGORIES).
INCIDENT_CATEGORIES = {
    "data-breach": "data breach",
    "ransomware": "ransomware attack",
    "ddos": "denial-of-service attack",
    "espionage": "espionage",
    "supply-chain": "supply-chain attack",
    "fraud": "fraud",
    "ai-incident": "AI incident",
}
EXPLOITED_CATEGORIES = {
    "active-exploitation": "actively exploited",
    "zero-day": "exploited as a zero-day",
}
# Categories that make a government or sector the target rather than the subject.
THREAT_CATEGORIES = {
    *INCIDENT_CATEGORIES, *EXPLOITED_CATEGORIES, "malware", "phishing", "emerging-threat",
    "vulnerability",
}

# Sectors (worker/ai/tasks.py SECTORS) that are public sector or critical infrastructure.
GOVERNMENT_SECTORS = ("government", "defence")
CRITICAL_SECTORS = (
    "critical-infrastructure", "energy", "water", "health", "telecommunications", "transport",
    "finance", "education",
)
# A government body named in a headline or among the organisations: "NSW government website",
# "Department of Home Affairs", "Services Australia". Lower case, whole words.
GOVERNMENT_WORDS = re.compile(
    r"\b(?:governments?|govt|ministry|ministries|minister|department of|parliament|senate|"
    r"council|municipal|federal agency|government agency|public sector|public service|"
    r"defence force|armed forces|services australia|home affairs|national parks)\b"
)

# Register scores name their source in a reason; the model's estimate says it is one.
_SEVERITY_SOURCE = {
    SeveritySource.CNA: "CNA score",
    SeveritySource.CISA_ADP: "CISA score",
    SeveritySource.NVD: "NVD score",
    SeveritySource.VENDOR: "vendor rating",
    SeveritySource.AI_ESTIMATE: "AI estimate",
}


@dataclass(frozen=True)
class Voice:
    """What importance needs of a source: who it is, as a reader knows it, and its standing and
    reputation score."""

    label: str
    standing: Standing
    reputation: int


@dataclass(frozen=True)
class Part:
    points: int
    reason: str | None


def _round(value: float) -> int:
    return math.floor(value + 0.5)


def _standing(event: Event, voices: Mapping[str, Voice]) -> Part:
    known = [voices[s.source_id] for s in event.sources if s.source_id in voices]
    if not known:
        # A source the registry no longer lists (or an event with none): the floor of standings.
        return Part(_round(STANDING_BASE[DEFAULT_STANDING] * STANDING_SHARE), None)
    best = max(known, key=lambda v: (v.reputation, v.label))
    return Part(
        _round(best.reputation * STANDING_SHARE),
        f"reported by {best.label} ({best.standing.value})",
    )


_AU_REASONS = (
    None, "some Australian relevance", "relevant to Australia", "strongly relevant to Australia",
)


def _australia(event: Event) -> Part:
    relevance = event.au.relevance or 0.0
    if relevance >= AU_STRONG_RELEVANCE:
        step = 3
    elif relevance >= AU_RELEVANT_RELEVANCE:
        step = 2
    else:
        step = 1 if relevance > 0 else 0
    if not event.au.directly_reported_in_au or step == 3:
        return Part(_AU_STEPS[step], _AU_REASONS[step])
    if step == 2:
        return Part(AU_STRONG, "relevant to Australia, reported there")
    return Part(AU_RELEVANT, "reported in Australia")


def names_government(event: Event) -> bool:
    """A government body is involved: the sectors say so, or the headline or the organisations
    name one."""
    if any(s in GOVERNMENT_SECTORS for s in [*event.au.sectors, *event.entities.industries]):
        return True
    texts = [event.title, *event.entities.organisations]
    return any(GOVERNMENT_WORDS.search(t.lower()) for t in texts if t)


def _public_sector(event: Event) -> Part:
    # "Australian" only for an Australian story, not a world one an Australian outlet carried.
    where = "Australian " if (event.au.relevance or 0.0) >= AU_RELEVANT_RELEVANCE else ""
    if names_government(event):
        who = f"{where}government"
    elif event.au.soci_asset_classes:
        who = f"critical infrastructure ({event.au.soci_asset_classes[0]})"
    else:
        critical = [s for s in event.au.sectors if s in CRITICAL_SECTORS]
        if not critical:
            return Part(0, None)
        who = f"{where}{critical[0].replace('-', ' ')} sector"
    threat = bool(THREAT_CATEGORIES.intersection(event.categories)) or any(
        c.kev.listed for c in event.cves
    )
    if threat:
        return Part(PUBLIC_TARGET, f"{who} target")
    return Part(PUBLIC_INVOLVED, f"{who} involved")


def _exploited(event: Event) -> str | None:
    if any(c.kev.listed for c in event.cves):
        return "actively exploited (CISA KEV)"
    return next((w for c, w in EXPLOITED_CATEGORIES.items() if c in event.categories), None)


def _harm(event: Event) -> Part:
    candidates: list[Part] = []
    exploited = _exploited(event)
    if event.severity in HARM_BY_SEVERITY:
        points = HARM_BY_SEVERITY[event.severity]
        if exploited:
            # Exploitation is the fact a reader needs, even where the severity counts as much.
            reason = f"{event.severity.value} severity, {exploited}"
            points = max(points, HARM_EXPLOITED)
        else:
            source = _SEVERITY_SOURCE.get(event.severity_source)
            reason = f"{event.severity.value} severity" + (f" ({source})" if source else "")
        candidates.append(Part(points, reason))
    elif exploited:
        candidates.append(Part(HARM_EXPLOITED, exploited))
    # Unknown, or only the model's estimate: an incident is harm done whatever the guess.
    if event.severity is Severity.UNKNOWN or event.severity_source is SeveritySource.AI_ESTIMATE:
        for category in event.categories:
            if category in INCIDENT_CATEGORIES:
                candidates.append(Part(HARM_INCIDENT, INCIDENT_CATEGORIES[category]))
                break
    significance = event.ai_significance.value if event.ai_significance else None
    if event.beat is Beat.AI and significance in HARM_BY_SIGNIFICANCE:
        candidates.append(Part(HARM_BY_SIGNIFICANCE[significance], f"{significance} AI story"))
    # The first of the largest: the order above breaks ties, register facts first.
    return max(candidates, key=lambda p: p.points, default=Part(0, None))


def _convergence(event: Event) -> Part:
    return Part(CONVERGENCE, "cyber and AI") if event.beat is Beat.BOTH else Part(0, None)


def _corroboration(event: Event) -> Part:
    n = independent_confirmations(event)
    if n >= 3:
        return Part(CORROBORATION_MORE, f"{n} independent sources")
    if n == 2:
        return Part(CORROBORATION_TWO, "2 independent sources")
    return Part(0, None)


def tier_of(score: int) -> ImportanceTier:
    if score >= KEY_AT:
        return ImportanceTier.KEY
    if score >= NOTABLE_AT:
        return ImportanceTier.NOTABLE
    return ImportanceTier.ROUTINE


def assess_importance(event: Event, voices: Mapping[str, Voice]) -> Importance:
    parts = [
        _harm(event),
        _australia(event),
        _public_sector(event),
        _standing(event, voices),
        _corroboration(event),
        _convergence(event),
    ]
    score = min(100, sum(p.points for p in parts))
    # Largest first; equal parts keep the order above.
    reasons = [
        p.reason for p in sorted(parts, key=lambda p: -p.points) if p.reason and p.points
    ]
    if event.beat is Beat.OTHER and score > OFF_DESK_CAP:
        score = OFF_DESK_CAP
        reasons.insert(0, "neither cyber nor AI")
    return Importance(
        version=IMPORTANCE_VERSION,
        score=score,
        tier=tier_of(score),
        reasons=reasons[:MAX_REASONS],
    )


def with_importance(event: Event, voices: Mapping[str, Voice]) -> Event:
    return event.model_copy(update={"importance": assess_importance(event, voices)})
