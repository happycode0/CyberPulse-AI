from datetime import UTC, datetime, timedelta
from pathlib import Path

from worker.pipeline.lineage import (
    Publishers,
    Report,
    assign,
    last_independent_confirmation,
)
from worker.sources.registry import load_publishers, load_registry

NOW = datetime(2026, 10, 3, 4, 0, tzinfo=UTC)
REGISTRY = Path("config/sources.yaml")
# The real registry: ACSC's feeds are ASD's, CISA's are CISA's, and the two agencies have names.
PUBLISHERS = Publishers.from_registry(load_registry(REGISTRY), load_publishers(REGISTRY))


def report(n, source_id, title, *, hours=0):
    return Report(n, source_id, title, NOW + timedelta(hours=hours))


def lineages(reports, publishers=PUBLISHERS):
    """`source_id: (lineage, independent)` per report, in the reports' order."""
    got = assign(reports, publishers)
    return [(r.source_id, got[r.id].lineage, got[r.id].independent) for r in reports]


def test_one_organisations_feeds_are_one_voice():
    reports = [
        report(1, "acsc_publications", "Agentic AI harnesses"),
        report(2, "acsc_news", "ASD releases new guidance on the agentic AI harnesses", hours=1),
    ]
    assert lineages(reports) == [
        ("acsc_publications", "asd", True),
        ("acsc_news", "asd", False),
    ]
    assert last_independent_confirmation(reports, assign(reports, PUBLISHERS)) is None


def test_a_headline_relaying_an_agency_on_the_event_is_that_agencys():
    reports = [
        report(1, "cisa_kev", "CVE-2026-5430: WSO2 Multiple Products Path Traversal"),
        report(
            2,
            "thn",
            "WSO2 and Adobe Commerce Flaws Exploited in Attacks, Added to CISA KEV",
            hours=3,
        ),
        report(3, "acsm", "ACSC warns of actively exploited WSO2 flaw", hours=4),
    ]
    # ACSC never reported it here: acsm's headline names it, but relays nothing on the event.
    assert lineages(reports) == [
        ("cisa_kev", "cisa", True),
        ("thn", "cisa", False),
        ("acsm", "mysecuritymedia", True),
    ]


def test_an_agencys_own_report_counts_even_when_a_relay_landed_first():
    reports = [
        report(1, "thn", "CISA Says Attackers Are Exploiting Two Critical Citrix NetScaler Flaws"),
        report(
            2, "cisa_advisories", "Critical Zero-Day Vulnerabilities in Citrix NetScaler", hours=2
        ),
    ]
    assert lineages(reports) == [("thn", "cisa", False), ("cisa_advisories", "cisa", True)]


def test_an_agency_naming_another_speaks_for_itself():
    reports = [
        report(1, "acsc_alerts", "CRITICAL ALERT: Citrix NetScaler vulnerabilities"),
        report(
            2, "cisa_advisories", "CISA and ASD's ACSC Release Joint Guidance on NetScaler", hours=1
        ),
    ]
    assert lineages(reports) == [("acsc_alerts", "asd", True), ("cisa_advisories", "cisa", True)]


def test_vendor_names_are_not_read_as_relays():
    """A vendor named in a headline ("Microsoft") is as often the product as the one speaking."""
    reports = [
        report(1, "microsoft_security", "Tracking CVE-2026-73570 on internet-facing mail servers"),
        report(2, "thn", "Microsoft Warns Attackers Exploit Zimbra Flaw", hours=2),
    ]
    assert lineages(reports) == [("microsoft_security", "microsoft", True), ("thn", "thn", True)]


def test_a_word_for_word_copy_from_another_publisher_is_the_originals():
    reports = [
        report(1, "thn", "Kiteworks Urges Customers to Shut Down Systems"),
        report(2, "cyber_daily", "Kiteworks urges customers to shut down systems!", hours=5),
        report(3, "therecord", "Kiteworks tells customers to pull the plug", hours=6),
    ]
    assert lineages(reports) == [
        ("thn", "thn", True),
        ("cyber_daily", "thn", False),
        ("therecord", "therecord", True),
    ]
    assert last_independent_confirmation(reports, assign(reports, PUBLISHERS)) == NOW + timedelta(
        hours=6
    )


