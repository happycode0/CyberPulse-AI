"""Tests for the Event schema and domain models."""

import json
from datetime import UTC, date, datetime
from pathlib import Path

import jsonschema
import pytest
from pydantic import ValidationError
from referencing import Registry, Resource

from worker.models import (
    AiSubdomain,
    AuRelevance,
    Claim,
    CveRef,
    CvssScore,
    EpssScore,
    Event,
    EventStatus,
    EvidenceClass,
    KevEntry,
    Lane,
    MaterialChange,
    MitreTechnique,
    NormalisedItem,
    RawItem,
    Relationship,
    RelationshipType,
    Risk,
    RunSummary,
    Severity,
    SeveritySource,
    SourceConfig,
    SourceHealth,
    SourceRef,
    TimelineEntry,
)
from worker.version import (
    ENRICHMENT_VERSION,
    PIPELINE_VERSION,
    SCHEMA_VERSION,
    SCORING_VERSION,
)

NOW = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)
SCHEMAS = Path(__file__).resolve().parents[2] / "schemas"


def _minimal(**overrides):
    fields = {
        "event_id": "evt-2026-000001",
        "title": "Example vulnerability is being actively exploited",
        "summary": "Short original summary.",
        "first_seen": NOW,
        "last_seen": NOW,
    }
    fields.update(overrides)
    return Event(**fields)


def _full_event() -> Event:
    return Event(
        event_id="evt-2026-000123",
        first_seen=datetime(2026, 9, 29, 4, 0, tzinfo=UTC),
        last_seen=NOW,
        last_material_update=NOW,
        last_independent_confirmation=datetime(2026, 9, 29, 9, 0, tzinfo=UTC),
        status=EventStatus.DEVELOPING,
        title="Example vulnerability is being actively exploited",
        summary="Short original summary.",
        why_it_matters="Evidence-based, at most three sentences.",
        domains=["cybersecurity"],
        categories=["vulnerability", "active-exploitation"],
        ai_subdomain=AiSubdomain.AI_SECURITY,
        severity=Severity.CRITICAL,
        severity_source=SeveritySource.CNA,
        risk=Risk(urgency=0.93, confidence=0.96, novelty=0.87, prominence=0.91),
        au=AuRelevance(
            relevance=0.94,
            directly_reported_in_au=True,
            reasons=["ACSC advisory issued"],
            sectors=["government"],
            soci_asset_classes=["data storage or processing"],
        ),
        cves=[
            CveRef(
                id="CVE-2026-88772",
                cvss=CvssScore(score=9.8, vector="CVSS:4.0/AV:N", source="cna"),
                epss=EpssScore(score=None, status="unknown"),
                kev=KevEntry(
                    listed=True,
                    date_added=date(2026, 9, 27),
                    due_date=date(2026, 10, 18),
                ),
            )
        ],
        mitre_techniques=[
            MitreTechnique(
                id="T1190",
                name="Exploit Public-Facing Application",
                confidence=0.84,
                confidence_type="ai_suggested",
                dataset_version="ATT&CK v19.2",
                evidence=["vendor_advisory"],
            )
        ],
        claims=[
            Claim(
                text="The vulnerability is being actively exploited.",
                confidence=0.97,
                evidence=["cisa_kev", "vendor_advisory"],
            )
        ],
        sources=[
            SourceRef(
                source_id="acsc_alerts",
                url="https://example.org/advisory",
                published=datetime(2026, 9, 29, 4, 0, tzinfo=UTC),
                evidence_class=EvidenceClass.AUTHORITATIVE,
                lineage_id="vendor-release-8831",
                independent=True,
            )
        ],
        timeline=[
            TimelineEntry(
                timestamp=NOW,
                type=MaterialChange.EXPLOIT_CONFIRMED,
                summary="Vendor confirmed exploitation in the wild.",
                sources=["vendor_psirt"],
            )
        ],
        relationships=[
            Relationship(type=RelationshipType.EXPLOITS, event_id="evt-2026-000098")
        ],
    )


