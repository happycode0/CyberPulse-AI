"""Scoring: urgency, confidence, novelty and prominence for an event.

Severity and prominence stay separate. Corroboration counts independent lineages, never
raw sources. Freshness decays from `last_material_update` only, so a story repeated by
other outlets (`last_seen`) never refreshes prominence.

    urgency    = (severity weight + KEV bonus) / (max weight + KEV bonus)
    confidence = best evidence-class weight, lifted toward 1.0 by each extra independent lineage
    novelty    = 0.5 ** (hours since first_seen / novelty half-life)
    freshness  = 0.5 ** (hours since last_material_update / severity half-life)
    prominence = freshness * (w_sev*urgency + w_corr*corroboration + w_au*au + w_nov*novelty)
"""

from datetime import datetime
from pathlib import Path
from typing import Self

import yaml
from pydantic import BaseModel, ConfigDict, PositiveFloat, model_validator

from worker.models import Event, EvidenceClass, Risk, Severity
from worker.version import SCORING_VERSION

# The scored result is an `Event` with `risk` and `scoring_version` populated.
ScoredEvent = Event


class ProminenceWeights(BaseModel):
    model_config = ConfigDict(extra="forbid")

    severity: float
    corroboration: float
    au_relevance: float
    novelty: float

    @model_validator(mode="after")
    def _sum_to_one(self) -> Self:
        total = self.severity + self.corroboration + self.au_relevance + self.novelty
        if any(w < 0 for w in self.model_dump().values()) or abs(total - 1.0) > 1e-9:
            raise ValueError(f"prominence_weights must be non-negative and sum to 1, got {total}")
        return self


class ScoringConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: str
    severity_weights: dict[Severity, float]
    kev_bonus: float
    half_life_hours: dict[Severity, PositiveFloat]
    evidence_class_weights: dict[EvidenceClass, float]
    corroboration_gain: float
    corroboration_cap: PositiveFloat
    novelty_half_life_hours: PositiveFloat
    prominence_weights: ProminenceWeights

    @model_validator(mode="after")
    def _complete_and_consistent(self) -> Self:
        for name in ("severity_weights", "half_life_hours"):
            missing = set(Severity) - set(getattr(self, name))
            if missing:
                raise ValueError(f"{name} missing: {sorted(m.value for m in missing)}")
        missing_ev = set(EvidenceClass) - set(self.evidence_class_weights)
        if missing_ev:
            raise ValueError(f"evidence_class_weights missing: {sorted(m.value for m in missing_ev)}")
        if any(not 0.0 <= w <= 1.0 for w in self.evidence_class_weights.values()):
            raise ValueError("evidence_class_weights must be within [0, 1]")
        if not 0.0 <= self.corroboration_gain <= 1.0:
            raise ValueError("corroboration_gain must be within [0, 1]")
        if any(w <= 0 for w in self.severity_weights.values()) or self.kev_bonus < 0:
            raise ValueError("severity weights must be positive and kev_bonus non-negative")
        if self.version != SCORING_VERSION:
            raise ValueError(
                f"scoring config version {self.version!r} != SCORING_VERSION {SCORING_VERSION!r}"
            )
        return self

    @classmethod
    def load(cls, path: Path) -> "ScoringConfig":
        with open(path) as f:
            return cls.model_validate(yaml.safe_load(f))


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, value))


def _hours_since(then: datetime, now: datetime) -> float:
    """Elapsed hours, floored at zero so a future timestamp cannot boost a score."""
    return max(0.0, (now - then).total_seconds() / 3600.0)


def base_weight(severity: Severity, config: ScoringConfig) -> float:
    return config.severity_weights[severity]


def independent_confirmations(event: Event) -> int:
    """Distinct lineages among independent sources; syndicated copies share one lineage.

    A source with no `lineage_id` is its own lineage (keyed by `source_id`).
    """
    return len(
        {
            s.lineage_id if s.lineage_id is not None else f"source:{s.source_id}"
            for s in event.sources
            if s.independent
        }
    )


def freshness(event: Event, config: ScoringConfig, now: datetime) -> float:
    """Exponential decay from `last_material_update`; never from `last_seen`.

    An event with no material update yet decays from `first_seen`.
    """
    anchor = event.last_material_update or event.first_seen
    half_life = config.half_life_hours[event.severity]
    return 0.5 ** (_hours_since(anchor, now) / half_life)


def _urgency(event: Event, config: ScoringConfig) -> float:
    kev = any(c.kev.listed for c in event.cves)
    bonus = config.kev_bonus if kev else 0.0
    ceiling = max(config.severity_weights.values()) + config.kev_bonus
    return _clamp((base_weight(event.severity, config) + bonus) / ceiling)


def _confidence(event: Event, config: ScoringConfig) -> float:
    weights = config.evidence_class_weights
    best = max(
        (weights[s.evidence_class] for s in event.sources),
        default=weights[EvidenceClass.AI_INFERENCE],
    )
    extra = max(independent_confirmations(event) - 1, 0)
    return _clamp(best + (1.0 - best) * (1.0 - (1.0 - config.corroboration_gain) ** extra))


def _novelty(event: Event, config: ScoringConfig, now: datetime) -> float:
    return 0.5 ** (_hours_since(event.first_seen, now) / config.novelty_half_life_hours)


def score_event(event: Event, config: ScoringConfig, *, now: datetime) -> ScoredEvent:
    """Return a copy of `event` with `risk` and `scoring_version` populated."""
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")

    urgency = _urgency(event, config)
    novelty = _novelty(event, config, now)
    corroboration = min(independent_confirmations(event) / config.corroboration_cap, 1.0)
    au = event.au.relevance or 0.0
    w = config.prominence_weights
    intensity = (
        w.severity * urgency
        + w.corroboration * corroboration
        + w.au_relevance * au
        + w.novelty * novelty
    )
    risk = Risk(
        urgency=urgency,
        confidence=_confidence(event, config),
        novelty=_clamp(novelty),
        prominence=_clamp(freshness(event, config, now) * intensity),
    )
    return event.model_copy(update={"risk": risk, "scoring_version": config.version})
