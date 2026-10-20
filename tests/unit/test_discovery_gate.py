"""SERAPH's gate (worker/discovery/gate.py): the config, home pages, probes and TACHIKOMA's
proposals."""

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from worker.discovery.gate import (
    DISCOVERED_CATEGORY,
    DISCOVERED_CLASS,
    MAX_EXAMPLES,
    NAME_MAX_CHARS,
    Collected,
    DiscoveryConfig,
    GateConfig,
    ProposalRejected,
    PruneConfig,
    assess_probe,
    check_proposal,
    clean_name,
    discovered_source,
    host_of,
    read_home,
    region_for,
    same_site,
    skipped,
    source_id_for,
    weak_reason,
    weakest,
)
from worker.models import Lane, NormalisedItem, Standing
from worker.pipeline.assemble import evidence_class_for
from worker.pipeline.reputation import Reputation

NOW = datetime(2026, 10, 3, 8, 0, tzinfo=UTC)
GATE = GateConfig(
    probes_to_activate=6, failures_to_reject=3, recent_days=14, min_recent_items=3,
    min_on_beat=2, max_already_collected=0.6, probes_per_pass=10, homes_per_pass=5, max_open=20,
    max_active=25, ready_reprobe_hours=24,
    prune=PruneConfig(per_pass=2, min_days=21, corroboration_below=0.05),
    forget_after_days=90, beat=["ransomware", "vulnerab", "breach", "AI safety"],
)
PRUNE = GATE.prune
REASON = "Writes up Australian ransomware incidents that no registered source covers."


def item(n: int, title: str = "Ransomware hits a council", *, age_days: float = 1.0,
         estimated: bool = False, cves=(), published=True) -> NormalisedItem:
    return NormalisedItem(
        source_id="found_example_org",
        url=f"https://example.org/{n}",
        canonical_url=f"https://example.org/{n}",
        title=title,
        normalised_title=title.lower(),
        published=NOW - timedelta(days=age_days) if published else None,
        fetched_at=NOW,
        cves=list(cves),
        url_hash=f"hash-{n}",
        title_hash=f"title-{n}",
        published_is_estimated=estimated,
    )


# --- Config --------------------------------------------------------------------------------------


def test_the_repositorys_config_loads():
    config = DiscoveryConfig.load()
    assert config.search.queries and config.search.credits_per_day <= 100
    assert "x.com" in config.search.skip_hosts
    assert config.gate.probes_to_activate >= 2


def test_an_unknown_setting_is_refused(tmp_path):
    path = tmp_path / "discovery.yaml"
    path.write_text(DiscoveryConfig.load().model_dump_json().replace('"gate":{', '"gate":{"x":1,'))
    with pytest.raises(ValidationError):
        DiscoveryConfig.load(path)


def test_the_repositorys_gate_makes_room_slowly_and_reprobes_a_waiting_candidate_daily():
    gate = DiscoveryConfig.load().gate
    assert gate.ready_reprobe_hours == 24
    assert (gate.prune.per_pass, gate.prune.min_days, gate.prune.corroboration_below) == (
        2, 21, 0.05
    )


@pytest.mark.parametrize(
    "change",
    [
        {"ready_reprobe_hours": 3},  # under a gate pass: it would be every pass again
        {"ready_reprobe_hours": 169},
        {"prune": {"per_pass": -1, "min_days": 21, "corroboration_below": 0.05}},
        {"prune": {"per_pass": 11, "min_days": 21, "corroboration_below": 0.05}},
        {"prune": {"per_pass": 2, "min_days": 6, "corroboration_below": 0.05}},
        {"prune": {"per_pass": 2, "min_days": 21, "corroboration_below": 1.5}},
        {"prune": {"per_pass": 2, "min_days": 21}},
        {"prune": {"per_pass": 2, "min_days": 21, "corroboration_below": 0.05, "x": 1}},
    ],
)
def test_a_gate_setting_out_of_range_is_refused(change):
    with pytest.raises(ValidationError):
        GateConfig.model_validate({**GATE.model_dump(), **change})


# --- Hosts ---------------------------------------------------------------------------------------


def test_hosts_are_one_form_per_site():
    assert host_of("https://WWW.Example.org/a") == "example.org"
    assert same_site("blog.example.org", "example.org")
    assert same_site("example.org", "blog.example.org")
    assert not same_site("badexample.org", "example.org")
    assert skipped("m.youtube.com", ["youtube.com"]) and not skipped("notyoutube.com",
                                                                    ["youtube.com"])


def test_a_found_source_is_named_for_its_host():
    assert source_id_for("blog.Example-Site.org") == "found_blog_example_site_org"
    assert len(source_id_for("a" * 200 + ".org")) <= 66
    assert region_for("itnews.com.au") == "au" and region_for("example.org") == "global"


