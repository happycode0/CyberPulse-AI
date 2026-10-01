"""Turning a CVSS base score into one of the five `Severity` members.

`resolve_cvss` answers "what is this CVE's base score, and who said so". This answers the question
the site actually asks: which band does that put the event in. Keeping them apart matters because
the numeric score is published alongside the band — a reader who disagrees with the banding can see
the number it came from, so the band is a presentation of the evidence rather than a replacement
for it.

The rule from PLAN.md §2 still governs the edges: no score means `unknown`, and `unknown` is not a
quiet synonym for `low`. config/scoring.yaml already encodes that distinction by weighting
`unknown: 1.5` against `low: 1` — "unrated is not the same as harmless" — so an unscored CVE
outranks a known-trivial one, which is the behaviour we want and the reason this function must never
reach for a default.
"""

from worker.models import CvssScore, Severity, SeveritySource

# The CVSS v3.1 qualitative severity rating scale, highest band first. CVSS v4.0 uses the same
# thresholds.
#
# Two deliberate departures from the specification, both of which keep the published number
# authoritative and only affect the label beside it:
#
# 1. The spec's NONE band (exactly 0.0) has no member in `Severity`, so a 0.0 lands in LOW. That
#    overstates by exactly one band and never understates, and because scoring.yaml weights
#    `low: 1` below `unknown: 1.5`, a CVE known to have no impact still ranks beneath an unrated
#    one. Reporting it as `unknown` instead would be worse: it would throw away a real measurement
#    and file it with the hundreds of CVEs NVD has genuinely not assessed.
# 2. CVSS v2.0 defined only Low/Medium/High, topping out at 7.0, so a v2 base score of 9.5 is
#    labelled CRITICAL here where NVD would say High. The severity chain prefers v4.0, v3.1 and
#    v3.0 in that order and only falls back to v2.0, so this reaches a handful of old CVEs; the
#    vector string and the score are both published, so the stricter reading stays available.
CVSS_BANDS: tuple[tuple[float, Severity], ...] = (
    (9.0, Severity.CRITICAL),
    (7.0, Severity.HIGH),
    (4.0, Severity.MEDIUM),
    (0.0, Severity.LOW),
)


def severity_for_score(score: float | None) -> Severity:
    """Band a CVSS base score.

    Parameters
    ----------
    score : float | None
        A base score in [0, 10], or None when no container carried one.

    Returns
    -------
    Severity
        The matching band, or `Severity.UNKNOWN` for None and for anything outside [0, 10].
    """
    if score is None:
        return Severity.UNKNOWN
    # Out of range is treated as no score rather than clamped into the nearest band. A score above
    # 10 or below 0 did not come from a CVSS calculation, so the honest report is that we do not
    # know the severity — clamping would present a malformed field as a confident CRITICAL.
    if not 0.0 <= score <= 10.0:
        return Severity.UNKNOWN
    for floor, severity in CVSS_BANDS:
        if score >= floor:
            return severity
    return Severity.UNKNOWN  # unreachable: the last band's floor is 0.0


def severity_from_cvss(cvss: CvssScore | None) -> tuple[Severity, SeveritySource]:
    """Band a resolved score and name the rung of the chain it came from.

    Returns
    -------
    tuple[Severity, SeveritySource]
        `(UNKNOWN, UNKNOWN)` when there is no score. The two always travel together: a severity
        with no stated provenance is the shape that lets a guess pass for a reading, which is why
        `events.severity_source` exists as a column rather than being inferred later.
    """
    if cvss is None:
        return Severity.UNKNOWN, SeveritySource.UNKNOWN

    severity = severity_for_score(cvss.score)
    if severity is Severity.UNKNOWN:
        # The score was unusable, so whoever published it is not evidence of anything here.
        return Severity.UNKNOWN, SeveritySource.UNKNOWN

    try:
        source = SeveritySource(cvss.source)
    except ValueError:
        # `CvssScore.source` is a plain string, so it can hold a provider this enum does not know.
        # The band stands — it was computed from a real score — but the provenance is recorded as
        # unknown rather than guessed at, which is the same choice `ADP_SOURCES` makes upstream.
        source = SeveritySource.UNKNOWN
    return severity, source
