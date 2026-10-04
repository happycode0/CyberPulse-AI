"""Domain models for CyberPulse-AI: the event schema (PLAN.md section 5) and pipeline items."""

from collections.abc import Iterable
from datetime import date
from enum import StrEnum
from typing import Annotated, Any, ClassVar

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    computed_field,
    model_validator,
)

from worker.version import (
    PIPELINE_VERSION,
    SCHEMA_VERSION,
    SCORING_VERSION,
    UNENRICHED,
)

EVENT_ID_PATTERN = r"^evt-\d{4}-\d{6}$"
CVE_ID_PATTERN = r"^CVE-\d{4}-\d{4,}$"

EventId = Annotated[str, Field(pattern=EVENT_ID_PATTERN)]
Score = Annotated[float, Field(ge=0.0, le=1.0)]


class EventStatus(StrEnum):
    NEW = "new"
    ACTIVE = "active"
    DEVELOPING = "developing"
    MONITORING = "monitoring"
    CONTAINED = "contained"
    RESOLVED = "resolved"
    ARCHIVED = "archived"


class Severity(StrEnum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    UNKNOWN = "unknown"


class SeveritySource(StrEnum):
    CNA = "cna"
    CISA_ADP = "cisa_adp"
    NVD = "nvd"
    VENDOR = "vendor"
    AI_ESTIMATE = "ai_estimate"
    UNKNOWN = "unknown"


class EvidenceClass(StrEnum):
    PRIMARY = "PRIMARY"
    AUTHORITATIVE = "AUTHORITATIVE"
    VENDOR = "VENDOR"
    SPECIALIST = "SPECIALIST"
    NEWS = "NEWS"
    COMMUNITY = "COMMUNITY"
    SOCIAL = "SOCIAL"
    AI_INFERENCE = "AI_INFERENCE"


class MaterialChange(StrEnum):
    NEW_FACT = "NEW_FACT"
    NEW_CVE = "NEW_CVE"
    NEW_EXPLOIT = "NEW_EXPLOIT"
    EXPLOIT_CONFIRMED = "EXPLOIT_CONFIRMED"
    NEW_ACTOR = "NEW_ACTOR"
    NEW_TARGET = "NEW_TARGET"
    NEW_GEOGRAPHY = "NEW_GEOGRAPHY"
    NEW_AU_EXPOSURE = "NEW_AU_EXPOSURE"
    NEW_IMPACT = "NEW_IMPACT"
    NEW_PATCH = "NEW_PATCH"
    NEW_MITIGATION = "NEW_MITIGATION"
    NEW_EVIDENCE = "NEW_EVIDENCE"
    CORRECTION = "CORRECTION"
    NO_MATERIAL_CHANGE = "NO_MATERIAL_CHANGE"


class RelationshipType(StrEnum):
    RELATED_EVENT = "related_event"
    FOLLOW_UP_TO = "follow_up_to"
    CAUSED_BY = "caused_by"
    EXPLOITS = "exploits"
    AFFECTS = "affects"
    TARGETS = "targets"
    USES = "uses"
    ATTRIBUTED_TO = "attributed_to"
    MITIGATED_BY = "mitigated_by"
    RESOLVES = "resolves"


class AiSubdomain(StrEnum):
    AI_INDUSTRY = "AI_INDUSTRY"
    AI_SECURITY = "AI_SECURITY"
    AI_THREAT_ACTIVITY = "AI_THREAT_ACTIVITY"
    AI_CYBER_CONVERGENCE = "AI_CYBER_CONVERGENCE"


class Beat(StrEnum):
    """Which desk an event belongs to (docs/wiki/ai-news-beat.md).

    A source's beat seeds an event's `domains`; triage then sets them. An event's beat is
    always derived from its domains, never stored. OTHER only appears once triage has found
    neither domain; no source is on it.
    """

    CYBER = "cyber"
    AI = "ai"
    BOTH = "both"
    OTHER = "other"


class AiSignificance(StrEnum):
    """How much an AI story matters, on its own scale (the AI desk's "severity")."""

    MAJOR = "major"
    NOTABLE = "notable"
    MINOR = "minor"


CYBER_DOMAIN = "cybersecurity"
AI_DOMAIN = "ai"

_SEED_DOMAINS: dict[Beat | None, list[str]] = {
    None: [CYBER_DOMAIN],
    Beat.CYBER: [CYBER_DOMAIN],
    Beat.AI: [AI_DOMAIN],
    Beat.BOTH: [CYBER_DOMAIN, AI_DOMAIN],
}


def seed_domains(beat: Beat | None) -> list[str]:
    """The `domains` a new event starts with, from its source's beat (None means cyber)."""
    return list(_SEED_DOMAINS[beat])


def beat_of(domains: Iterable[str]) -> Beat:
    present = set(domains)
    cyber, ai = CYBER_DOMAIN in present, AI_DOMAIN in present
    if cyber and ai:
        return Beat.BOTH
    if ai:
        return Beat.AI
    return Beat.CYBER if cyber else Beat.OTHER


class Lane(StrEnum):
    FAST = "fast"
    NORMAL = "normal"
    DEEP = "deep"


class HealthStatus(StrEnum):
    OK = "ok"
    ERROR = "error"
    TIMEOUT = "timeout"
    EMPTY = "empty"
    STALE = "stale"
    DEGRADED = "degraded"
    DISABLED = "disabled"


class Standing(StrEnum):
    """What kind of voice a source is: the base of its reputation (worker/pipeline/reputation.py).

    Set by hand in config/sources.yaml. A source the discovery gate added has none there and is
    COMMUNITY until someone gives it one.
    """

    AUTHORITATIVE = "authoritative"
    ESTABLISHED = "established"
    SPECIALIST = "specialist"
    COMMUNITY = "community"


class ImportanceTier(StrEnum):
    KEY = "key"
    NOTABLE = "notable"
    ROUTINE = "routine"


class LifecycleState(StrEnum):
    DISCOVERED = "discovered"
    CANDIDATE = "candidate"
    TESTING = "testing"
    VALIDATED = "validated"
    ACTIVE = "active"
    DEGRADED = "degraded"
    BROKEN = "broken"
    RETIRED = "retired"


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Risk(_Model):
    """An unscored event has `None` for each number, never zero."""

    urgency: Score | None = None
    confidence: Score | None = None
    novelty: Score | None = None
    prominence: Score | None = None


class AuRelevance(_Model):
    relevance: Score | None = None
    directly_reported_in_au: bool = False
    reasons: list[str] = Field(default_factory=list)
    sectors: list[str] = Field(default_factory=list)
    soci_asset_classes: list[str] = Field(default_factory=list)


class Entities(_Model):
    actors: list[str] = Field(default_factory=list)
    organisations: list[str] = Field(default_factory=list)
    products: list[str] = Field(default_factory=list)
    countries: list[str] = Field(default_factory=list)
    industries: list[str] = Field(default_factory=list)


class CvssScore(_Model):
    score: Annotated[float, Field(ge=0.0, le=10.0)]
    vector: str | None = None
    source: str


class EpssScore(_Model):
    """An absent EPSS score is `score=None, status="unknown"`, never zero."""

    score: Score | None = None
    status: str = "unknown"


class KevEntry(_Model):
    listed: bool = False
    date_added: date | None = None
    due_date: date | None = None


class AdvisoryPackage(_Model):
    """`fixed` empty means no fixed version is published, not that the package is safe."""

    ecosystem: str
    name: str
    fixed: list[str] = Field(default_factory=list)


class AdvisoryRef(_Model):
    """An OSV or GitHub advisory naming the CVE (worker/groundtruth/osv.py)."""

    id: str
    source: str
    severity: Severity | None = None
    reviewed: bool = False
    packages: list[AdvisoryPackage] = Field(default_factory=list)
    url: str


class CveRef(_Model):
    """`cvss=None` means no score is known; it is never coerced to zero."""

    id: Annotated[str, Field(pattern=CVE_ID_PATTERN)]
    cvss: CvssScore | None = None
    epss: EpssScore = Field(default_factory=EpssScore)
    kev: KevEntry = Field(default_factory=KevEntry)
    advisories: list[AdvisoryRef] = Field(default_factory=list)


class MitreTechnique(_Model):
    id: str
    name: str
    confidence: Score
    confidence_type: str
    dataset_version: str
    evidence: list[str] = Field(default_factory=list)


class Claim(_Model):
    text: str
    confidence: Score
    evidence: list[str] = Field(default_factory=list)


class SourceRef(_Model):
    source_id: str
    url: str
    published: AwareDatetime | None = None
    evidence_class: EvidenceClass
    lineage_id: str | None = None
    independent: bool = False


class TimelineEntry(_Model):
    timestamp: AwareDatetime
    type: MaterialChange
    summary: str
    sources: list[str] = Field(default_factory=list)


class Relationship(_Model):
    type: RelationshipType
    event_id: EventId


class Importance(_Model):
    """How much an event matters, from its facts (worker/pipeline/importance.py)."""

    version: str
    score: Annotated[int, Field(ge=0, le=100)]
    tier: ImportanceTier
    reasons: Annotated[list[str], Field(max_length=5)] = Field(default_factory=list)


class Event(_Model):
    """The canonical unit. Article-shaped records exist only as `sources`."""

    # Fields excluded from the published shape. Empty today: every PLAN.md
    # section 5 field, including `pending_enrichment`, is published.
    INTERNAL_FIELDS: ClassVar[frozenset[str]] = frozenset()

    event_id: EventId
    schema_version: str = SCHEMA_VERSION
    pipeline_version: str = PIPELINE_VERSION
    scoring_version: str = SCORING_VERSION
    enrichment_version: str = UNENRICHED

    first_seen: AwareDatetime
    last_seen: AwareDatetime
    last_material_update: AwareDatetime | None = None
    last_independent_confirmation: AwareDatetime | None = None
    status: EventStatus = EventStatus.NEW

    title: str
    summary: str
    why_it_matters: str | None = None
    # A resolved event's closing summary of the whole case (worker/db/followup.py).
    resolution: str | None = None

    domains: list[str] = Field(default_factory=list)
    categories: list[str] = Field(default_factory=list)
    ai_subdomain: AiSubdomain | None = None
    # The AI desk's scale; None unless "ai" is in `domains` (docs/wiki/ai-news-beat.md).
    ai_significance: AiSignificance | None = None

    severity: Severity = Severity.UNKNOWN
    severity_source: SeveritySource = SeveritySource.UNKNOWN

    risk: Risk = Field(default_factory=Risk)
    au: AuRelevance = Field(default_factory=AuRelevance)
    entities: Entities = Field(default_factory=Entities)

    cves: list[CveRef] = Field(default_factory=list)
    mitre_techniques: list[MitreTechnique] = Field(default_factory=list)
    claims: list[Claim] = Field(default_factory=list)
    sources: list[SourceRef] = Field(default_factory=list)
    timeline: list[TimelineEntry] = Field(default_factory=list)
    relationships: list[Relationship] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    pending_enrichment: bool = False
    # Set at publish time from the sources' reputations (worker/publish/build.py), never
    # stored: every published event has one, and an event read from the database has none.
    importance: Importance | None = None

    @model_validator(mode="before")
    @classmethod
    def _drop_derived(cls, data: Any) -> Any:
        # `beat` is derived from `domains`, so a dumped event read back carries it; drop it.
        if isinstance(data, dict) and "beat" in data:
            data = {k: v for k, v in data.items() if k != "beat"}
        return data

    @model_validator(mode="after")
    def _significance_needs_ai(self) -> "Event":
        if self.ai_significance is not None and AI_DOMAIN not in self.domains:
            self.ai_significance = None
        return self

    @computed_field  # type: ignore[prop-decorator]
    @property
    def beat(self) -> Beat:
        return beat_of(self.domains)

    def model_dump_public(self) -> dict[str, Any]:
        """The publication shape: JSON-safe, nulls kept, internal-only fields excluded."""
        return self.model_dump(mode="json", exclude=set(self.INTERNAL_FIELDS))


class RawItem(_Model):
    source_id: str
    url: str
    guid: str | None = None
    title: str
    raw_summary: str | None = None
    published: AwareDatetime | None = None
    fetched_at: AwareDatetime
    payload_hash: str


class NormalisedItem(_Model):
    source_id: str
    url: str
    canonical_url: str
    guid: str | None = None
    title: str
    normalised_title: str
    summary: str | None = None
    published: AwareDatetime | None = None
    fetched_at: AwareDatetime
    cves: list[str] = Field(default_factory=list)
    url_hash: str
    title_hash: str
    tokens: frozenset[str] = Field(default_factory=frozenset)
    published_is_estimated: bool = False


class SourceConfig(_Model):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    id: str
    name: str
    type: str
    region: str
    category: str
    source_class: str = Field(alias="class")
    priority: int
    lane: Lane
    enabled: bool
    url: str
    parser: str
    expected_frequency: str
    notes: str | None = None
    # The organisation behind the feed, when it publishes through more than one (an id under
    # `publishers` in the registry); None means the source speaks for itself.
    publisher: str | None = None
    lifecycle_state: LifecycleState | None = None
    # The desk its items start on (seed_domains); None means cyber. Never OTHER.
    beat: Beat | None = None
    # One plain sentence on what the source is, and what kind of voice it is (Standing). Every
    # source in config/sources.yaml has both (tests/unit/test_registry.py); one the discovery
    # gate added has neither.
    description: str | None = None
    standing: Standing | None = None

    @model_validator(mode="after")
    def _no_other_beat(self) -> "SourceConfig":
        if self.beat is Beat.OTHER:
            raise ValueError(f"source {self.id}: beat 'other' is for triage, not for sources")
        return self

    @model_validator(mode="after")
    def _one_line_description(self) -> "SourceConfig":
        if self.description is not None and (
            not self.description.strip() or "\n" in self.description
        ):
            raise ValueError(f"source {self.id}: description must be one non-empty line")
        return self


class PublisherConfig(_Model):
    """An organisation in the registry's `publishers` (worker/pipeline/lineage.py)."""

    name: str
    # How headlines name it when relaying what it said ("ACSC warns ..."); see lineage.py.
    names: list[str] = Field(default_factory=list)


class SourceHealth(_Model):
    source_id: str
    checked_at: AwareDatetime
    status: HealthStatus
    error: str | None = None
    newest_item_age_days: float | None = None
    items_fetched: int = 0
    duration_ms: int = 0


class RunSummary(_Model):
    run_id: str
    lane: Lane
    started_at: AwareDatetime
    finished_at: AwareDatetime | None = None
    sources_ok: int
    sources_failed: int
    sources_stale: int
    items_fetched: int
    new_events: int
    updated_events: int
    duplicates: int
    archived_events: int
    errors: list[str] = Field(default_factory=list)
