"""Claims from ground truth (PLAN.md §5: claims with evidence references).

Each claim is a sentence a reader can check against the register it names: CISA's KEV catalogue,
the CVE record's CVSS score, FIRST's EPSS, the advisories that publish fixes, and the event's own
independent sources. Its evidence is that register, and its confidence is the register's
evidence-class weight from config/scoring.yaml, the weights scoring already trusts.

They are worked out at publish time from what the event already carries, so they can never
disagree with the event's own CVE data, and a register update changes them at the next publish
with nothing stored. Stored claims come first; a fact claim with the same words is not repeated.
"""

from collections.abc import Mapping, Sequence
from functools import cache

from worker.models import AdvisoryRef, Claim, CveRef, Event, EvidenceClass
from worker.pipeline.score import DEFAULT_SCORING_PATH, ScoringConfig, independent_confirmations

Weights = Mapping[EvidenceClass, float]

MAX_NAMED_CVES = 4
MAX_FIX_CLAIMS = 3
MAX_EVIDENCE_SOURCES = 5

# Who set a CVSS score (worker/groundtruth/cvss.py), the words for them, the class of what they
# said, and where a reader finds it. CNA and CISA ADP scores are in the CVE record itself.
_CVSS_SOURCES: Mapping[str, tuple[str, EvidenceClass, str]] = {
    "cna": ("the CNA that assigned it", EvidenceClass.PRIMARY, "cve_record"),
    "cisa_adp": ("CISA in the CVE record", EvidenceClass.AUTHORITATIVE, "cve_record"),
    "nvd": ("NVD", EvidenceClass.AUTHORITATIVE, "nvd"),
}


@cache
def default_weights() -> Weights:
    return ScoringConfig.load(DEFAULT_SCORING_PATH).evidence_class_weights


def _ids(cves: Sequence[CveRef]) -> str:
    ids = [c.id for c in cves[:MAX_NAMED_CVES]]
    more = len(cves) - len(ids)
    if more:
        return f"{', '.join(ids)} and {more} more"
    return ids[0] if len(ids) == 1 else f"{', '.join(ids[:-1])} and {ids[-1]}"


def _band(score: float) -> str:
    if score >= 9.0:
        return "critical"
    if score >= 7.0:
        return "high"
    if score >= 4.0:
        return "medium"
    return "low" if score > 0 else "none"


def _cvss_version(vector: str | None) -> str:
    if vector and vector.startswith("CVSS:"):
        return "CVSS " + vector.split("/", 1)[0].removeprefix("CVSS:")
    return "CVSS"


def _percent(p: float) -> str:
    pct = p * 100
    if pct >= 10:
        return f"{pct:.0f}%"
    return f"{pct:.1f}%" if pct >= 1 else "under 1%"


def kev_claim(event: Event, w: Weights) -> Claim | None:
    listed = [c for c in event.cves if c.kev.listed]
    if not listed:
        return None
    text = f"CISA lists {_ids(listed)} as exploited in the wild"
    if len(listed) == 1 and listed[0].kev.date_added:
        kev = listed[0].kev
        text += f", added {kev.date_added.isoformat()}"
        if kev.due_date:
            text += f", with US federal agencies due to fix it by {kev.due_date.isoformat()}"
    return Claim(text=text + ".", confidence=w[EvidenceClass.AUTHORITATIVE], evidence=["cisa_kev"])


def cvss_claim(event: Event, w: Weights) -> Claim | None:
    scored = [
        (c.cvss, c.id) for c in event.cves if c.cvss is not None and c.cvss.source in _CVSS_SOURCES
    ]
    if not scored:
        return None
    cvss, cve_id = max(scored, key=lambda p: (p[0].score, p[1]))
    who, cls, register = _CVSS_SOURCES[cvss.source]
    text = (
        f"{cve_id} is rated {cvss.score:.1f} ({_band(cvss.score)}) under "
        f"{_cvss_version(cvss.vector)} by {who}"
    )
    if len(scored) > 1:
        text += f", the highest of the {len(scored)} scored CVEs here"
    return Claim(text=text + ".", confidence=w[cls], evidence=[f"{register}:{cve_id}"])