def _schema_validator(name: str) -> jsonschema.Draft202012Validator:
    registry = Registry()
    for path in SCHEMAS.glob("*.schema.json"):
        contents = json.loads(path.read_text())
        registry = registry.with_resource(path.name, Resource.from_contents(contents))
    schema = json.loads((SCHEMAS / name).read_text())
    jsonschema.Draft202012Validator.check_schema(schema)
    return jsonschema.Draft202012Validator(schema, registry=registry)


def test_event_requires_event_id_pattern():
    with pytest.raises(ValidationError):
        _minimal(event_id="nope")


@pytest.mark.parametrize("bad", ["evt-26-000001", "evt-2026-1", "evt-2026-0000001", "EVT-2026-000001"])
def test_event_id_rejects_near_misses(bad):
    with pytest.raises(ValidationError):
        _minimal(event_id=bad)


def test_event_id_accepts_canonical_form():
    assert _minimal(event_id="evt-2026-000123").event_id == "evt-2026-000123"


def test_severity_unknown_is_valid_and_is_not_low():
    assert Severity.UNKNOWN in Severity and Severity.UNKNOWN != Severity.LOW
    assert _minimal().severity is Severity.UNKNOWN
    assert _minimal().severity_source is SeveritySource.UNKNOWN


def test_event_defaults_versions_from_version_module():
    e = _minimal()
    assert (e.schema_version, e.pipeline_version, e.scoring_version) == (
        SCHEMA_VERSION,
        PIPELINE_VERSION,
        SCORING_VERSION,
    )
    assert e.enrichment_version == ENRICHMENT_VERSION


def test_event_minimal_defaults():
    e = _minimal()
    assert e.status is EventStatus.NEW
    assert e.ai_subdomain is None
    assert e.cves == [] and e.sources == [] and e.tags == []
    assert e.pending_enrichment is False


def test_enum_members_match_plan():
    assert {m.value for m in EventStatus} == {
        "new", "active", "developing", "monitoring", "contained", "resolved", "archived"
    }
    assert {m.value for m in Severity} == {"critical", "high", "medium", "low", "unknown"}
    assert {m.value for m in SeveritySource} == {
        "cna", "cisa_adp", "nvd", "vendor", "ai_estimate", "unknown"
    }
    assert {m.value for m in EvidenceClass} == {
        "PRIMARY", "AUTHORITATIVE", "VENDOR", "SPECIALIST", "NEWS", "COMMUNITY",
        "SOCIAL", "AI_INFERENCE",
    }
    assert {m.value for m in MaterialChange} == {
        "NEW_FACT", "NEW_CVE", "NEW_EXPLOIT", "EXPLOIT_CONFIRMED", "NEW_ACTOR",
        "NEW_TARGET", "NEW_GEOGRAPHY", "NEW_AU_EXPOSURE", "NEW_IMPACT", "NEW_PATCH",
        "NEW_MITIGATION", "NEW_EVIDENCE", "CORRECTION", "NO_MATERIAL_CHANGE",
    }
    assert {m.value for m in RelationshipType} == {
        "related_event", "follow_up_to", "caused_by", "exploits", "affects", "targets",
        "uses", "attributed_to", "mitigated_by", "resolves",
    }
    assert {m.value for m in AiSubdomain} == {
        "AI_INDUSTRY", "AI_SECURITY", "AI_THREAT_ACTIVITY", "AI_CYBER_CONVERGENCE"
    }
    assert {m.value for m in Lane} == {"fast", "normal", "deep"}


def test_cvss_absent_is_unknown_not_zero():
    cve = CveRef(id="CVE-2026-88772")
    assert cve.cvss is None and cve.epss.status == "unknown"
    assert cve.epss.score is None
    assert cve.kev.listed is False


def test_cve_id_pattern_enforced():
    with pytest.raises(ValidationError):
        CveRef(id="cve-2026-1")


def test_cvss_score_range_enforced():
    with pytest.raises(ValidationError):
        CvssScore(score=10.5, source="nvd")


def test_risk_range_enforced():
    with pytest.raises(ValidationError):
        Risk(urgency=1.2, confidence=0.5, novelty=0.5, prominence=0.5)


