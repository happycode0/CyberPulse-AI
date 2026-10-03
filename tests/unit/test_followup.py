"""worker/pipeline/followup.py: DECKARD's reports, checked as if hostile."""

from datetime import date

import pytest

from worker.models import MaterialChange
from worker.pipeline.followup import (
    MAX_AGENT_ENTRIES,
    MAX_CHANGES,
    EventContext,
    ReportRejected,
    check_report,
    clean_url,
)

TODAY = date(2026, 10, 3)
EVENT = EventContext(
    cves=frozenset({"CVE-2026-1234"}),
    types=frozenset({MaterialChange.NEW_FACT, MaterialChange.NEW_PATCH}),
    summaries=frozenset({"first reported by triskele labs"}),
    agent_entries=0,
    first_seen=date(2026, 9, 28),
)
SUMMARY = "Exploitation of CVE-2026-1234 was confirmed against two hospitals in Victoria."
URL = "https://www.cyber.gov.au/about-us/view-all-content/alerts-and-advisories/x"


def change(**overrides) -> dict:
    return {"type": "EXPLOIT_CONFIRMED", "summary": SUMMARY, "url": URL, **overrides}


def check(body, *, kind="check", event=EVENT):
    return check_report(body, kind=kind, event=event, today=TODAY)


def test_no_change_is_a_report():
    report = check({"outcome": "no_change"})
    assert (report.outcome, report.changes, report.refused) == ("no_change", (), ())


def test_a_change_is_kept_cleaned():
    report = check({"outcome": "changed", "changes": [
        change(summary="  " + SUMMARY + "\u200b ", url=URL + "?utm_source=x&token=abc#top",
               date="2026-10-02")
    ]})
    [kept] = report.changes
    assert kept.type is MaterialChange.EXPLOIT_CONFIRMED
    assert kept.summary == SUMMARY
    assert kept.url == URL  # query and fragment dropped
    assert kept.happened_on == date(2026, 10, 2)
    assert report.refused == ()


@pytest.mark.parametrize(
    "body, says",
    [
        ([], "JSON object"),
        ("no_change", "JSON object"),
        ({}, "needs outcome"),
        ({"outcome": "summary", "summary": "x" * 50}, "no_change or changed"),
        ({"outcome": "resolved"}, "no_change or changed"),
        ({"outcome": "no_change", "status": "resolved"}, "unknown field 'status'"),
        ({"outcome": "no_change", "changes": [change()]}, "carries no changes"),
        ({"outcome": "changed"}, f"1 to {MAX_CHANGES}"),
        ({"outcome": "changed", "changes": []}, f"1 to {MAX_CHANGES}"),
        ({"outcome": "changed", "changes": [change()] * (MAX_CHANGES + 1)}, f"1 to {MAX_CHANGES}"),
        ({"outcome": "changed", "changes": change()}, f"1 to {MAX_CHANGES}"),
        ({"outcome": "changed", "changes": ["x"]}, "change 0 must be a JSON object"),
        ({"outcome": "changed", "changes": [{"type": "NEW_PATCH"}]}, "change 0 needs"),
        ({"outcome": "changed", "changes": [change(status="x")]}, "unknown field"),
        ({"outcome": "changed", "changes": [change(summary=7)]}, "must be a string"),
        ({"outcome": "changed", "summary": SUMMARY, "changes": [change()]}, "in its change"),
    ],
)
def test_a_report_of_the_wrong_shape_is_rejected_whole(body, says):
    with pytest.raises(ReportRejected, match=says):
        check(body)


