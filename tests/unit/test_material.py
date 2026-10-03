from datetime import UTC, date, datetime, timedelta

import pytest

from worker.models import (
    AdvisoryPackage,
    AdvisoryRef,
    CveRef,
    KevEntry,
    MaterialChange,
    RawItem,
    SourceConfig,
    TimelineEntry,
)
from worker.pipeline.assemble import build_new_event, plan_update
from worker.pipeline.material import (
    fix_published,
    kev_listing,
    known,
    report_changes,
    signals,
)
from worker.pipeline.normalise import normalise
from worker.pipeline.resolve import EventCandidate

NOW = datetime(2026, 10, 3, 6, 0, tzinfo=UTC)
M = MaterialChange


def source(sid="s2", *, region="us", name="Other"):
    return SourceConfig(
        id=sid, name=name, type="rss", region=region, category="news", source_class="feed",
        priority=1, lane="fast", enabled=True, url="https://example.org/feed", parser="rss",
        expected_frequency="daily",
    )


def item(title, url="https://o.example/x", *, published=NOW - timedelta(hours=1)):
    raw = RawItem(source_id="s2", url=url, title=title, published=published, fetched_at=NOW,
                  payload_hash="h")
    return normalise(raw, now=NOW)


def event(title="Acme VPN flaw CVE-2026-0042", *, region="us", **update):
    e = build_new_event(item(title, "https://example.org/a", published=NOW - timedelta(days=2)),
                        source("s1", region=region, name="Source One"), "evt-2026-000001",
                        now=NOW - timedelta(days=1))
    return e.model_copy(update=update)


@pytest.mark.parametrize(
    "headline,expected",
    [
        ("Acme VPN flaw CVE-2026-0042", set()),
        ("Citrix NetScaler flaw actively exploited", {M.EXPLOIT_CONFIRMED}),
        ("Unpatched Acme flaw exploited in the wild", {M.EXPLOIT_CONFIRMED}),
        ("Hackers exploiting Acme VPN bug to breach networks", {M.EXPLOIT_CONFIRMED}),
        ("Fortinet patches FortiOS zero-day exploited in attacks",
         {M.EXPLOIT_CONFIRMED, M.NEW_PATCH}),
        ("Acme bug exploited as a zero-day", {M.EXPLOIT_CONFIRMED}),
        ("PoC exploit released for Acme VPN bug", {M.NEW_EXPLOIT}),
        ("Researchers publish proof-of-concept for Acme flaw", {M.NEW_EXPLOIT}),
        ("Acme releases security updates for VPN flaw", {M.NEW_PATCH}),
        ("Acme VPN flaw: update now", {M.NEW_PATCH}),
        ("No patch yet for Acme VPN flaw", set()),
        ("Acme VPN flaw remains unpatched after fixes promised", set()),
        ("Acme says no evidence of exploitation; patches available", {M.NEW_PATCH}),
        ("Acme shares workarounds for VPN flaw", {M.NEW_MITIGATION}),
        ("Correction: Acme VPN flaw affects version 7 only", {M.CORRECTION}),
        ("Patching roundup for the week", set()),
    ],
)
def test_signals_read_what_a_headline_reports(headline, expected):
    assert signals(headline) == expected


def test_no_headline_reports_nothing():
    assert signals(None) == set() and signals("") == set()


def test_what_the_event_already_says_is_known():
    assert known(event(), ()) == {M.NEW_FACT}
    assert M.EXPLOIT_CONFIRMED in known(event("Acme flaw actively exploited"), ())
    assert M.NEW_PATCH in known(event(), ["acme patches vpn flaw"])


def test_a_kev_listing_or_a_published_fix_is_known():
    listed = CveRef(id="CVE-2026-0042", kev=KevEntry(listed=True, date_added=date(2026, 10, 1)))
    fixed = CveRef(
        id="CVE-2026-0042",
        advisories=[AdvisoryRef(id="GHSA-x", source="ghsa", url="https://example.org/g",
                                packages=[AdvisoryPackage(ecosystem="npm", name="acme",
                                                          fixed=["1.2.3"])])],
    )
    unfixed = CveRef(
        id="CVE-2026-0042",
        advisories=[AdvisoryRef(id="GHSA-x", source="ghsa", url="https://example.org/g",
                                packages=[AdvisoryPackage(ecosystem="npm", name="acme")])],
    )
    assert M.EXPLOIT_CONFIRMED in known(event(cves=[listed]), ())
    assert M.NEW_PATCH in known(event(cves=[fixed]), ())
    assert M.NEW_PATCH not in known(event(cves=[unfixed]), ())


