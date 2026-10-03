import hashlib
from datetime import UTC, datetime, timedelta

from worker.models import CveRef, Event, EvidenceClass, RawItem, SourceRef
from worker.pipeline.normalise import normalise
from worker.pipeline.resolve import (
    Decision,
    EventCandidate,
    token_overlap,
    resolve,
)

NOW = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)
SOURCE = "acsc-alerts"


def make_item(
    title="Acme VPN gateway zero-day exploited in the wild",
    *,
    url="https://example.org/news/acme-vpn-zero-day",
    guid=None,
    published=NOW,
    source_id=SOURCE,
):
    raw = RawItem(
        source_id=source_id,
        url=url,
        guid=guid,
        title=title,
        published=published,
        fetched_at=published,
        payload_hash="p",
    )
    return normalise(raw, now=NOW + timedelta(days=365))


def make_event(
    title="Acme VPN gateway zero-day exploited in the wild",
    *,
    first_seen=NOW,
    last_seen=None,
    event_id="evt-2026-000001",
    cves=(),
    sources=(),
    **candidate_fields,
):
    return EventCandidate(
        event_id=event_id,
        first_seen=first_seen,
        last_seen=last_seen or first_seen,
        title=title,
        summary="s",
        cves=[CveRef(id=c) for c in cves],
        sources=list(sources),
        **candidate_fields,
    )


def source_ref(url, source_id=SOURCE, published=None):
    return SourceRef(
        source_id=source_id,
        url=url,
        published=published,
        evidence_class=EvidenceClass.NEWS,
    )


def words(prefix, n):
    return " ".join(f"{prefix}{i:02d}x" for i in range(n))


def titles_with_overlap(shared, only_item, only_event):
    """Titles whose token sets have Jaccard shared / (shared + only_item + only_event)."""
    common = words("common", shared)
    return (
        f"{common} {words('item', only_item)}".strip(),
        f"{common} {words('event', only_event)}".strip(),
    )


# --- identity rungs -------------------------------------------------------------


def test_identical_url_hash_is_duplicate():
    item = make_item()
    event = make_event(source_url_hashes=frozenset({item.url_hash}))
    r = resolve(item, [event])
    assert (r.decision, r.method, r.event_id) == (Decision.DUPLICATE, "url_hash", event.event_id)


def test_url_hash_is_derived_from_plain_event_source_urls():
    item = make_item(url="https://example.org/a?utm_source=x")
    event = Event(
        event_id="evt-2026-000002",
        first_seen=NOW,
        last_seen=NOW,
        title="Something else entirely",
        summary="s",
        sources=[source_ref("https://EXAMPLE.org/a/")],
    )
    r = resolve(item, [event])
    assert (r.decision, r.method) == (Decision.DUPLICATE, "url_hash")


def test_matching_guid_is_duplicate():
    item = make_item(guid="tag:acsc,2026:42")
    event = make_event("Unrelated title words", source_guids=frozenset({(SOURCE, "tag:acsc,2026:42")}))
    r = resolve(item, [event])
    assert (r.decision, r.method) == (Decision.DUPLICATE, "guid")


def test_guid_from_a_different_source_is_not_a_match():
    """Feed GUIDs such as '1' or '42' are only unique within one feed."""
    item = make_item(guid="42")
    event = make_event("Unrelated title words", source_guids=frozenset({("other-source", "42")}))
    assert resolve(item, [event]).decision is Decision.NEW_EVENT


def test_same_url_over_http_and_https_is_duplicate_by_canonical_url():
    item = make_item(url="https://www.example.org/news/story")
    event = make_event(
        "Unrelated title words",
        source_canonical_urls=frozenset({"http://example.org/news/story"}),
        source_url_hashes=frozenset({hashlib.sha256(b"http://example.org/news/story").hexdigest()}),
    )
    r = resolve(item, [event])
    assert (r.decision, r.method) == (Decision.DUPLICATE, "canonical_url")


def test_url_identity_is_not_bounded_by_the_window():
    """The same URL is the same item, however old the event is."""
    item = make_item()
    event = make_event(
        first_seen=NOW - timedelta(days=200),
        source_url_hashes=frozenset({item.url_hash}),
    )
    assert resolve(item, [event]).decision is Decision.DUPLICATE


