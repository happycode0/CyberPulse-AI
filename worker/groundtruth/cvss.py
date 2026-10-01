"""The Stage 2 severity chain: CNA record → CISA Vulnrichment ADP → NVD.

PLAN.md §2.5 sets this order and the reason for it. NVD's analysis backlog means a large share of
recent CVEs are marked "Deferred" and carry no NVD-authored CVSS at all, so a pipeline that waits
for NVD reports `unknown` for most of what it collects. Sampling 300 recent CVEs found 223
deferred with no NVD score, but 297 of the 300 carried a score from either the CNA that published
the record or CISA's Vulnrichment ADP container. Reading the CNA record first turns almost all of
those back into known severities, and NVD becomes the optional last rung rather than the gate.

The rule that survives all of it: a CVE with no base score anywhere resolves to None, which the
models record as `unknown`. It is never a 0.0 and never a default of "low". An invented low score
on an unscored critical vulnerability is the single most damaging fabrication this pipeline could
make, which is why `resolve_cvss` has no fallback value.
"""

from worker.models import CvssScore, SeveritySource

# Highest version first: this is the preference order within a single container. A CNA that
# publishes both a v3.1 and a v4.0 metric has said the same thing twice in two dialects, and the
# newer one is the one its vector string can be re-scored from.
CVSS_METRIC_KEYS = ("cvssV4_0", "cvssV3_1", "cvssV3_0", "cvssV2_0")

# `providerMetadata.shortName` values that map onto a rung of the chain. Anything else — a vendor
# ADP, the CVE Program's own container — is skipped rather than guessed at: SeveritySource has a
# `vendor` member, but attributing an arbitrary ADP to it would assert a provenance nobody stated.
ADP_SOURCES = {
    "cisa-adp": SeveritySource.CISA_ADP,
    "nvd": SeveritySource.NVD,
}

# The order the chain is consulted in, after the CNA container.
ADP_CHAIN = (SeveritySource.CISA_ADP, SeveritySource.NVD)


def resolve_cvss(record: object) -> CvssScore | None:
    """Resolve one CVSS base score from a cvelistV5 record.

    Parameters
    ----------
    record : object
        A parsed CVE record as served by the CVE Services API (`containers.cna`, `containers.adp`).
        Anything that is not shaped like one resolves to None rather than raising: this runs over
        thousands of records in a sync, and one malformed record must not end the run.

    Returns
    -------
    CvssScore | None
        The score from the most authoritative container that has one, with `source` naming which
        rung it came from. None when no container carries a base score, which callers record as
        `unknown` — never as zero.
    """
    for source, metrics in _rungs(record):
        score = _score_from_metrics(metrics, source)
        if score is not None:
            return score
    return None


def _rungs(record: object):
    """Yield `(source, metrics)` for each rung of the chain that exists, in chain order.

    The ADP containers are collected before being yielded because `containers.adp` is a list in
    whatever order the record happens to carry. In a sampled record CISA-ADP was `adp[0]` and the
    CVE Program's container was `adp[1]`, but nothing guarantees that; iterating the list directly
    would make the chain's priority depend on array order, so an NVD container listed first would
    outrank the CISA enrichment that PLAN.md §2.5 puts above it.
    """
    if not isinstance(record, dict):
        return
    containers = record.get("containers")
    if not isinstance(containers, dict):
        return

    cna = containers.get("cna")
    if isinstance(cna, dict):
        yield SeveritySource.CNA, cna.get("metrics")

    found: dict[SeveritySource, object] = {}
    adp_entries = containers.get("adp")
    if isinstance(adp_entries, list):
        for adp in adp_entries:
            if not isinstance(adp, dict):
                continue
            provider = adp.get("providerMetadata")
            short_name = provider.get("shortName") if isinstance(provider, dict) else None
            if not isinstance(short_name, str):
                continue
            source = ADP_SOURCES.get(short_name.strip().lower())
            if source is None:
                continue
            # First container of a given provider wins; a record with two is already malformed and
            # picking the earlier one at least makes the outcome deterministic.
            found.setdefault(source, adp.get("metrics"))

    for source in ADP_CHAIN:
        if source in found:
            yield source, found[source]


def _score_from_metrics(metrics: object, source: SeveritySource) -> CvssScore | None:
    """Pick the best CVSS base score out of one container's `metrics` list.

    `metrics` is a list of dicts, each keyed by CVSS version ("cvssV3_1") alongside unrelated keys
    such as "format" and "scenarios", and CISA-ADP additionally carries `other` entries whose
    `type` is "ssvc" or "kev". Only the version keys are read here; the SSVC decision points
    (Exploitation, Automatable, Technical Impact) have no field in CveRef, so capturing them would
    mean changing schemas/event.schema.json — worth doing, but as its own decision.
    """
    if not isinstance(metrics, list):
        return None

    best_rank: int | None = None
    best: CvssScore | None = None

    for entry in metrics:
        if not isinstance(entry, dict):
            continue
        for rank, key in enumerate(CVSS_METRIC_KEYS):
            block = entry.get(key)
            if not isinstance(block, dict):
                continue
            base = block.get("baseScore")
            # bool is an int subclass, so `True` would otherwise arrive as a base score of 1.0.
            if isinstance(base, bool) or not isinstance(base, (int, float)):
                continue
            base = float(base)
            # 0.0 is a legitimate CVSS base score (severity NONE), so this is a range check and
            # not a truthiness check. `if not base` would silently discard it and report unknown.
            if not 0.0 <= base <= 10.0:
                continue
            if best_rank is not None and rank >= best_rank:
                continue
            vector = block.get("vectorString")
            best_rank = rank
            best = CvssScore(
                score=base,
                vector=vector if isinstance(vector, str) and vector.strip() else None,
                source=source.value,
            )

    return best