def test_a_report_saying_something_new_is_a_timeline_entry():
    [entry] = report_changes(event(), (), "Acme VPN flaw actively exploited", source(), when=NOW)
    assert entry == TimelineEntry(timestamp=NOW, type=M.EXPLOIT_CONFIRMED,
                                  summary="Other: Acme VPN flaw actively exploited",
                                  sources=["s2"])


def test_a_report_repeating_what_the_event_said_changes_nothing():
    e = event("Acme VPN flaw actively exploited")
    assert report_changes(e, (), "Acme VPN bug actively exploited", source(), when=NOW) == []
    tl = [*e.timeline, TimelineEntry(timestamp=NOW, type=M.NEW_PATCH, summary="x")]
    assert report_changes(event(timeline=tl), (), "Acme patches VPN flaw", source(),
                          when=NOW) == []


def test_two_changes_in_one_headline_are_two_entries_in_a_fixed_order():
    found = report_changes(event(), (), "Acme patches VPN zero-day exploited in attacks",
                           source(), when=NOW)
    assert [t.type for t in found] == [M.EXPLOIT_CONFIRMED, M.NEW_PATCH]


def test_the_first_australian_source_on_a_foreign_story_is_au_exposure():
    au = source("acsc", region="AU", name="ACSC")
    [entry] = report_changes(event(), (), "Acme VPN flaw", au, when=NOW)
    assert entry.type is M.NEW_AU_EXPOSURE
    assert entry.summary == "First reported in Australia by ACSC" and entry.sources == ["acsc"]
    # Not again, and not for a story an Australian source broke.
    seen = event(timeline=[entry])
    assert report_changes(seen, (), "Acme VPN flaw", au, when=NOW) == []
    assert report_changes(event(region="au"), (), "Acme VPN flaw", au, when=NOW) == []


def test_a_kev_listing_dates_from_midnight_utc_on_the_day_cisa_added_it():
    c = kev_listing("CVE-2026-0042", date(2026, 10, 1))
    assert c.type is M.EXPLOIT_CONFIRMED and c.at == datetime(2026, 10, 1, tzinfo=UTC)
    assert "CVE-2026-0042" in c.summary


def test_a_published_fix_dates_from_when_it_was_read():
    c = fix_published("CVE-2026-0042", "GHSA-x", now=NOW)
    assert (c.type, c.at) == (M.NEW_PATCH, NOW) and "GHSA-x" in c.summary


# --- plan_update: what a material change does to an event ----------------------------------------


def test_a_material_report_moves_the_clock_and_is_not_also_evidence():
    e = event()
    u = plan_update(e, item("Acme VPN flaw CVE-2026-0042 actively exploited"), source(), now=NOW)
    assert u.material is True
    assert [t.type for t in u.timeline] == [M.EXPLOIT_CONFIRMED]
    assert u.last_material_update == NOW - timedelta(hours=1) > e.last_material_update
    # Still a confirmation, though the timeline says what it added rather than that it agreed.
    assert u.last_independent_confirmation == NOW - timedelta(hours=1)


def test_a_repeat_is_evidence_and_not_material():
    u = plan_update(event(), item("Acme VPN flaw CVE-2026-0042"), source(), now=NOW)
    assert u.material is False and [t.type for t in u.timeline] == [M.NEW_EVIDENCE]


def test_a_headline_one_of_the_events_reports_already_had_is_not_news_again():
    e = EventCandidate(**event().model_dump(),
                       source_titles=("acme vpn flaw cve 2026 0042 exploited in the wild",))
    u = plan_update(e, item("Acme VPN flaw actively exploited"), source(), now=NOW)
    assert u.material is False