def test_identity_rungs_outrank_title_rungs():
    item = make_item()
    by_title = make_event(event_id="evt-2026-000001", first_seen=NOW - timedelta(hours=1))
    by_url = make_event(
        "Different", event_id="evt-2026-000002", source_url_hashes=frozenset({item.url_hash})
    )
    r = resolve(item, [by_title, by_url])
    assert (r.decision, r.event_id) == (Decision.DUPLICATE, "evt-2026-000002")


# --- title_hash and the window --------------------------------------------------


def test_same_normalised_title_within_window_updates_existing():
    item = make_item()
    event = make_event(item.title, first_seen=item.published - timedelta(hours=6))
    r = resolve(item, [event])
    assert (r.decision, r.method, r.event_id) == (
        Decision.UPDATE_EXISTING,
        "title_hash",
        event.event_id,
    )


def test_punctuation_and_case_do_not_defeat_the_title_match():
    item = make_item("ACME VPN Gateway: Zero-Day Exploited In The Wild!")
    event = make_event("acme vpn gateway zeroday exploited in the wild")
    assert resolve(item, [event]).method == "title_hash"


def test_title_hash_window_is_seventy_two_hours_inclusive():
    item = make_item()
    inside = make_event(first_seen=item.published - timedelta(hours=72))
    outside = make_event(first_seen=item.published - timedelta(hours=72, seconds=1))
    assert resolve(item, [inside]).decision is Decision.UPDATE_EXISTING
    assert resolve(item, [outside]).decision is Decision.NEW_EVENT


def test_identical_title_two_to_three_days_apart_still_merges():
    """A corrected or next-day re-publish is what the title_hash rung exists to catch."""
    item = make_item()
    for delta in (timedelta(hours=30), timedelta(days=2, hours=12)):
        r = resolve(item, [make_event(first_seen=item.published - delta)])
        assert (r.decision, r.method) == (Decision.UPDATE_EXISTING, "title_hash")


def test_weekly_recurring_identical_title_is_a_new_event():
    """SANS-style weekly roundups repeat every ~7 days with the same title (plan line 39)."""
    item = make_item("SANS NewsBites weekly roundup")
    last_week = make_event("SANS NewsBites weekly roundup", first_seen=NOW - timedelta(days=7))
    r = resolve(item, [last_week])
    assert (r.decision, r.event_id, r.method) == (Decision.NEW_EVENT, None, "none")


def test_item_older_than_the_event_matches_within_the_title_window():
    """Backfilled or late-fed articles can pre-date the event's first_seen."""
    item = make_item(published=NOW - timedelta(days=2))
    event = make_event(first_seen=NOW)
    assert resolve(item, [event]).decision is Decision.UPDATE_EXISTING


def test_title_similarity_alone_beyond_the_window_is_not_a_match():
    item = make_item()
    old = make_event(item.title, first_seen=item.published - timedelta(days=45))
    r = resolve(item, [old])
    assert (r.decision, r.event_id, r.method) == (Decision.NEW_EVENT, None, "none")


# --- Review Focus #4: recurring titles must not false-merge ---------------------


def test_recurring_title_in_a_different_period_is_a_new_event():
    """Patch Tuesday repeats monthly with a near-identical title."""
    old = make_event(
        "microsoft patch tuesday september 2026", first_seen=datetime(2026, 9, 8, tzinfo=UTC)
    )
    new = make_item(
        "Microsoft Patch Tuesday October 2026", published=datetime(2026, 10, 13, tzinfo=UTC)
    )
    assert resolve(new, [old]).decision is Decision.NEW_EVENT


def test_recurring_identical_title_does_not_chain_onto_a_still_active_event():
    """An event kept alive by follow-ups (recent last_seen) must not absorb next period's
    identically titled item: the window is measured from the event's origin."""
    item = make_item("Weekly vulnerability digest")
    alive = make_event(
        "Weekly vulnerability digest",
        first_seen=item.published - timedelta(days=7),
        last_seen=item.published - timedelta(hours=2),
    )
    assert resolve(item, [alive]).decision is Decision.NEW_EVENT


