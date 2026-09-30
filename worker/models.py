"""Domain models for CyberPulse-AI: the event schema (PLAN.md section 5) and pipeline items."""

from datetime import date
from enum import StrEnum
from typing import Annotated, Any, ClassVar

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from worker.version import (
    ENRICHMENT_VERSION,
    PIPELINE_VERSION,
    SCHEMA_VERSION,
    SCORING_VERSION,
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


class Lane(StrEnum):
    FAST = "fast"
    NORMAL = "normal"
    DEEP = "deep"


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Risk(_Model):
    urgency: Score
    confidence: Score
    novelty: Score
    prominence: Score


class AuRelevance(_Model):
    relevance: Score = 0.0
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


class CveRef(_Model):
    """`cvss=None` means no score is known; it is never coerced to zero."""

    id: Annotated[str, Field(pattern=CVE_ID_PATTERN)]
    cvss: CvssScore | None = None
    epss: EpssScore = Field(default_factory=EpssScore)
    kev: KevEntry = Field(default_factory=KevEntry)


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


class Event(_Model):
    """The canonical unit. Article-shaped records exist only as `sources`."""

    # Fields excluded from the published shape. Empty today: every PLAN.md
    # section 5 field, including `pending_enrichment`, is published.
    INTERNAL_FIELDS: ClassVar[frozenset[str]] = frozenset()

    event_id: EventId
    schema_version: str = SCHEMA_VERSION
    pipeline_version: str = PIPELINE_VERSION
    scoring_version: str = SCORING_VERSION
    enrichment_version: str = ENRICHMENT_VERSION

    first_seen: AwareDatetime
    last_seen: AwareDatetime
    last_material_update: AwareDatetime | None = None
    last_independent_confirmation: AwareDatetime | None = None
    status: EventStatus = EventStatus.NEW

    title: str
    summary: str
    why_it_matters: str | None = None

    domains: list[str] = Field(default_factory=list)
    categories: list[str] = Field(default_factory=list)
    ai_subdomain: AiSubdomain | None = None

    severity: Severity = Severity.UNKNOWN
    severity_source: SeveritySource = SeveritySource.UNKNOWN

    risk: Risk = Field(
        default_factory=lambda: Risk(urgency=0.0, confidence=0.0, novelty=0.0, prominence=0.0)
    )
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


class SourceHealth(_Model):
    source_id: str
    checked_at: AwareDatetime
    status: str
    error: str | None = None


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
