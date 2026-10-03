"""Listing-page parsers for sources with no feed (worker/collectors/web_page.py)."""

from datetime import UTC, datetime

import pytest

from worker.collectors.web_page import PACIFIC, SYDNEY, parse_day, parse_html, parse_web_page
from worker.pipeline.run import DEFAULT_REGISTRY_PATH
from worker.sources.registry import load_registry

SOURCES = {s.id: s for s in load_registry(DEFAULT_REGISTRY_PATH)}


def parse(source_id, fixture):
    return parse_web_page(
        SOURCES[source_id], fixture(f"web_page_{SOURCES[source_id].parser}.html").read_bytes()
    )


def test_asd_news_reads_each_article_and_nothing_else(fixture):
    items = parse("asd", fixture)
    assert [i.title for i in items] == [
        "Five Eyes cyber security agencies statement",
        "ASD has open-sourced Azul to help uplift cyber defences globally again",
    ]
    first = items[0]
    assert first.url == (
        "https://www.asd.gov.au/news/2026-06-22-five-eyes-cyber-security-agencies-statement"
    )
    assert first.guid == first.url
    assert first.published == datetime(2026, 6, 22, 12, tzinfo=UTC)
    assert first.raw_summary.startswith("Our joint Five Eyes statement highlights AI")
    assert all(i.source_id == "asd" for i in items)


def test_entities_are_decoded_once(fixture):
    azul = parse("asd", fixture)[1]
    assert azul.raw_summary == (
        "ASD’s open‑source malware analysis tool is now available to defenders worldwide."
    )


def test_anthropic_news_takes_featured_and_listed_cards_once_each(fixture):
    items = parse("anthropic_news", fixture)
    assert [(i.title, i.url) for i in items] == [
        ("Introducing Claude Sonnet 5.5", "https://www.anthropic.com/claude-sonnet-5-5"),
        (
            "Barclays scales Claude to upgrade operations and improve client experience",
            "https://www.anthropic.com/news/barclays-scales-claude",
        ),
    ]
    sonnet, barclays = items
    # The day as printed, from the start of that day in San Francisco.
    assert sonnet.published == datetime(2026, 9, 28, 7, tzinfo=UTC)
    assert sonnet.raw_summary.startswith("A clear upgrade over Sonnet 5")
    assert barclays.raw_summary is None


def test_oaic_media_unwraps_the_click_tracking_redirect(fixture):
    items = parse("oaic", fixture)
    assert [i.url for i in items] == [
        "https://www.oaic.gov.au/news/media-centre/new-resources-on-transparency-for-use-of-ai-and-automated-decision-making",
        "https://www.oaic.gov.au/news/media-centre/supporting-information-integrity-on-international-access-to-information-day",
    ]
    first, second = items
    assert (
        first.title == "New resources on transparency for use of AI and automated decision-making"
    )
    assert (
        second.title
        == "Supporting information integrity on International Access to Information Day"
    )
    # 30 September in Sydney began at 14:00 UTC on the 29th.
    assert first.published == datetime(2026, 9, 29, 14, tzinfo=UTC)
    assert first.raw_summary.endswith("individuals’ rights or interests")


def test_a_redirect_off_the_publishers_site_is_not_taken(fixture):
    assert "Somewhere else" not in [i.title for i in parse("oaic", fixture)]


def test_a_link_that_is_not_https_is_dropped():
    asd = SOURCES["asd"]
    body = (
        b'<article class="node--type-news"><a href="javascript:alert(1)"><h3>One</h3></a></article>'
        b'<article class="node--type-news"><a href="http://www.asd.gov.au/x"><h3>Two</h3></a></article>'
        b'<article class="node--type-news"><a href="/news/three"><h3>Three</h3></a></article>'
    )
    assert [i.title for i in parse_web_page(asd, body)] == ["Three"]


def test_a_redesigned_page_yields_nothing_rather_than_guesses():
    """No matching cards means no items, which source health reports as EMPTY."""
    body = b"<html><body><div class='new-layout'><a href='/news/x'>A story</a></div></body></html>"
    for source_id in ("asd", "anthropic_news", "oaic"):
        assert parse_web_page(SOURCES[source_id], body) == []


def test_an_empty_body_yields_nothing():
    assert parse_web_page(SOURCES["asd"], b"") == []


def test_an_unknown_parser_is_a_configuration_error():
    source = SOURCES["asd"].model_copy(update={"parser": "nobody"})
    with pytest.raises(KeyError, match="no web page parser named 'nobody'"):
        parse_web_page(source, b"<html/>")


def test_the_same_page_hashes_the_same_and_an_edit_does_not():
    asd = SOURCES["asd"]
    page = b'<article class="node--type-news"><a href="/n/1"><h3>%s</h3></a></article>'
    [a], [b], [c] = (parse_web_page(asd, page % t) for t in (b"One", b"One", b"One!"))
    assert a.payload_hash == b.payload_hash != c.payload_hash


@pytest.mark.parametrize(
    ("text", "tz", "expected"),
    [
        ("30 September 2026", SYDNEY, datetime(2026, 9, 29, 14, tzinfo=UTC)),
        ("22 Jun 2026", SYDNEY, datetime(2026, 6, 21, 14, tzinfo=UTC)),
        ("Sep 28, 2026", PACIFIC, datetime(2026, 9, 28, 7, tzinfo=UTC)),
        ("  October  2, 2026 ", PACIFIC, datetime(2026, 10, 2, 7, tzinfo=UTC)),
        # Daylight saving: Sydney is UTC+10 in June and UTC+11 from October.
        ("10 October 2026", SYDNEY, datetime(2026, 10, 9, 13, tzinfo=UTC)),
    ],
)
def test_a_printed_day_is_the_start_of_that_day_where_the_publisher_is(text, tz, expected):
    assert parse_day(text, tz) == expected


def test_an_unreadable_day_is_none():
    assert parse_day("last Tuesday", SYDNEY) is None


def test_script_text_and_void_elements_do_not_disturb_the_tree():
    root = parse_html(
        b"<div class='a'><script>if (a < b) { x = '</div>' }</script>"
        b"<img src=x><br><p>Kept <b>text</b></p></div><p>After</p>"
    )
    div = root.find("div", cls="a")
    assert div.text() == "Kept text"
    assert [p.text() for p in root.find_all("p")] == ["Kept text", "After"]


def test_an_end_tag_nothing_opened_is_ignored():
    root = parse_html(b"<div><p>One</span></p><p>Two</p></div>")
    assert [p.text() for p in root.find("div").find_all("p")] == ["One", "Two"]