def test_recurring_title_matches_the_current_period_not_a_stale_one():
    item = make_item("Weekly vulnerability digest")
    stale = make_event(
        "Weekly vulnerability digest",
        event_id="evt-2026-000001",
        first_seen=item.published - timedelta(days=7),
    )
    current = make_event(
        "Weekly vulnerability digest",
        event_id="evt-2026-000002",
        first_seen=item.published - timedelta(days=1),
    )
    r = resolve(item, [stale, current])
    assert (r.decision, r.event_id) == (Decision.UPDATE_EXISTING, "evt-2026-000002")


def test_out_of_window_similarity_does_not_reach_the_token_rungs_either():
    """Identical tokens 35 days apart: no title rung may fire, not even AMBIGUOUS."""
    item = make_item("Microsoft Patch Tuesday October 2026", published=NOW)
    old = make_event(
        "Microsoft Patch Tuesday October 2026 revisited",
        first_seen=NOW - timedelta(days=35),
    )
    r = resolve(item, [old])
    assert (r.decision, r.method) == (Decision.NEW_EVENT, "none")


def test_event_origin_is_its_earliest_source_publication():
    """first_seen may be a late fetch time; the earliest source date is the true origin."""
    item = make_item()
    event = make_event(
        item.title,
        first_seen=NOW - timedelta(days=1),
        sources=[source_ref("https://example.org/old", published=NOW - timedelta(days=40))],
    )
    assert resolve(item, [event]).decision is Decision.NEW_EVENT


# --- CVE + tokens ---------------------------------------------------------------


def test_shared_cve_and_high_token_overlap_updates_existing():
    item_title, event_title = titles_with_overlap(6, 1, 1)  # 6/8 = 0.75
    # Each names a CVE the other doesn't, so `cve_set` does not apply.
    item = make_item(f"CVE-2026-88772 CVE-2026-90001 {item_title}", published=NOW)
    event = make_event(
        f"CVE-2026-88772 {event_title}",
        first_seen=NOW - timedelta(days=9),
        cves=["CVE-2026-88772", "CVE-2026-90002"],
    )
    r = resolve(item, [event])
    assert (r.decision, r.method) == (Decision.UPDATE_EXISTING, "cve+tokens")


def test_shared_cve_matches_regardless_of_the_title_window():
    """A CVE id is a strong identity signal; only title-only paths are window-gated."""
    item_title, event_title = titles_with_overlap(6, 2, 2)  # 6/10 = 0.6
    item = make_item(f"{item_title} CVE-2026-88772")
    event = make_event(
        f"{event_title} CVE-2026-88772",
        first_seen=NOW - timedelta(days=45),  # past `cve_set`'s 30 days
        cves=["CVE-2026-88772"],
    )
    assert resolve(item, [event]).method == "cve+tokens"


def test_shared_cve_with_unrelated_titles_is_related_but_distinct():
    # The roundup names one more CVE, and the headlines share no word, so `cve_set` does not
    # apply either.
    item = make_item("Patch Tuesday roundup lists fixes for CVE-2026-88772 and CVE-2026-90001")
    event = make_event(
        "Threat actor weaponises the flaw in ransomware campaign",
        cves=["CVE-2026-88772"],
    )
    r = resolve(item, [event])
    assert (r.decision, r.event_id, r.method) == (
        Decision.RELATED_BUT_DISTINCT,
        event.event_id,
        "cve",
    )


def test_similar_titles_without_a_shared_cve_do_not_use_the_cve_rung():
    item_title, event_title = titles_with_overlap(6, 2, 2)  # 0.6
    item = make_item(item_title)
    event = make_event(event_title, cves=["CVE-2026-88772"])
    assert resolve(item, [event]).method != "cve+tokens"


# --- token-overlap rungs --------------------------------------------------------


def test_high_overlap_with_date_proximity_updates_existing():
    item_title, event_title = titles_with_overlap(8, 1, 1)  # 8/10 = 0.8
    item = make_item(item_title)
    event = make_event(event_title, first_seen=NOW - timedelta(hours=72))
    r = resolve(item, [event])
    assert (r.decision, r.method) == (Decision.UPDATE_EXISTING, "tokens+date")