def test_an_activated_source_is_the_least_trusted_kind_and_collected_slowly():
    source = discovered_source(host="itnews.com.au", feed_url="https://itnews.com.au/rss",
                               name=None, found_by="tachikoma", now=NOW)
    assert source.id == "found_itnews_com_au" and source.name == "itnews.com.au"
    assert (source.source_class, source.category) == (DISCOVERED_CLASS, DISCOVERED_CATEGORY)
    assert source.lane is Lane.NORMAL and source.region == "au"
    assert evidence_class_for(source).value == "COMMUNITY"
    assert "TACHIKOMA" in source.notes and "2026-10-03" in source.notes


# --- Home pages ----------------------------------------------------------------------------------


def test_a_home_page_gives_its_advertised_feeds_first_then_the_usual_places():
    html = b"""<html><head><title> Example &amp; Co
      </title>
      <link rel="alternate" type="application/rss+xml" href="/feed.rss">
      <link rel="Alternate" type="application/atom+xml" href="https://blog.example.org/atom">
      <link rel="alternate" type="application/rss+xml" href="https://elsewhere.org/feed">
      <link rel="alternate" type="application/rss+xml" href="http://example.org/insecure">
      <link rel="stylesheet" href="/style.css">
    </head></html>"""
    page = read_home("https://example.org/", html)
    assert page.title == "Example & Co"
    assert page.feeds[:2] == ["https://example.org/feed.rss", "https://blog.example.org/atom"]
    assert "https://example.org/feed/" in page.feeds and "https://example.org/rss" in page.feeds
    assert not any("elsewhere" in f or "insecure" in f for f in page.feeds)
    assert len(page.feeds) == len(set(page.feeds))


def test_a_broken_page_still_gives_the_usual_places():
    page = read_home("https://example.org/", b"\xff\xfe<html><link rel=alternate type=")
    assert page.feeds[0] == "https://example.org/feed/"


def test_a_name_is_one_short_line_with_no_link():
    assert clean_name("  A   name\nwith lines ") == "A name with lines"
    assert clean_name("See https://example.org") is None
    assert clean_name(42) is None and clean_name("") is None
    long = clean_name("x" * 200)
    assert len(long) == NAME_MAX_CHARS and long.endswith("…")


# --- Probes --------------------------------------------------------------------------------------


def test_a_lively_feed_on_the_beat_is_healthy():
    items = [item(1), item(2, "New vulnerability in a router"), item(3, "Weather today"),
             item(4, "Old breach", age_days=40)]
    probe = assess_probe(items, [], now=NOW, gate=GATE)
    assert probe.healthy, probe.failures
    assert (probe.items, probe.recent, probe.on_beat, probe.already_collected) == (4, 3, 2, 0)
    assert probe.newest_age_days == 1.0
    assert probe.samples == ["Ransomware hits a council", "New vulnerability in a router"]
    assert probe.as_json()["healthy"] is True


def test_an_item_naming_a_cve_is_on_the_beat():
    items = [item(i, "Patch notes", cves=["CVE-2026-1234"]) for i in range(3)]
    assert assess_probe(items, [], now=NOW, gate=GATE).on_beat == 3


def test_items_dated_only_by_their_fetch_are_not_recent():
    items = [item(i, estimated=True) for i in range(5)] + [item(9, published=False)]
    probe = assess_probe(items, [], now=NOW, gate=GATE)
    assert probe.recent == 0 and not probe.healthy
    assert probe.newest_age_days is None


def test_a_quiet_or_off_beat_feed_is_not_healthy():
    quiet = assess_probe([item(1), item(2)], [], now=NOW, gate=GATE)
    assert any("last 14 days" in f for f in quiet.failures)
    off_beat = assess_probe([item(i, "Gardening tips") for i in range(5)], [], now=NOW, gate=GATE)
    assert any("on the beat" in f for f in off_beat.failures)
    empty = assess_probe([], [], now=NOW, gate=GATE)
    assert "the feed has no items" in empty.failures


def test_a_feed_that_copies_what_the_worker_already_has_is_not_healthy():
    items = [item(i) for i in range(5)]
    probe = assess_probe(items, ["hash-0", "hash-1", "hash-2", "hash-3"], now=NOW, gate=GATE)
    assert probe.already_collected == 4
    assert any("already collected" in f for f in probe.failures)


def test_the_beat_matches_word_starts_only():
    items = [item(i, "Unbreachable cake recipes") for i in range(3)]
    assert assess_probe(items, [], now=NOW, gate=GATE).on_beat == 0


# --- Making room ---------------------------------------------------------------------------------


def collected(source_id, *, score=40, corroboration=0.0, events=100, days=30,
              source_class=DISCOVERED_CLASS) -> Collected:
    return Collected(
        source_id=source_id,
        source_class=source_class,
        first_checked_at=NOW - timedelta(days=days) if days is not None else None,
        reputation=Reputation(score, Standing.COMMUNITY, 1.0, corroboration, events, "basis"),
    )


def test_the_weakest_go_first_and_no_more_than_the_pass_allows():
    sources = [
        collected("found_c", score=34, corroboration=0.01),
        collected("found_a", score=31, corroboration=0.0),
        collected("found_b", score=33, corroboration=0.02),
    ]
    assert [s.source_id for s in weakest(sources, need=5, prune=PRUNE, now=NOW)] == [
        "found_a", "found_b",
    ]


