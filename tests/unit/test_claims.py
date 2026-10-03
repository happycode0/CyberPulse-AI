"""Claims from ground truth (worker/publish/claims.py): what each register lets the site say, the
evidence and confidence it says it with, and that stored claims keep their place."""

from datetime import UTC, date, datetime

from worker.models import (
    AdvisoryPackage,
    AdvisoryRef,
    Claim,
    CveRef,
    CvssScore,
    EpssScore,
    Event,
    EvidenceClass,
    KevEntry,
    Risk,
    SourceRef,
)
from worker.publish.claims import (
    MAX_FIX_CLAIMS,
    corroboration_claim,
    cvss_claim,
    default_weights,
    epss_claim,
    fact_claims,
    fix_claims,
    kev_claim,
    with_fact_claims,
)

T0 = datetime(2026, 10, 3, 2, 0, tzinfo=UTC)
W = default_weights()
LOG4J = "CVE-2021-44228"


def cve(cve_id=LOG4J, *, score=None, vector=None, source="cna", epss=None, kev=None, adv=()):
    return CveRef(
        id=cve_id,
        cvss=CvssScore(score=score, vector=vector, source=source) if score is not None else None,
        epss=EpssScore(score=epss, status="known") if epss is not None else EpssScore(),
        kev=KevEntry(listed=True, date_added=kev[0], due_date=kev[1]) if kev else KevEntry(),
        advisories=list(adv),
    )


def source(source_id, *, lineage=None, independent=True):
    return SourceRef(
        source_id=source_id,
        url=f"https://{source_id}.example/story",
        evidence_class=EvidenceClass.NEWS,
        lineage_id=lineage,
        independent=independent,
    )


def event(*cves, sources=(), claims=(), confidence=0.7):
    return Event(
        event_id="evt-2026-000001",
        first_seen=T0,
        last_seen=T0,
        title="Log4Shell exploited again",
        summary="Attackers are exploiting Log4j.",
        cves=list(cves),
        sources=list(sources) or [source("wire")],
        claims=list(claims),
        risk=Risk(urgency=0.9, confidence=confidence, novelty=0.5, prominence=0.6),
    )


def advisory(adv_id, *packages, source="ghsa", reviewed=True):
    return AdvisoryRef(
        id=adv_id,
        source=source,
        reviewed=reviewed,
        packages=[AdvisoryPackage(ecosystem=e, name=n, fixed=list(f)) for e, n, f in packages],
        url=f"https://example.test/{adv_id}",
    )


# ─── Each register ────────────────────────────────────────────────────────────────────────────────


def test_a_kev_listing_names_its_dates_and_cites_the_catalogue():
    c = kev_claim(event(cve(kev=(date(2021, 12, 10), date(2021, 12, 24)))), W)
    assert c == Claim(
        text=(
            "CISA lists CVE-2021-44228 as exploited in the wild, added 2021-12-10, with US "
            "federal agencies due to fix it by 2021-12-24."
        ),
        confidence=W[EvidenceClass.AUTHORITATIVE],
        evidence=["cisa_kev"],
    )


def test_many_kev_listings_are_one_claim_naming_the_first_few():
    listed = [cve(f"CVE-2026-{i:04d}", kev=(date(2026, 9, 1), None)) for i in range(1, 7)]
    c = kev_claim(event(*listed, cve("CVE-2026-0099")), W)
    assert c is not None and c.text == (
        "CISA lists CVE-2026-0001, CVE-2026-0002, CVE-2026-0003, CVE-2026-0004 and 2 more as "
        "exploited in the wild."
    )
    assert kev_claim(event(cve()), W) is None


def test_the_highest_cvss_score_is_claimed_with_its_version_band_and_scorer():
    e = event(
        cve(LOG4J, score=10.0, vector="CVSS:3.1/AV:N/AC:L", source="cna"),
        cve("CVE-2021-45046", score=9.0, vector="CVSS:3.1/AV:N/AC:H", source="cisa_adp"),
        cve("CVE-2021-45105"),
    )
    c = cvss_claim(e, W)
    assert c is not None
    assert c.text == (
        "CVE-2021-44228 is rated 10.0 (critical) under CVSS 3.1 by the CNA that assigned it, "
        "the highest of the 2 scored CVEs here."
    )
    assert c.confidence == W[EvidenceClass.PRIMARY] and c.evidence == ["cve_record:CVE-2021-44228"]
    nvd = cvss_claim(event(cve(score=5.3, source="nvd")), W)
    assert nvd is not None
    assert nvd.text == "CVE-2021-44228 is rated 5.3 (medium) under CVSS by NVD."
    assert nvd.confidence == W[EvidenceClass.AUTHORITATIVE] and nvd.evidence == [f"nvd:{LOG4J}"]