def test_high_overlap_beyond_proximity_is_a_recurrence_not_a_doubt():
    item_title, event_title = titles_with_overlap(8, 1, 1)  # 0.8
    item = make_item(item_title)
    for delta in (timedelta(hours=73), timedelta(days=7)):
        event = make_event(event_title, first_seen=NOW - delta)
        assert resolve(item, [event]).decision is Decision.NEW_EVENT


def test_borderline_similarity_is_ambiguous_not_a_guess():
    item_title, event_title = titles_with_overlap(11, 4, 5)  # 11/20 = 0.55
    item = make_item(item_title)
    event = make_event(event_title, first_seen=NOW - timedelta(hours=5))
    r = resolve(item, [event])
    assert (r.decision, r.event_id, r.method) == (Decision.AMBIGUOUS, event.event_id, "tokens")
    assert r.score == 0.55


def test_borderline_similarity_is_ambiguous_out_to_fourteen_days_only():
    item_title, event_title = titles_with_overlap(11, 4, 5)
    item = make_item(item_title)
    inside = make_event(event_title, first_seen=NOW - timedelta(days=14))
    outside = make_event(event_title, first_seen=NOW - timedelta(days=14, seconds=1))
    assert resolve(item, [inside]).decision is Decision.AMBIGUOUS
    assert resolve(item, [outside]).decision is Decision.NEW_EVENT


def test_low_similarity_is_a_new_event():
    item_title, event_title = titles_with_overlap(3, 4, 4)  # 3/11
    item = make_item(item_title)
    event = make_event(event_title, first_seen=NOW - timedelta(hours=5))
    assert resolve(item, [event]).decision is Decision.NEW_EVENT


def test_overlap_is_computed_on_normalised_titles_so_punctuation_is_irrelevant():
    item = make_item("Critical Acme flaw exploited in the wild, warns CISA!")
    event = make_event("CISA warns: Acme flaw exploited in wild; critical", first_seen=NOW)
    r = resolve(item, [event])
    assert (r.decision, r.method) == (Decision.UPDATE_EXISTING, "tokens+date")


def test_source_article_titles_also_count_as_event_titles():
    item = make_item("Vendor confirms Acme gateway breach")
    event = make_event(
        "Something the first outlet called it",
        source_titles=("Vendor confirms Acme gateway breach",),
    )
    r = resolve(item, [event])
    assert (r.decision, r.method) == (Decision.UPDATE_EXISTING, "title_hash")


# --- misc -----------------------------------------------------------------------


def test_no_signal_creates_new_event():
    assert resolve(make_item(), []).decision is Decision.NEW_EVENT


def test_ties_go_to_the_oldest_event_then_lowest_id():
    item = make_item()
    a = make_event(event_id="evt-2026-000007", first_seen=NOW - timedelta(days=2))
    b = make_event(event_id="evt-2026-000003", first_seen=NOW - timedelta(days=1))
    c = make_event(event_id="evt-2026-000002", first_seen=NOW - timedelta(days=1))
    assert resolve(item, [a, b, c]).event_id == "evt-2026-000007"
    assert resolve(item, [b, c]).event_id == "evt-2026-000002"


def test_token_overlap_is_jaccard_and_safe_on_empty():
    assert token_overlap(frozenset("abc"), frozenset("abd")) == 0.5
    assert token_overlap(frozenset(), frozenset("abc")) == 0.0
    assert token_overlap(frozenset(), frozenset()) == 0.0


def test_match_keys_never_leak_into_the_published_shape():
    event = make_event(source_url_hashes=frozenset({"h"}), source_titles=("t",))
    dumped = event.model_dump_public()
    assert "source_url_hashes" not in dumped and "source_titles" not in dumped
    assert "source_guids" not in dumped and "source_canonical_urls" not in dumped
    Event.model_validate(dumped)


# --- generic titles ---------------------------------------------------------------


