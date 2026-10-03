"""MITRE technique suggestions (PLAN.md §7.3): a model picks from the cached catalogue only.

An enriched event about an attack gets one more call, on tier 2 as §7.1 asks for MITRE
mapping. The model is not asked to recall ATT&CK. It is shown a shortlist of techniques from
the loaded release and may name only those: the schema's enum is the shortlist, so a technique
id it invents fails the schema rather than reaching the site. ATLAS joins the shortlist for
events about attacks on or with AI.

The shortlist is deterministic. Each attack category brings the techniques it most often
involves, and technique names that share words with the record add the rest. Suggestions are
published as `ai_suggested` with their confidence and release, and the site labels them AI
SUGGESTED. The one-sentence basis for each is kept as AI inference evidence, not published.

The task never gates `pending_enrichment`: an event is complete without it, and a suggestion
that waits for money or fails leaves the event as it was.
"""

import asyncio
import functools
import logging
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import httpx
from sqlalchemy import Engine

from worker.ai.budget import Governor, Mode
from worker.ai.client import OpenRouterClient
from worker.ai.enrich import (
    AiLayer,
    Done,
    Failed,
    Outcome,
    Waiting,
    _route,
    _utcnow,
    _work,
    run_task,
)
from worker.ai.ladder import Tier
from worker.ai.tasks import Checker, Rejected, Subject, Task, TaskName
from worker.db.enrichment import load_subjects, mark_done, mark_failed
from worker.db.ledger import record_to
from worker.db.mitre import (
    CurrentCatalogue,
    Suggestion,
    current_catalogue,
    events_due_for_mitre,
    record_mitre_inference,
    replace_suggestions,
)
from worker.db.session import get_engine
from worker.groundtruth.mitre import Technique
from worker.models import AiSubdomain
from worker.publish.build import LIVE_MIN_PROMINENCE
from worker.version import ENRICHMENT_VERSION

logger = logging.getLogger(__name__)

# Tier 2 is the dear tier, so a pass takes few events. Twice an hour this still covers the
# site's attack stories within a day or two of their being enriched.
DEFAULT_MITRE_BATCH = 5
MAX_CONCURRENT_EVENTS = 3
CANDIDATE_FACTOR = 5

MAX_CANDIDATES = 30
MAX_ATLAS_CANDIDATES = 12
MAX_SUGGESTIONS = 3
# Below this a suggestion is not published. The record has to show the technique, not allow it.
MIN_CONFIDENCE = 0.5
MAX_EVIDENCE_SOURCES = 5

# Categories (worker/ai/tasks.py) whose events describe an attack or a flaw an attack uses.
# Policy, regulation and research stories get no technique.
ATTACK_CATEGORIES: tuple[str, ...] = (
    "vulnerability",
    "zero-day",
    "active-exploitation",
    "malware",
    "ransomware",
    "data-breach",
    "phishing",
    "supply-chain",
    "ddos",
    "espionage",
    "fraud",
    "emerging-threat",
    "ai-security",
)
AI_SUBDOMAINS: tuple[AiSubdomain, ...] = (AiSubdomain.AI_SECURITY, AiSubdomain.AI_THREAT_ACTIVITY)

# The techniques each category most often involves, best first. Ids are checked against the
# loaded release, so one that a later release drops is skipped, never suggested.
CATEGORY_HINTS: Mapping[str, tuple[str, ...]] = {
    "vulnerability": ("T1190", "T1203", "T1068", "T1210", "T1133", "T1189"),
    "zero-day": ("T1190", "T1203", "T1068", "T1189"),
    "active-exploitation": ("T1190", "T1203", "T1068", "T1210", "T1133"),
    "malware": ("T1204", "T1059", "T1027", "T1105", "T1547", "T1071", "T1555", "T1003"),
    "ransomware": ("T1486", "T1490", "T1489", "T1485", "T1567", "T1048", "T1021", "T1078"),
    "data-breach": ("T1530", "T1567", "T1213", "T1048", "T1041", "T1078", "T1110", "T1555"),
    "phishing": ("T1566", "T1598", "T1204", "T1078", "T1189"),
    "supply-chain": ("T1195", "T1199", "T1078"),
    "ddos": ("T1498", "T1499"),
    "espionage": ("T1078", "T1133", "T1190", "T1566", "T1071", "T1505", "T1041", "T1003"),
    "fraud": ("T1657", "T1566", "T1078"),
    "emerging-threat": ("T1588", "T1583", "T1059", "T1105"),
}
ATLAS_HINTS: tuple[str, ...] = (
    "AML.T0051",  # prompt injection
    "AML.T0054",  # jailbreak
    "AML.T0053",  # AI agent tool invocation
    "AML.T0057",  # LLM data leakage
    "AML.T0010",  # AI supply chain compromise
    "AML.T0020",  # training data poisoning
    "AML.T0018",
    "AML.T0043",
    "AML.T0048",
    "AML.T0024",
    "AML.T0056",
    "AML.T0011",
)

