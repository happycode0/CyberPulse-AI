"""Tests for item normalisation: URLs, titles, CVE extraction, and tokenisation."""

from datetime import datetime, timedelta, timezone

import pytest

from worker.models import RawItem, NormalisedItem
from worker.pipeline.normalise import (
    canonical_url,
    normalise_title,
    extract_cves,
    tokenise,
    normalise,
)


NOW = datetime(2026, 9, 29, 12, 0, 0, tzinfo=timezone.utc)

# Fixture: a base RawItem for testing
@pytest.fixture
def item():
    return RawItem(
        source_id="test-source",
        url="https://example.com/article",
        guid="guid-123",
        title="Critical RCE in Acme Framework",
        raw_summary="A critical vulnerability has been found",
        published=NOW,
        fetched_at=NOW,
        payload_hash="hash123",
    )


@pytest.mark.parametrize("raw,expected", [
    ("https://x.com/a?utm_source=rss&utm_medium=feed", "https://x.com/a"),
    ("https://x.com/a/?fbclid=1", "https://x.com/a"),
    ("http://X.COM/A", "http://x.com/A"),        # host lowered, path preserved
    ("https://x.com/a#section", "https://x.com/a"),
])
def test_canonical_url_strips_tracking_and_normalises_host(raw, expected):
    assert canonical_url(raw) == expected


def test_normalise_title_is_case_and_punctuation_insensitive():
    assert normalise_title("Critical RCE in Acme!") == normalise_title("critical rce in acme")


def test_extract_cves_finds_all_and_dedupes_and_uppercases():
    assert extract_cves("cve-2026-0001 and CVE-2026-0001 and CVE-2025-12345") == ["CVE-2025-12345", "CVE-2026-0001"]


def test_extract_cves_ignores_malformed_ids():
    assert extract_cves("CVE-26-1 CVE-2026 CVEX-2026-1 CVE-2026-1 CVE-2026-123") == []


def test_missing_published_falls_back_to_fetched_at(item):
    n = normalise(item.model_copy(update={"published": None, "fetched_at": NOW}), now=NOW)
    assert n.published == NOW and n.published_is_estimated is True


def test_future_published_is_clamped_to_now(item):
    n = normalise(item.model_copy(update={"published": NOW + timedelta(days=30)}), now=NOW)
    assert n.published == NOW and n.published_is_estimated is True


def test_url_hash_matches_for_urls_differing_only_by_tracking_params(item):
    a = normalise(item.model_copy(update={"url": "https://x.com/a?utm_source=rss"}), now=NOW)
    b = normalise(item.model_copy(update={"url": "https://x.com/a"}), now=NOW)
    assert a.url_hash == b.url_hash


def test_tokenise_ignores_punctuation_and_case():
    assert tokenise("Critical RCE in Acme!") == tokenise("critical rce in acme")
    assert tokenise("CISA warns: Acme flaw exploited, critical!") == frozenset(
        {"cisa", "warns", "acme", "flaw", "exploited", "critical"}
    )


def test_tokenise_drops_short_tokens_and_stopwords():
    assert tokenise("The RCE in an Acme VPN") == frozenset({"rce", "acme", "vpn"})


def test_tokenise_is_idempotent_on_a_normalised_title():
    title = "Zero-Day: Acme's VPN, exploited!"
    assert tokenise(normalise_title(title)) == tokenise(title)


def test_normalised_item_tokens_match_across_punctuation_variants(item):
    a = normalise(item.model_copy(update={"title": "Critical RCE in Acme!"}), now=NOW)
    b = normalise(item.model_copy(update={"title": "Critical RCE in Acme"}), now=NOW)
    assert a.tokens == b.tokens


def test_titles_are_published_as_plain_text(item):
    from worker.pipeline.normalise import clean_title

    dta = (
        '<a href="https://www.dta.gov.au/articles/australia-joins-new-oecd-working-group" '
        'hreflang="en">Australia joins new OECD working group on agentic AI in government</a>'
    )
    assert clean_title(dta) == "Australia joins new OECD working group on agentic AI in government"
    assert clean_title("SASE Converges Network &amp; Security") == "SASE Converges Network & Security"
    assert clean_title("  Forrester Wave&trade;:  Q3  ") == "Forrester Wave™: Q3"
    assert clean_title("<br/>") == "<br/>"  # nothing but markup: kept rather than emptied

    n = normalise(item.model_copy(update={"title": dta}), now=NOW)
    assert n.title.startswith("Australia joins")
    assert n.normalised_title == normalise_title(n.title)
    assert "href" not in n.tokens