def test_generic_notice_titles_are_recognised_raw_or_normalised():
    from worker.pipeline.resolve import is_generic_title

    for title in (
        "CISA Adds Two Known Exploited Vulnerabilities to Catalog",
        "CISA Adds One Known Exploited Vulnerability to the Catalog",
        "cisa releases four industrial control systems advisories",
        "ISC Stormcast For Friday, October 2nd, 2026 https://isc.sans.edu/podcastdetail/9999",
        "Smashing Security podcast #437: The pig butcher's apprentice",
    ):
        assert is_generic_title(title), title
    assert not is_generic_title("CISA adds Citrix flaw to KEV after attacks")


def test_a_generic_title_never_merges_on_its_words():
    title = "CISA Adds Two Known Exploited Vulnerabilities to Catalog"
    item = make_item(title, url="https://example.org/kev-2")
    event = make_event(title, first_seen=NOW - timedelta(hours=20))
    assert resolve(item, [event]).decision is Decision.NEW_EVENT


# --- CVE sets -------------------------------------------------------------------


def test_the_same_cves_in_different_words_are_one_story():
    item = make_item("Zimbra mail servers hit through CVE-2026-88772")
    event = make_event(
        "Collaboration suite flaw under active attack",
        first_seen=NOW - timedelta(days=6),
        cves=["CVE-2026-88772"],
    )
    r = resolve(item, [event])
    assert (r.decision, r.method, r.score) == (Decision.UPDATE_EXISTING, "cve_set", 1.0)


def test_a_few_of_an_events_cves_need_a_shared_headline_word():
    event = make_event(
        "Citrix fixes four NetScaler flaws",
        first_seen=NOW - timedelta(days=2),
        cves=["CVE-2026-88771", "CVE-2026-88772", "CVE-2026-88773", "CVE-2026-88774"],
    )
    shares = make_item("NetScaler bug CVE-2026-88772 exploited")
    r = resolve(shares, [event])
    assert (r.decision, r.method, r.score) == (Decision.UPDATE_EXISTING, "cve_set", 0.25)
    unrelated = make_item("Ransomware crew abuses CVE-2026-88772")
    assert resolve(unrelated, [event]).decision is Decision.RELATED_BUT_DISTINCT


def test_a_generic_notice_joins_a_story_through_its_cves_but_never_absorbs_one():
    story = make_event(
        "Citrix fixes four NetScaler flaws",
        first_seen=NOW - timedelta(days=1),
        cves=["CVE-2026-88771", "CVE-2026-88772"],
    )
    notice = make_item("CISA Adds One Known Exploited Vulnerability to Catalog CVE-2026-88772")
    r = resolve(notice, [story])
    assert (r.decision, r.method) == (Decision.UPDATE_EXISTING, "cve_set")

    roundup = make_event(
        "CISA Adds Two Known Exploited Vulnerabilities to Catalog",
        first_seen=NOW - timedelta(days=1),
        cves=["CVE-2026-88772", "CVE-2026-90001"],
    )
    article = make_item("Attackers exploit CVE-2026-88772 in NetScaler")
    assert resolve(article, [roundup]).decision is Decision.RELATED_BUT_DISTINCT


def test_cve_sets_are_bounded_in_size_and_time():
    many = [f"CVE-2026-{88700 + i}" for i in range(11)]
    big = make_event("Vendor patch roundup NetScaler", cves=many)
    item = make_item(f"NetScaler flaw {many[0]}")
    assert resolve(item, [big]).method != "cve_set"
    old = make_event("Zimbra flaw", first_seen=NOW - timedelta(days=31), cves=["CVE-2026-88772"])
    assert resolve(make_item("Mail suite hit via CVE-2026-88772"), [old]).method != "cve_set"


def test_one_registers_two_advisories_never_merge_on_their_cves():
    register = SourceRef(
        source_id="cisa_ics",
        url="https://example.org/icsa-26-01",
        evidence_class=EvidenceClass.AUTHORITATIVE,
    )
    event = make_event(
        "Siemens SIMATIC advisory",
        cves=["CVE-2026-88772"],
        sources=[register],
    )
    item = make_item(
        "ABB controller advisory CVE-2026-88772",
        url="https://example.org/icsa-26-02",
        source_id="cisa_ics",
    )
    r = resolve(item, [event], evidence_class=EvidenceClass.AUTHORITATIVE)
    assert r.decision is Decision.RELATED_BUT_DISTINCT
    # Another source's report of the same CVE still joins.
    news = make_item("Mail suite hit via CVE-2026-88772", source_id="wire")
    assert resolve(news, [event], evidence_class=EvidenceClass.NEWS).method == "cve_set"