def test_a_score_from_no_known_scorer_is_not_claimed():
    assert cvss_claim(event(cve(score=7.5, source="unknown")), W) is None


def test_epss_is_claimed_as_a_percentage_and_as_a_prediction():
    c = epss_claim(event(cve(epss=0.97421)), W)
    assert c is not None and c.text == (
        "FIRST's EPSS puts the chance of CVE-2021-44228 being exploited in the next 30 days at 97%."
    )
    assert c.confidence == W[EvidenceClass.SPECIALIST] and c.evidence == [f"first_epss:{LOG4J}"]
    low = epss_claim(event(cve(epss=0.0004), cve("CVE-2021-45046", epss=0.031)), W)
    assert (
        low is not None
        and "CVE-2021-45046" in low.text
        and "3.1%, the highest of the 2" in (low.text)
    )
    assert epss_claim(event(cve()), W) is None


def test_fixes_come_from_the_best_advisory_and_a_package_without_one_says_so():
    log4j = ("Maven", "org.apache.logging.log4j:log4j-core", ("2.15.0", "2.12.2"))
    e = event(
        cve(
            adv=[
                advisory(LOG4J, log4j, source="osv", reviewed=False),
                advisory("GHSA-jfh8-c2jp-5v3q", log4j, ("npm", "log4js-shim", ())),
            ]
        )
    )
    claims = fix_claims(e, W)
    assert [c.text for c in claims] == [
        (
            "A fix for org.apache.logging.log4j:log4j-core (Maven) is published in 2.15.0 and "
            "2.12.2, per GHSA-jfh8-c2jp-5v3q."
        ),
        "No fixed version of log4js-shim (npm) is published yet, per GHSA-jfh8-c2jp-5v3q.",
    ]
    assert claims[0].confidence == W[EvidenceClass.AUTHORITATIVE]
    assert claims[0].evidence == ["GHSA-jfh8-c2jp-5v3q"]


def test_an_unreviewed_record_is_specialist_evidence_and_fix_claims_are_capped():
    packages = [("PyPI", f"pkg{i}", ("1.0.1",)) for i in range(MAX_FIX_CLAIMS + 2)]
    claims = fix_claims(event(cve(adv=[advisory(LOG4J, *packages, source="osv")])), W)
    assert len(claims) == MAX_FIX_CLAIMS
    assert {c.confidence for c in claims} == {W[EvidenceClass.SPECIALIST]}


def test_corroboration_counts_lineages_and_cites_the_independent_sources():
    sources = [source("a"), source("b", lineage="wire-1"), source("c", lineage="wire-1")]
    c = corroboration_claim(event(sources=[*sources, source("d", independent=False)]))
    assert c == Claim(
        text="2 independent sources report this event.", confidence=0.7, evidence=["a", "b", "c"]
    )
    assert corroboration_claim(event(sources=[source("a")])) is None
    assert corroboration_claim(event(sources=sources, confidence=None)) is None


# ─── Together ─────────────────────────────────────────────────────────────────────────────────────


def test_an_event_with_no_ground_truth_and_one_source_has_no_fact_claims():
    assert fact_claims(event(), W) == []


def test_stored_claims_come_first_and_are_not_repeated():
    kev = cve(kev=(date(2021, 12, 10), None))
    first = kev_claim(event(kev), W)
    assert first is not None
    stored = Claim(text=first.text.upper(), confidence=0.5, evidence=["analyst"])
    out = with_fact_claims(event(kev, claims=[stored]), W)
    assert out.claims == [stored]
    other = Claim(text="Our analyst saw it.", confidence=0.5, evidence=["analyst"])
    assert with_fact_claims(event(kev, claims=[other])).claims == [other, first]