# Words in technique names too common to say anything about a record.
_STOPWORDS = frozenset(
    {
        "and",
        "the",
        "for",
        "from",
        "with",
        "via",
        "into",
        "over",
        "through",
        "other",
        "use",
        "using",
        "based",
        "data",
        "information",
        "system",
        "systems",
        "service",
        "services",
        "account",
        "accounts",
        "access",
        "file",
        "files",
        "remote",
        "network",
        "local",
        "domain",
        "user",
        "users",
        "application",
        "applications",
        "software",
        "tool",
        "tools",
        "content",
    }
)
_WORD = re.compile(r"[a-z0-9]+")


def _stem(word: str) -> str:
    return word[:-1] if len(word) > 4 and word.endswith("s") else word


def _terms(text: str) -> set[str]:
    return {_stem(w) for w in _WORD.findall(text.casefold()) if len(w) > 3 and w not in _STOPWORDS}


# ─── Who gets a suggestion, and from what ─────────────────────────────────────────────────────────


def wants_mitre(subject: Subject) -> bool:
    e = subject.event
    return bool(set(e.categories) & set(ATTACK_CATEGORIES)) or e.ai_subdomain in AI_SUBDOMAINS


def uses_atlas(subject: Subject) -> bool:
    e = subject.event
    return e.ai_subdomain in AI_SUBDOMAINS or "ai-security" in e.categories


@dataclass(frozen=True)
class Candidate:
    technique: Technique  # `name` as the site shows it: "Phishing: Spearphishing Link"
    dataset_version: str


def _display(t: Technique, by_id: Mapping[str, Technique]) -> Technique:
    parent = by_id.get(t.parent_id) if t.parent_id else None
    if parent is None:
        return t
    return Technique(t.id, f"{parent.name}: {t.name}", t.tactics, t.parent_id)


def _pick(
    catalogue: CurrentCatalogue, hints: Iterable[str], text: set[str], limit: int
) -> list[Candidate]:
    by_id = catalogue.by_id()
    chosen = dict.fromkeys(h for h in hints if h in by_id)
    scored = []
    for t in catalogue.techniques:
        if t.id in chosen:
            continue
        words = _terms(t.name)
        hits = len(words & text)
        if hits and hits * 2 >= len(words):
            scored.append((-hits / len(words), -hits, t.id))
    chosen.update(dict.fromkeys(tid for *_, tid in sorted(scored)))
    return [
        Candidate(_display(by_id[tid], by_id), catalogue.version) for tid in list(chosen)[:limit]
    ]


def shortlist(
    subject: Subject, attack: CurrentCatalogue | None, atlas: CurrentCatalogue | None
) -> list[Candidate]:
    """The techniques the model may choose from, at most `MAX_CANDIDATES`."""
    e = subject.event
    text = _terms(
        " ".join(
            [
                e.title,
                subject.source_text,
                *subject.headlines,
                *e.tags,
                *e.entities.products,
            ]
        )
    )
    out: list[Candidate] = []
    if atlas is not None and uses_atlas(subject):
        out += _pick(atlas, ATLAS_HINTS, text, MAX_ATLAS_CANDIDATES)
    if attack is not None:
        hints = (h for c in e.categories for h in CATEGORY_HINTS.get(c, ()))
        out += _pick(attack, hints, text, MAX_CANDIDATES - len(out))
    return out


# ─── The call ─────────────────────────────────────────────────────────────────────────────────────