def test_unscored_event_has_null_risk_and_au_relevance_not_zero():
    e = _minimal()
    assert e.risk.urgency is None and e.risk.confidence is None
    assert e.risk.novelty is None and e.risk.prominence is None
    assert e.au.relevance is None
    dumped = e.model_dump_public()
    assert dumped["risk"] == {
        "urgency": None, "confidence": None, "novelty": None, "prominence": None
    }
    assert dumped["au"]["relevance"] is None
    _schema_validator("event.schema.json").validate(dumped)


def test_genuine_zero_score_is_distinct_from_unscored():
    e = _minimal(risk=Risk(urgency=0.0, confidence=0.0, novelty=0.0, prominence=0.0))
    assert e.risk.urgency == 0.0 and e.risk.urgency is not None
    _schema_validator("event.schema.json").validate(e.model_dump_public())


def test_event_schema_risk_and_au_relevance_are_optional_and_nullable():
    schema = json.loads((SCHEMAS / "event.schema.json").read_text())
    assert "required" not in schema["$defs"]["risk"]
    assert "relevance" not in schema["$defs"]["au"]["required"]
    validator = _schema_validator("event.schema.json")
    dumped = _minimal().model_dump_public()
    dumped["risk"] = {}
    del dumped["au"]["relevance"]
    validator.validate(dumped)
    dumped["risk"] = {"urgency": 1.5}
    with pytest.raises(jsonschema.ValidationError):
        validator.validate(dumped)


def test_naive_datetime_is_rejected():
    with pytest.raises(ValidationError):
        _minimal(first_seen=datetime(2026, 9, 29))


def test_naive_datetime_rejected_in_nested_and_items():
    with pytest.raises(ValidationError):
        SourceRef(
            source_id="s", url="https://e.org", published=datetime(2026, 9, 29),
            evidence_class=EvidenceClass.NEWS,
        )
    with pytest.raises(ValidationError):
        TimelineEntry(
            timestamp=datetime(2026, 9, 29), type=MaterialChange.NEW_FACT, summary="x"
        )
    with pytest.raises(ValidationError):
        RawItem(
            source_id="s", url="https://e.org", title="t",
            fetched_at=datetime(2026, 9, 29), payload_hash="h",
        )
    with pytest.raises(ValidationError):
        RunSummary(
            run_id="r", lane=Lane.FAST, started_at=datetime(2026, 9, 29),
            finished_at=None, sources_ok=0, sources_failed=0, sources_stale=0,
            items_fetched=0, new_events=0, updated_events=0, duplicates=0,
            archived_events=0, errors=[],
        )
    with pytest.raises(ValidationError):
        SourceHealth(source_id="s", checked_at=datetime(2026, 9, 29), status="ok")


def test_unknown_fields_are_rejected():
    with pytest.raises(ValidationError):
        _minimal(surprise="x")


def test_public_dump_validates_against_json_schema():
    for event in (_minimal(), _full_event()):
        _schema_validator("event.schema.json").validate(event.model_dump_public())


def test_public_dump_is_json_serialisable_with_utc_z():
    dumped = _full_event().model_dump_public()
    json.dumps(dumped)
    assert dumped["first_seen"] == "2026-09-29T04:00:00Z"
    assert dumped["cves"][0]["kev"]["date_added"] == "2026-09-27"
    assert dumped["severity"] == "critical"
    assert dumped["ai_subdomain"] == "AI_SECURITY"


def test_public_dump_keeps_explicit_nulls():
    dumped = _minimal().model_dump_public()
    assert dumped["ai_subdomain"] is None
    assert "last_material_update" in dumped


def test_public_dump_covers_every_model_field():
    assert set(_minimal().model_dump_public()) == set(Event.model_fields)


def test_event_schema_rejects_extra_and_bad_enum():
    validator = _schema_validator("event.schema.json")
    dumped = _minimal().model_dump_public()
    with pytest.raises(jsonschema.ValidationError):
        validator.validate({**dumped, "extra": 1})
    with pytest.raises(jsonschema.ValidationError):
        validator.validate({**dumped, "severity": "catastrophic"})
    with pytest.raises(jsonschema.ValidationError):
        validator.validate({**dumped, "event_id": "nope"})