@pytest.mark.parametrize(
    "overrides, says",
    [
        ({"type": "NO_MATERIAL_CHANGE"}, "type must be one of"),
        ({"type": "NEW_EVIDENCE"}, "type must be one of"),
        ({"type": "SEVERITY_UP"}, "type must be one of"),
        ({"type": "NEW_PATCH"}, "already has NEW_PATCH"),
        ({"summary": "Too short."}, "20 to 280"),
        ({"summary": "A" * 281}, "20 to 280"),
        ({"summary": "One thing happened. Then another. Then a third."}, "at most 2 sentences"),
        ({"summary": "See https://evil.example/x for the full story."}, "may not contain a URL"),
        ({"summary": "See evil.example/x for the full story, it says."}, "may not contain a URL"),
        ({"summary": "CVE-2026-9999 was exploited at two hospitals."}, "names a CVE"),
        ({"summary": "First reported by Triskele Labs"}, "already says this"),
        ({"summary": "The key ghp_" + "a" * 36 + " was leaked."}, "credential"),
        ({"url": "http://www.cyber.gov.au/x"}, "https"),
        ({"url": "https://192.168.128.39/x"}, "host name"),
        ({"url": "https://localhost/x"}, "host name"),
        ({"url": "https://user:pw@example.org/x"}, "user name"),
        ({"url": "https://example.org:8443/x"}, "port"),
        ({"url": "https://example.org/a b"}, "no spaces"),
        ({"url": "https://example.org/" + "a" * 300}, "at most 300"),
        ({"url": "javascript:alert(1)"}, "https"),
        ({"date": "03/10/2026"}, "YYYY-MM-DD"),
        ({"date": "2026-02-30"}, "not a real date"),
        ({"date": "2026-10-04"}, "in the future"),
        ({"date": "2026-01-01"}, "long before"),
        ({"date": 20261002}, "YYYY-MM-DD"),
    ],
)
def test_a_change_that_fails_a_check_is_refused_with_the_reason(overrides, says):
    report = check({"outcome": "changed", "changes": [change(**overrides), change(
        type="NEW_MITIGATION", summary="Microsoft published a registry workaround for it.")]})
    [refused] = report.refused
    assert refused.index == 0 and says in refused.reason
    assert [c.type for c in report.changes] == [MaterialChange.NEW_MITIGATION]


def test_one_report_cannot_say_the_same_thing_twice():
    report = check({"outcome": "changed", "changes": [
        change(), change(), change(type="NEW_TARGET"),
    ]})
    assert [c.type for c in report.changes] == [MaterialChange.EXPLOIT_CONFIRMED]
    assert [r.index for r in report.refused] == [1, 2]
    assert "already has EXPLOIT_CONFIRMED" in report.refused[0].reason
    assert "already says this" in report.refused[1].reason


def test_an_event_takes_only_so_many_agent_entries():
    full = EventContext(**{**EVENT.__dict__, "agent_entries": MAX_AGENT_ENTRIES - 1})
    report = check({"outcome": "changed", "changes": [
        change(), change(type="NEW_TARGET", summary="Two more hospitals in Victoria were hit.")
    ]}, event=full)
    assert len(report.changes) == 1
    assert f"{MAX_AGENT_ENTRIES} entries" in report.refused[0].reason


def test_a_final_summary():
    text = ("A ransomware crew exploited CVE-2026-1234 in a hospital records system. "
            "The vendor patched it within a week and no new victims were reported.")
    report = check({"outcome": "summary", "summary": text}, kind="final_summary")
    assert (report.outcome, report.summary, report.changes) == ("summary", text, ())


@pytest.mark.parametrize(
    "body, says",
    [
        ({"outcome": "no_change"}, "outcome summary"),
        ({"outcome": "summary"}, "must be a string"),
        ({"outcome": "summary", "summary": "Too short to say anything."}, "40 to 600"),
        ({"outcome": "summary", "summary": "It was fixed. " * 5}, "at most 4 sentences"),
        ({"outcome": "summary", "summary": SUMMARY, "changes": []}, "carries no changes"),
    ],
)
def test_a_bad_final_summary_is_rejected(body, says):
    with pytest.raises(ReportRejected, match=says):
        check(body, kind="final_summary")


@pytest.mark.parametrize(
    "url, expected",
    [
        ("https://Example.ORG", "https://example.org/"),
        ("https://example.org./a/b", "https://example.org/a/b"),
        ("https://example.org:443/a", "https://example.org/a"),
        ("https://example.org/a?session=1#x", "https://example.org/a"),
    ],
)
def test_clean_url(url, expected):
    assert clean_url(url) == expected