def test_no_more_are_retired_than_there_are_candidates_waiting():
    sources = [collected("found_a"), collected("found_b")]
    assert [s.source_id for s in weakest(sources, need=1, prune=PRUNE, now=NOW)] == ["found_a"]
    assert weakest(sources, need=0, prune=PRUNE, now=NOW) == []


def test_a_source_corroborated_often_enough_is_kept():
    sources = [collected("found_a", corroboration=0.05), collected("found_b", corroboration=0.3)]
    assert weakest(sources, need=2, prune=PRUNE, now=NOW) == []


def test_a_source_with_too_few_events_to_judge_is_kept():
    quiet = collected("found_a", corroboration=None, events=3, score=28)
    assert weakest([quiet], need=1, prune=PRUNE, now=NOW) == []


def test_a_source_collected_for_less_than_the_minimum_is_kept():
    sources = [collected("found_new", days=20), collected("found_unchecked", days=None)]
    assert weakest(sources, need=2, prune=PRUNE, now=NOW) == []
    old = collected("found_old", days=PRUNE.min_days)
    assert weakest([old], need=1, prune=PRUNE, now=NOW) == [old]


def test_a_curated_source_is_never_retired_to_make_room():
    curated = collected("acsc_alerts", score=20, source_class="authoritative")
    assert weakest([curated], need=1, prune=PRUNE, now=NOW) == []


def test_making_room_can_be_turned_off():
    off = PRUNE.model_copy(update={"per_pass": 0})
    assert weakest([collected("found_a")], need=1, prune=off, now=NOW) == []


def test_the_reason_says_what_made_the_source_weak():
    source = collected("found_a", score=31, corroboration=0.004, events=1078, days=30)
    assert weak_reason(source, now=NOW) == (
        "retired for a ready candidate: corroboration 0% of 1078 events in 90 days, "
        "reputation 31, after 30 days collected"
    )


# --- Proposals -----------------------------------------------------------------------------------


def test_a_feed_proposal_is_checked_and_kept():
    proposal = check_proposal(
        {"url": "https://WWW.Example.org/feed?x=1#a", "name": "Example News", "reason": REASON,
         "examples": ["https://example.org/2026/10/story"]}
    )
    assert proposal.host == "example.org" and proposal.url == "https://www.example.org/feed?x=1"
    assert proposal.is_home is False and proposal.name == "Example News"
    assert proposal.examples == ["https://example.org/2026/10/story"]


def test_a_home_page_proposal_leaves_the_feed_to_find():
    assert check_proposal({"url": "https://example.org", "reason": REASON}).is_home is True


def secret_like() -> str:
    # Built at run time so this file never holds a credential-shaped literal.
    return "gh" + "p_" + "a1B2" * 9


@pytest.mark.parametrize(
    "body, why",
    [
        ([], "JSON object"),
        ({"url": "https://example.org/feed"}, "needs reason"),
        ({"reason": REASON}, "needs url"),
        ({"url": "https://example.org/feed", "reason": REASON, "priority": 1}, "unknown field"),
        ({"url": "http://example.org/feed", "reason": REASON}, "url:"),
        ({"url": "https://10.0.0.1/feed", "reason": REASON}, "url:"),
        ({"url": "https://www.youtube.com/@channel", "reason": REASON}, "platform"),
        ({"url": "https://example.org/feed", "reason": "Too short."}, "20 to 280"),
        ({"url": "https://example.org/feed", "reason": 7}, "string"),
        ({"url": "https://example.org/feed", "reason": "x" * 281}, "20 to 280"),
        ({"url": "https://example.org/feed",
          "reason": "Covers ransomware, see https://example.org/about for more."}, "URL"),
        ({"url": "https://example.org/feed",
          "reason": "Covers ransomware; details at example.org/about today."}, "URL"),
        ({"url": "https://example.org/feed", "reason": REASON, "name": "https://evil.org"},
         "name"),
        ({"url": "https://example.org/feed", "reason": REASON,
          "examples": ["https://elsewhere.org/a"]}, "not on example.org"),
        ({"url": "https://example.org/feed", "reason": REASON,
          "examples": ["https://example.org/a"] * (MAX_EXAMPLES + 1)}, "at most"),
        ({"url": "https://example.org/feed", "reason": REASON, "examples": "one"}, "list"),
        ({"url": "https://example.org/feed", "reason": REASON,
          "examples": ["http://example.org/a"]}, "examples[0]"),
    ],
)
def test_a_proposal_is_refused_with_a_reason_to_fix(body, why):
    with pytest.raises(ProposalRejected, match=why.replace("[", r"\[").replace("]", r"\]")):
        check_proposal(body, skip_hosts=["youtube.com"])


def test_a_proposal_carrying_a_credential_is_refused():
    with pytest.raises(ProposalRejected, match="credential"):
        check_proposal({"url": "https://example.org/feed",
                        "reason": f"Ransomware coverage, token {secret_like()} for access."})