def test_a_short_headline_two_outlets_share_is_not_a_copy():
    reports = [
        report(1, "nvd", "CVE-2026-1234"),
        report(2, "osv", "CVE-2026-1234", hours=1),
        report(3, "thn", "Weekly update", hours=2),
        report(4, "therecord", "Weekly Update", hours=3),
    ]
    assert all(independent for _, _, independent in lineages(reports))


def test_a_copy_of_a_relay_is_the_agencys():
    reports = [
        report(1, "cisa_kev", "CVE-2026-76504: Cisco Catalyst SD-WAN Manager"),
        report(2, "bleepingcomputer", "CISA orders agencies to patch SD-WAN flaw", hours=1),
        report(3, "securitybrief_au", "CISA orders agencies to patch SD-WAN flaw", hours=2),
    ]
    assert [line for _, line, _ in lineages(reports)] == ["cisa", "cisa", "cisa"]


def test_without_publishers_each_source_is_its_own_lineage():
    reports = [
        report(1, "acsc_alerts", "HIGH ALERT: Risks of AI misalignment"),
        report(
            2, "acsm", "ACSC warns Australian organisations about risks of AI misalignment", hours=1
        ),
        report(3, "acsm", "AI misalignment: what the ACSC alert means", hours=2),
    ]
    assert lineages(reports, Publishers.none()) == [
        ("acsc_alerts", "acsc_alerts", True),
        ("acsm", "acsm", True),
        ("acsm", "acsm", False),
    ]
    # With the registry, acsm relays ASD's alert both times.
    assert lineages(reports) == [
        ("acsc_alerts", "asd", True),
        ("acsm", "asd", False),
        ("acsm", "asd", False),
    ]


def test_the_netscaler_story_has_five_voices_not_nine():
    """evt-2026-002001 as it stood on VM 200 on 2026-10-03."""
    reports = [
        report(1, "cisa_kev", "CVE-2026-88772: Citrix NetScaler Improper Restriction"),
        report(2, "cisa_kev", "CVE-2026-88771: Citrix NetScaler Improper Input Validation"),
        report(
            3,
            "acsc_alerts",
            "CRITICAL ALERT: Critical vulnerabilities in Citrix NetScaler",
            hours=1,
        ),
        report(
            4,
            "cisa_advisories",
            "Critical Zero-Day Vulnerabilities Exploited in NetScaler",
            hours=2,
        ),
        report(
            5,
            "acsm",
            "ACSC warns of actively exploited vulnerabilities in Citrix NetScaler",
            hours=3,
        ),
        report(
            6, "triskele", "Citrix NetScaler ADC Remote Code Execution Vulnerabilities", hours=4
        ),
        report(
            7,
            "thn",
            "CISA Says Attackers Are Exploiting Two Critical Citrix NetScaler Flaws",
            hours=5,
        ),
        report(
            8, "thn", "Citrix NetScaler CVE-2026-88772 Exploit Details Show Pre-Auth Path", hours=6
        ),
        report(9, "unit42", "Threat Brief: NetScaler Zero Days Exploited in the Wild", hours=7),
    ]
    independent = [(s, line) for s, line, ind in lineages(reports) if ind]
    assert independent == [
        ("cisa_kev", "cisa"),
        ("acsc_alerts", "asd"),
        ("triskele", "triskele"),
        ("thn", "thn"),
        ("unit42", "unit42"),
    ]


def test_the_registry_names_its_agencies_and_their_feeds():
    assert {PUBLISHERS.of(s) for s in ("acsc_alerts", "acsc_news", "asd", "acsc_advice")} == {"asd"}
    assert {PUBLISHERS.of(s) for s in ("cisa_kev", "cisa_advisories", "cisa_ics")} == {"cisa"}
    assert PUBLISHERS.of("thn") == "thn"
    assert PUBLISHERS.named_in("Added to CISA KEV") == {"cisa"}
    assert PUBLISHERS.named_in("ASD's ACSC warns") == {"asd"}
    assert PUBLISHERS.named_in("Cisa and asd, in lower case") == set()
    assert set(PUBLISHERS.patterns) == {"asd", "cisa"}