def test_event_schema_enums_match_python_enums():
    schema = json.loads((SCHEMAS / "event.schema.json").read_text())
    props = schema["properties"]
    defs = schema["$defs"]
    assert set(props["status"]["enum"]) == {m.value for m in EventStatus}
    assert set(props["severity"]["enum"]) == {m.value for m in Severity}
    assert set(props["severity_source"]["enum"]) == {m.value for m in SeveritySource}
    assert set(defs["source_ref"]["properties"]["evidence_class"]["enum"]) == {
        m.value for m in EvidenceClass
    }
    assert set(defs["timeline_entry"]["properties"]["type"]["enum"]) == {
        m.value for m in MaterialChange
    }
    assert set(defs["relationship"]["properties"]["type"]["enum"]) == {
        m.value for m in RelationshipType
    }
    ai = [v for v in props["ai_subdomain"]["anyOf"] if "enum" in v][0]
    assert set(ai["enum"]) == {m.value for m in AiSubdomain}
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == set(Event.model_fields)


def test_live_schema_validates_document_with_events():
    validator = _schema_validator("live.schema.json")
    doc = {
        "generated_at": "2026-09-29T12:00:00Z",
        "last_completed_collection": "2026-09-29T11:55:00Z",
        "pipeline_version": PIPELINE_VERSION,
        "counts": {"total": 2, "critical": 1},
        "events": [_minimal().model_dump_public(), _full_event().model_dump_public()],
    }
    validator.validate(doc)
    doc["last_completed_collection"] = None
    validator.validate(doc)


def test_live_schema_rejects_bad_event_and_extra_keys():
    validator = _schema_validator("live.schema.json")
    doc = {
        "generated_at": "2026-09-29T12:00:00Z",
        "last_completed_collection": None,
        "pipeline_version": PIPELINE_VERSION,
        "counts": {},
        "events": [{"event_id": "nope"}],
    }
    with pytest.raises(jsonschema.ValidationError):
        validator.validate(doc)
    doc["events"] = []
    validator.validate(doc)
    with pytest.raises(jsonschema.ValidationError):
        validator.validate({**doc, "extra": 1})


def test_raw_item_optional_fields():
    item = RawItem(
        source_id="s", url="https://e.org/a", title="t", fetched_at=NOW, payload_hash="h"
    )
    assert item.guid is None and item.raw_summary is None and item.published is None


def test_normalised_item_tokens_are_frozenset():
    item = NormalisedItem(
        source_id="s", url="https://e.org/a?utm=1", canonical_url="https://e.org/a",
        guid=None, title="T", normalised_title="t", summary=None, published=None,
        fetched_at=NOW, cves=["CVE-2026-1234"], url_hash="u", title_hash="t",
        tokens=frozenset({"a", "b"}),
    )
    assert isinstance(item.tokens, frozenset)
    assert hasattr(item, "published_is_estimated")
    assert item.published_is_estimated is False


def test_source_config_accepts_class_alias_and_attribute_name():
    base = dict(
        id="acsc_alerts", name="ACSC Alerts", type="rss", region="AU",
        category="government", priority=1, lane="fast", enabled=True,
        url="https://example.org/feed", parser="feedparser", expected_frequency="1h",
    )
    by_alias = SourceConfig(**base, **{"class": "AUTHORITATIVE"})
    by_name = SourceConfig(**base, source_class="AUTHORITATIVE")
    assert by_alias.source_class == by_name.source_class == "AUTHORITATIVE"
    assert by_alias.lane is Lane.FAST
    assert by_alias.notes is None


def test_source_health_and_run_summary():
    h = SourceHealth(source_id="s", checked_at=NOW, status="ok")
    assert h.error is None
    r = RunSummary(
        run_id="r1", lane=Lane.NORMAL, started_at=NOW, finished_at=None,
        sources_ok=1, sources_failed=0, sources_stale=0, items_fetched=5,
        new_events=1, updated_events=0, duplicates=4, archived_events=0, errors=[],
    )
    assert r.finished_at is None and r.lane is Lane.NORMAL