MITRE_PROMPT = """\
Task: name the MITRE techniques the record shows an attacker used, choosing only from the \
candidates below.

- techniques: at most three, the best supported first. Each has:
  id: one of the candidate ids.
  confidence: 0 to 1, how directly the record shows this technique was used.
  basis: one sentence, in your own words, naming the fact in the record that shows it.
- Name a technique only when the record describes it being used or exploitable in this case, \
not because it is possible in general. An empty list is the right answer when the record does \
not say how an attack works.

Candidates, as id: name [tactics]:
{candidates}"""


@dataclass(frozen=True)
class MitreAnswer:
    picks: tuple[tuple[Suggestion, str], ...]  # each with the model's basis for it
    dropped: int = 0  # under MIN_CONFIDENCE


def _schema(ids: Sequence[str]) -> dict[str, Any]:
    item = {
        "type": "object",
        "properties": {
            "id": {"type": "string", "enum": list(ids)},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "basis": {"type": "string"},
        },
        "required": ["id", "confidence", "basis"],
        "additionalProperties": False,
    }
    return {
        "type": "object",
        "properties": {"techniques": {"type": "array", "items": item}},
        "required": ["techniques"],
        "additionalProperties": False,
    }


def _evidence(subject: Subject) -> tuple[str, ...]:
    """What a reader can check a suggestion against: the reports it was drawn from."""
    ids = dict.fromkeys(s.source_id for s in subject.event.sources)
    return tuple(ids)[:MAX_EVIDENCE_SOURCES]


def parse_mitre(
    data: dict[str, Any], subject: Subject, candidates: Mapping[str, Candidate]
) -> MitreAnswer:
    check = Checker(subject)
    picks: dict[str, tuple[Suggestion, str]] = {}
    dropped = 0
    for item in data["techniques"]:
        candidate = candidates.get(item["id"])
        if candidate is None:
            raise Rejected("a technique is not one of the candidates")
        confidence = float(item["confidence"])
        if confidence < MIN_CONFIDENCE:
            dropped += 1
            continue
        if candidate.technique.id in picks:
            continue
        basis = check.prose("basis", item["basis"], max_chars=240, max_sentences=2)
        suggestion = Suggestion(
            candidate.technique, candidate.dataset_version, confidence, _evidence(subject)
        )
        picks[candidate.technique.id] = (suggestion, basis)
    ranked = sorted(picks.values(), key=lambda p: (-p[0].confidence, p[0].technique.id))
    return MitreAnswer(tuple(ranked[:MAX_SUGGESTIONS]), dropped)


def mitre_task(candidates: Sequence[Candidate]) -> Task:
    """The task for one event: its candidates are its prompt's list and its schema's enum."""
    by_id = {c.technique.id: c for c in candidates}
    lines = "\n".join(
        f"  {c.technique.id}: {c.technique.name}"
        + (f" [{', '.join(c.technique.tactics)}]" if c.technique.tactics else "")
        for c in candidates
    )
    return Task(
        TaskName.MITRE,
        Tier.STRONG,
        _schema(list(by_id)),
        MITRE_PROMPT.format(candidates=lines),
        functools.partial(parse_mitre, candidates=by_id),  # type: ignore[arg-type]
    )


# ─── One pass ─────────────────────────────────────────────────────────────────────────────────────


@dataclass
class MitreSummary:
    mode: str | None = None
    catalogues: list[str] = field(default_factory=list)
    events: int = 0
    blocked: int = 0
    done: int = 0
    suggested: int = 0  # techniques published
    failed: int = 0
    waiting: int = 0

    @property
    def changed_anything(self) -> bool:
        return bool(self.done)


def _load(
    engine: Engine, now: datetime, limit: int
) -> tuple[CurrentCatalogue | None, CurrentCatalogue | None, list[Subject]]:
    with engine.connect() as conn:
        attack = current_catalogue(conn, "enterprise")
        atlas = current_catalogue(conn, "atlas")
        ids = events_due_for_mitre(
            conn,
            version=ENRICHMENT_VERSION,
            now=now,
            floor=LIVE_MIN_PROMINENCE,
            categories=ATTACK_CATEGORIES,
            subdomains=[s.value for s in AI_SUBDOMAINS],
            limit=limit,
        )
        return attack, atlas, load_subjects(conn, ids)