def epss_claim(event: Event, w: Weights) -> Claim | None:
    known = [(c.epss.score, c.id) for c in event.cves if c.epss.score is not None]
    if not known:
        return None
    score, cve_id = max(known)
    text = (
        f"FIRST's EPSS puts the chance of {cve_id} being exploited in the next 30 days at "
        f"{_percent(score)}"
    )
    if len(known) > 1:
        text += f", the highest of the {len(known)} CVEs here"
    return Claim(
        text=text + ".", confidence=w[EvidenceClass.SPECIALIST], evidence=[f"first_epss:{cve_id}"]
    )


def _advisory_class(a: AdvisoryRef) -> EvidenceClass:
    """A GitHub advisory its staff reviewed is authoritative; anything else is a specialist's."""
    if a.source == "ghsa" and a.reviewed:
        return EvidenceClass.AUTHORITATIVE
    return EvidenceClass.SPECIALIST


def _versions(fixed: Sequence[str]) -> str:
    return fixed[0] if len(fixed) == 1 else f"{', '.join(fixed[:-1])} and {fixed[-1]}"


def fix_claims(event: Event, w: Weights) -> list[Claim]:
    """One claim per package, from the best advisory naming it: a reviewed GitHub advisory, then
    any GitHub advisory, then OSV's own record. A package no advisory has a fix for says so."""
    best: dict[tuple[str, str], tuple[AdvisoryRef, list[str]]] = {}
    unfixed: dict[tuple[str, str], AdvisoryRef] = {}
    ranked = sorted(
        (a for c in event.cves for a in c.advisories),
        key=lambda a: (not (a.source == "ghsa" and a.reviewed), a.source != "ghsa", a.id),
    )
    for a in ranked:
        for p in a.packages:
            key = (p.ecosystem, p.name)
            if p.fixed and key not in best:
                best[key] = (a, list(p.fixed))
            elif not p.fixed:
                unfixed.setdefault(key, a)

    out = [
        Claim(
            text=f"A fix for {name} ({eco}) is published in {_versions(fixed)}, per {a.id}.",
            confidence=w[_advisory_class(a)],
            evidence=[a.id],
        )
        for (eco, name), (a, fixed) in best.items()
    ]
    out += [
        Claim(
            text=f"No fixed version of {name} ({eco}) is published yet, per {a.id}.",
            confidence=w[_advisory_class(a)],
            evidence=[a.id],
        )
        for (eco, name), a in unfixed.items()
        if (eco, name) not in best
    ]
    return out[:MAX_FIX_CLAIMS]


def corroboration_claim(event: Event) -> Claim | None:
    n = independent_confirmations(event)
    if n < 2 or event.risk.confidence is None:
        return None
    ids = list(dict.fromkeys(s.source_id for s in event.sources if s.independent))
    return Claim(
        text=f"{n} independent sources report this event.",
        confidence=event.risk.confidence,
        evidence=ids[:MAX_EVIDENCE_SOURCES],
    )


def fact_claims(event: Event, weights: Weights) -> list[Claim]:
    found = [
        kev_claim(event, weights),
        cvss_claim(event, weights),
        epss_claim(event, weights),
        *fix_claims(event, weights),
        corroboration_claim(event),
    ]
    return [c for c in found if c is not None]


def with_fact_claims(event: Event, weights: Weights | None = None) -> Event:
    """The event with its fact claims after its stored ones."""
    claims = list(event.claims)
    seen = {c.text.casefold() for c in claims}
    for c in fact_claims(event, weights if weights is not None else default_weights()):
        if c.text.casefold() not in seen:
            seen.add(c.text.casefold())
            claims.append(c)
    return event.model_copy(update={"claims": claims})
