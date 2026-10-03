"""The cached MITRE catalogues (migration 007) and the technique suggestions written from them.

A release is loaded once, whole, in one transaction, so a catalogue is never half there. The
newest loaded release of each matrix is the current one; older ones stay, because a suggestion
already published names the release it was chosen from.
"""

import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import Connection, text

from worker.groundtruth.mitre import Catalogue, Technique
from worker.models import EvidenceClass

AI_SUGGESTED = "ai_suggested"


def loaded_versions(conn: Connection, matrix: str) -> set[str]:
    rows = conn.execute(
        text("select version from mitre_dataset_versions where matrix = :m and techniques > 0"),
        {"m": matrix},
    )
    return {r[0] for r in rows}


def load_catalogue(conn: Connection, catalogue: Catalogue) -> int:
    """Store a release and its techniques. Loading the same release twice changes nothing."""
    release = catalogue.release
    if release.version in loaded_versions(conn, release.matrix):
        return 0
    conn.execute(
        text(
            "insert into mitre_dataset_versions (version, matrix, released_at, source_url) "
            "values (:v, :m, :r, :u) on conflict (version) do update set matrix = excluded.matrix, "
            "released_at = excluded.released_at, source_url = excluded.source_url"
        ),
        {"v": release.version, "m": release.matrix, "r": release.released_at, "u": release.url},
    )
    conn.execute(
        text("delete from mitre_catalogue where dataset_version = :v"), {"v": release.version}
    )
    conn.execute(
        text(
            "insert into mitre_catalogue (dataset_version, technique_id, name, tactics, parent_id) "
            "values (:v, :id, :name, cast(:tactics as text[]), :parent)"
        ),
        [
            {
                "v": release.version,
                "id": t.id,
                "name": t.name,
                "tactics": list(t.tactics),
                "parent": t.parent_id,
            }
            for t in catalogue.techniques
        ],
    )
    conn.execute(
        text(
            "update mitre_dataset_versions set techniques = :n, loaded_at = now() where version = :v"
        ),
        {"n": len(catalogue.techniques), "v": release.version},
    )
    return len(catalogue.techniques)


@dataclass(frozen=True)
class CurrentCatalogue:
    version: str
    techniques: tuple[Technique, ...]

    def by_id(self) -> dict[str, Technique]:
        return {t.id: t for t in self.techniques}


def current_catalogue(conn: Connection, matrix: str) -> CurrentCatalogue | None:
    """The newest loaded release of a matrix, or None if none is loaded yet."""
    version = conn.execute(
        text(
            "select version from mitre_dataset_versions where matrix = :m and techniques > 0 "
            "order by released_at desc nulls last, loaded_at desc limit 1"
        ),
        {"m": matrix},
    ).scalar()
    if version is None:
        return None
    rows = conn.execute(
        text(
            "select technique_id, name, tactics, parent_id from mitre_catalogue "
            "where dataset_version = :v order by technique_id"
        ),
        {"v": version},
    )
    return CurrentCatalogue(version, tuple(Technique(r[0], r[1], tuple(r[2]), r[3]) for r in rows))


@dataclass(frozen=True)
class Suggestion:
    technique: Technique
    dataset_version: str
    confidence: float
    # What a reader can check the suggestion against: the source reports it was drawn from.
    # The model's own reasoning is kept in `evidence` as AI inference, not published here.
    evidence: tuple[str, ...]


def replace_suggestions(conn: Connection, event_id: str, suggestions: Sequence[Suggestion]) -> None:
    """The event's AI-suggested techniques become exactly `suggestions`. Rows of any other
    `confidence_type` (a mapping a source published) are never touched."""
    conn.execute(
        text("delete from mitre_techniques where event_id = :e and confidence_type = :ai"),
        {"e": event_id, "ai": AI_SUGGESTED},
    )
    if not suggestions:
        return
    conn.execute(
        text(
            "insert into mitre_techniques (event_id, technique_id, name, confidence, "
            "confidence_type, dataset_version, evidence) "
            "values (:e, :id, :name, :c, :ai, :v, cast(:evidence as text[])) "
            "on conflict (event_id, technique_id, dataset_version) do nothing"
        ),
        [
            {
                "e": event_id,
                "id": s.technique.id,
                "name": s.technique.name,
                "c": s.confidence,
                "ai": AI_SUGGESTED,
                "v": s.dataset_version,
                "evidence": list(s.evidence),
            }
            for s in suggestions
        ],
    )


def events_due_for_mitre(
    conn: Connection,
    *,
    version: str,
    now: datetime,
    floor: float,
    categories: Sequence[str],
    subdomains: Sequence[str],
    limit: int,
) -> list[str]:
    """Enriched events about an attack that have no suggestion at `version` yet, most prominent
    first. A suggestion in backoff waits; one that has given up waits for the next version."""
    rows = conn.execute(
        text(
            "select e.event_id from events e "
            "where not e.pending_enrichment and e.status <> 'archived' and e.prominence > :floor "
            "and (e.categories && cast(:categories as text[]) "
            "or e.ai_subdomain = any(cast(:subdomains as text[]))) "
            "and not exists (select 1 from event_enrichment x where x.event_id = e.event_id "
            "and x.task = 'mitre' and x.version = :version and (x.status = 'done' "
            "or x.next_attempt_at is null or x.next_attempt_at > :now)) "
            "order by e.prominence desc, e.event_id limit :limit"
        ),
        {
            "version": version,
            "now": now,
            "floor": floor,
            "categories": list(categories),
            "subdomains": list(subdomains),
            "limit": limit,
        },
    )
    return [r[0] for r in rows]


def record_mitre_inference(
    conn: Connection,
    event_id: str,
    picks: Sequence[tuple[Suggestion, str]],
    *,
    model: str | None,
    version: str,
) -> None:
    """Keep what the model said and why, as AI inference. The reasons are not published: the
    site shows the technique, its confidence and the release, labelled AI SUGGESTED."""
    conn.execute(
        text(
            "insert into evidence (event_id, kind, evidence_class, detail) "
            "values (:event_id, 'ai_mitre', :cls, cast(:detail as jsonb))"
        ),
        {
            "event_id": event_id,
            "cls": EvidenceClass.AI_INFERENCE.value,
            "detail": json.dumps(
                {
                    "techniques": [
                        {
                            "id": s.technique.id,
                            "dataset_version": s.dataset_version,
                            "confidence": s.confidence,
                            "basis": basis,
                        }
                        for s, basis in picks
                    ],
                    "model": model,
                    "enrichment_version": version,
                }
            ),
        },
    )