def _write(engine: Engine, subject: Subject, outcome: Outcome, now: datetime) -> None:
    event_id = subject.event.event_id
    with engine.begin() as conn:
        if isinstance(outcome, Done):
            answer = outcome.result
            assert isinstance(answer, MitreAnswer)
            replace_suggestions(conn, event_id, [s for s, _ in answer.picks])
            record_mitre_inference(
                conn, event_id, answer.picks, model=outcome.model, version=ENRICHMENT_VERSION
            )
            mark_done(
                conn,
                event_id,
                TaskName.MITRE,
                version=ENRICHMENT_VERSION,
                model=outcome.model,
                now=now,
            )
        elif isinstance(outcome, Failed):
            mark_failed(
                conn,
                event_id,
                TaskName.MITRE,
                version=ENRICHMENT_VERSION,
                model=outcome.model,
                reason=outcome.reason,
                now=now,
            )


async def suggest_techniques(
    *,
    engine: Engine | None = None,
    layer: AiLayer | None = None,
    batch: int = DEFAULT_MITRE_BATCH,
    now: datetime | None = None,
) -> MitreSummary:
    """Suggest techniques for up to `batch` enriched attack events, most prominent first."""
    engine = engine or get_engine()
    layer = layer or AiLayer()
    moment = now if now is not None else _utcnow()
    summary = MitreSummary()
    if batch <= 0:
        return summary

    attack, atlas, subjects = await asyncio.to_thread(
        _load, engine, moment, batch * CANDIDATE_FACTOR
    )
    summary.catalogues = [c.version for c in (attack, atlas) if c is not None]
    if attack is None:
        # ATLAS alone would leave most attack stories with no candidates at all.
        logger.info("MITRE suggestions: no ATT&CK release is loaded yet, so nothing runs")
        return summary

    async with httpx.AsyncClient() as http:
        governor = await layer.ready(http)
        if governor is None:
            return summary
        mode, why = governor.mode()
        summary.mode = mode.value
        if mode is Mode.OFF:
            return summary

        work: list[tuple[Subject, Task]] = []
        for subject in subjects:
            candidates = shortlist(subject, attack, atlas) if wants_mitre(subject) else []
            if not candidates:
                continue
            task = mitre_task(candidates)
            if isinstance(_route(governor, task, _work(task, subject)), Waiting):
                summary.blocked += 1
                continue
            work.append((subject, task))
        work = work[:batch]
        summary.events = len(work)
        if not work:
            return summary

        assert layer.verified is not None and layer.settings.openrouter_api_key is not None
        client = OpenRouterClient(
            layer.verified,
            layer.settings.openrouter_api_key,
            http,
            functools.partial(record_to, engine),
            layer.settings,
        )
        await _run(client, governor, engine, work, moment, summary)

    logger.info(
        "MITRE suggestions: mode %s (%s); catalogues %s; %d events, %d blocked; done=%d "
        "suggested=%d failed=%d waiting=%d",
        summary.mode,
        why,
        ", ".join(summary.catalogues),
        summary.events,
        summary.blocked,
        summary.done,
        summary.suggested,
        summary.failed,
        summary.waiting,
    )
    return summary


async def _run(
    client: OpenRouterClient,
    governor: Governor,
    engine: Engine,
    work: Sequence[tuple[Subject, Task]],
    now: datetime,
    summary: MitreSummary,
) -> None:
    gate = asyncio.Semaphore(MAX_CONCURRENT_EVENTS)

    async def one(subject: Subject, task: Task) -> None:
        async with gate:
            outcome = await run_task(client, governor, task, subject)
            await asyncio.to_thread(_write, engine, subject, outcome, now)
        event_id = subject.event.event_id
        match outcome:
            case Done(result=MitreAnswer(picks=picks)):
                summary.done += 1
                summary.suggested += len(picks)
            case Failed(reason=reason):
                summary.failed += 1
                logger.info("MITRE suggestion for %s failed: %s", event_id, reason)
            case Waiting(reason=reason):
                summary.waiting += 1
                logger.debug("MITRE suggestion for %s waits: %s", event_id, reason)

    # As in enrich_pending: an error that is not a failed call cancels the rest.
    async with asyncio.TaskGroup() as group:
        for subject, task in work:
            group.create_task(one(subject, task))
