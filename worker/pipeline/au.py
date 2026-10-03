"""AU relevance from the record's facts (PLAN.md §4, DECKARD's AU desk; §5, "Scoring").

Four facts each set a floor under an event's AU relevance and give a reason in fixed words: an
Australian government authority published it, the record names an Australian organisation, it
names Australia, an Australian place or a .au web address, or an Australian outlet reported it.
The model's reading (the brief task's `au`) may lift the number above the floors, never lower
it, and its reasons follow the facts'. With neither, relevance stays null: unrated, not zero.

Matching reads what the sources said (titles, the feed's own text, other sources' headlines),
never the model's summary, so no model can make an event Australian by writing the word. It is
case-sensitive and on whole words, which keeps "Coles" from matching "coles" and "NAB" from
matching "unable". The lists live in config/scoring.yaml under `au:`.

SOCI sectors come from the event's sectors through a fixed map, and only for an event relevant
enough to sit on the Australian desk: a global story about hospitals is not a SOCI matter.
"""

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from functools import cached_property
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from worker.models import AuRelevance
from worker.pipeline.assemble import AU_SOURCE_REASON

_Unit = Field(ge=0.0, le=1.0)

# "The post <title> appeared first on <outlet>." ends many WordPress feeds' text, and an
# Australian outlet's name would otherwise make every one of its posts "name Australia".
_BOILERPLATE = re.compile(r"\bappeared first on\b.*$", re.IGNORECASE | re.DOTALL)
# A host name under .au, e.g. cyber.gov.au or example.com.au, but not a word ending in "au".
_AU_HOST = re.compile(r"(?<![\w.-])(?:[A-Za-z0-9-]+\.)+au(?![\w-])(?!\.[A-Za-z0-9])")

_MAX_NAMED = 3


class AuFloors(BaseModel):
    model_config = ConfigDict(extra="forbid")

    authority: float = _Unit
    organisation: float = _Unit
    place: float = _Unit
    outlet: float = _Unit


class AuConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    floors: AuFloors
    max_reasons: int = Field(ge=1)
    soci_min_relevance: float = _Unit
    authorities: list[str]
    country: list[str]
    places: list[str]
    organisations: dict[str, list[str]]
    soci_sectors: dict[str, str]

    @model_validator(mode="after")
    def _names_are_words(self) -> Self:
        for name in [*self.country, *self.places, *self.organisations]:
            if not name.strip() or name != name.strip():
                raise ValueError(f"au: {name!r} is not a usable name")
        return self

    @cached_property
    def country_pattern(self) -> re.Pattern[str]:
        return _words(self.country)

    @cached_property
    def place_pattern(self) -> re.Pattern[str]:
        return _words(self.places)

    @cached_property
    def organisation_pattern(self) -> re.Pattern[str]:
        return _words(self.organisations)


def _words(names: Iterable[str]) -> re.Pattern[str]:
    """One pattern for whole-word, case-sensitive matches; longest names first, so "New South
    Wales" is found as itself rather than as nothing."""
    ordered = sorted(set(names), key=lambda n: (-len(n), n))
    if not ordered:
        return re.compile(r"(?!)")
    return re.compile(r"(?<!\w)(?:" + "|".join(map(re.escape, ordered)) + r")(?!\w)")


@dataclass(frozen=True)
class AuSource:
    source_id: str
    name: str
    region: str


@dataclass(frozen=True)
class ModelReading:
    """What the brief task said (migration 008's `au_model_*` columns)."""

    relevance: float | None = None
    reasons: tuple[str, ...] = ()
    sectors: tuple[str, ...] = ()


@dataclass(frozen=True)
class AuFacts:
    texts: tuple[str, ...]  # the title, the feed's own text, the other sources' headlines
    sources: tuple[AuSource, ...]
    model: ModelReading = ModelReading()


def _found(pattern: re.Pattern[str], texts: Sequence[str]) -> list[str]:
    return list(dict.fromkeys(m.group(0) for t in texts for m in pattern.finditer(t)))


def _named(things: Sequence[str]) -> str:
    shown = ", ".join(things[:_MAX_NAMED])
    return shown + (f" and {len(things) - _MAX_NAMED} more" if len(things) > _MAX_NAMED else "")


def _clean(texts: Iterable[str], sources: Iterable[AuSource]) -> list[str]:
    """The record's text without feed boilerplate or its own sources' names: "Australian
    Security Magazine" reporting a story says nothing about where the story happened."""
    names = sorted({s.name for s in sources if s.name}, key=len, reverse=True)
    out = []
    for text in texts:
        text = _BOILERPLATE.sub(" ", text or "")
        for name in names:
            text = text.replace(name, " ")
        out.append(text)
    return out


def assess_au(facts: AuFacts, config: AuConfig) -> AuRelevance:
    """The event's AU relevance, reasons, sectors and SOCI sectors."""
    floors = config.floors
    texts = _clean(facts.texts, facts.sources)
    reported = [s for s in facts.sources if s.region.casefold() == "au"]
    authority_ids = set(config.authorities)
    authorities = list(dict.fromkeys(s.name for s in facts.sources if s.source_id in authority_ids))
    organisations = _found(config.organisation_pattern, texts)
    country = _found(config.country_pattern, texts)
    places = _found(config.place_pattern, texts)
    hosts = list(dict.fromkeys(m.group(0).lower() for t in texts for m in _AU_HOST.finditer(t)))

    found: list[tuple[float, str]] = []
    if authorities:
        reason = f"published by an Australian government authority ({_named(authorities)})"
        found.append((floors.authority, reason))
    if organisations:
        one = len(organisations) == 1
        which = "an Australian organisation" if one else "Australian organisations"
        found.append((floors.organisation, f"names {which} ({_named(organisations)})"))
    if country:
        found.append((floors.place, "names Australia"))
    elif places:
        which = "an Australian place" if len(places) == 1 else "Australian places"
        found.append((floors.place, f"names {which} ({_named(places)})"))
    if hosts:
        found.append((floors.place, f"names an Australian (.au) web address ({_named(hosts)})"))
    if reported and not authorities:
        found.append((floors.outlet, AU_SOURCE_REASON))

    model = facts.model
    scores = [f for f, _ in found] + ([model.relevance] if model.relevance is not None else [])
    relevance = max(scores) if scores else None

    own = {r.casefold() for _, r in found} | {AU_SOURCE_REASON.casefold()}
    claimed = [r for r in model.reasons if r.casefold() not in own]
    reasons = list(dict.fromkeys([*(r for _, r in found), *claimed]))[: config.max_reasons]

    sectors = list(
        dict.fromkeys(
            [*model.sectors, *(s for o in organisations for s in config.organisations[o])]
        )
    )
    soci: list[str] = []
    if relevance is not None and relevance >= config.soci_min_relevance:
        soci = list(
            dict.fromkeys(config.soci_sectors[s] for s in sectors if s in config.soci_sectors)
        )

    return AuRelevance(
        relevance=relevance,
        directly_reported_in_au=bool(reported),
        reasons=reasons,
        sectors=sectors,
        soci_asset_classes=soci,
    )