# --- the consolidation rungs ------------------------------------------------------


def test_weighted_overlap_counts_rare_words_for_more():
    from worker.pipeline.resolve import TokenWeights, weighted_overlap

    weights = TokenWeights.from_titles(
        ["ransomware attack hits firm"] * 9 + ["KillSec claims Medibank"]
    )
    assert weights.weight("ransomware") < weights.weight("killsec") < weights.weight("unseen")
    rare = weighted_overlap(
        frozenset({"killsec", "ransomware", "hospital"}),
        frozenset({"killsec", "ransomware", "clinic"}),
        weights,
    )
    common = weighted_overlap(
        frozenset({"killsec", "ransomware", "hospital"}),
        frozenset({"lockbit", "ransomware", "clinic"}),
        weights,
    )
    assert common < 0.1 < rare
    assert weighted_overlap(frozenset(), frozenset({"a"}), weights) == 0.0


def test_same_story_adds_the_weighted_rung_within_72_hours():
    from worker.pipeline.resolve import TokenWeights, same_story, story_keys

    weights = TokenWeights.from_titles(
        ["ransomware gang claims attack"] * 100 + ["KillSec Medibank"]
    )
    a = story_keys("KillSec claims Medibank ransomware attack", (), (), NOW)
    b = story_keys("Medibank confirms KillSec breach", (), (), NOW + timedelta(hours=30))
    assert same_story(a, b, weights)[0] == "weighted"
    # On a handful of headlines every word looks rare: the rung waits.
    thin = TokenWeights.from_titles(["ransomware gang claims attack"] * 20 + ["KillSec Medibank"])
    assert same_story(a, b, thin) is None
    late = story_keys("Medibank confirms KillSec breach", (), (), NOW + timedelta(hours=73))
    assert same_story(a, late, weights) is None
    # Different CVEs say different stories, whatever the words.
    a2 = story_keys("KillSec claims Medibank attack", (), ("CVE-2026-1001",), NOW)
    b2 = story_keys("Medibank confirms KillSec breach", (), ("CVE-2026-1002",), NOW)
    assert same_story(a2, b2, weights) is None
    # And one register's two items stay two.
    a3 = story_keys("KillSec claims Medibank", (), (), NOW, registers=("acsc",))
    b3 = story_keys("Medibank confirms KillSec breach", (), (), NOW, registers=("acsc",))
    assert same_story(a3, b3, weights) is None
    # Nor do two pieces only one outlet wrote: that is its house style.
    a4 = story_keys("KillSec claims Medibank ransomware attack", (), (), NOW, outlets=("wire",))
    b4 = story_keys("Medibank confirms KillSec breach", (), (), NOW, outlets=("wire",))
    assert same_story(a4, b4, weights) is None
    b5 = story_keys("Medibank confirms KillSec breach", (), (), NOW, outlets=("wire", "itnews"))
    assert same_story(a4, b5, weights)[0] == "weighted"


def test_stored_events_naming_only_different_cves_are_two_stories_on_every_rung():
    from worker.pipeline.resolve import TokenWeights, same_story, story_keys

    weights = TokenWeights.from_titles(["ransomware gang claims attack"] * 100)
    microsoft = story_keys(
        "The July 2026 Security Update Review", (), ("CVE-2026-32161", "CVE-2026-32170"), NOW
    )
    apple = story_keys(
        "The July 2026 Apple Security Update Review",
        (),
        ("CVE-2026-28819", "CVE-2026-28840"),
        NOW + timedelta(hours=10),
    )
    assert same_story(microsoft, apple, weights) is None
    # The same words with no CVEs on one side are still `tokens+date`.
    bare = story_keys("July 2026 Apple Security Update Review published", (), (), NOW)
    assert same_story(apple, bare, weights)[0] == "tokens+date"
