from datetime import UTC, datetime, timedelta

from worker.pipeline.correlate import (
    StoryRecord,
    after_merges,
    candidate_pairs,
    plan_merges,
    possible_duplicates,
)
from worker.pipeline.resolve import TokenWeights, story_keys

NOW = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)
# Common words weigh little, rare names a lot (as in production, where weights come from
# 180 days of headlines).
WEIGHTS = TokenWeights.from_titles(
    ["ransomware gang claims attack on firm"] * 60
    + ["vulnerability patch released"] * 60
    + ["KillSec Medibank", "Acme VPN gateway"]
)


def record(
    event_id,
    title,
    *,
    hours=0,
    cves=(),
    enriched=False,
    prominence=None,
    registers=(),
    source_titles=(),
):
    when = NOW + timedelta(hours=hours)
    return StoryRecord(
        event_id=event_id,
        title=title,
        first_seen=when,
        enriched=enriched,
        prominence=prominence,
        keys=story_keys(title, source_titles, cves, when, registers),
    )


def test_pairs_come_from_shared_cves_and_from_shared_words_nearby():
    records = [
        record("evt-2026-000001", "KillSec claims Medibank attack"),
        record("evt-2026-000002", "Medibank confirms breach", hours=40),
        record("evt-2026-000003", "Medibank update", hours=200),  # too far for words
        record("evt-2026-000004", "Acme patch", cves=("CVE-2026-1001",)),
        record("evt-2026-000005", "Different words", hours=500, cves=("CVE-2026-1001",)),
    ]
    assert candidate_pairs(records) == {(0, 1), (3, 4)}


def test_a_story_split_by_its_headlines_is_joined_into_its_enriched_event():
    records = [
        record("evt-2026-000001", "KillSec claims Medibank ransomware attack", prominence=0.4),
        record(
            "evt-2026-000002",
            "Medibank confirms KillSec breach",
            hours=30,
            enriched=True,
            prominence=0.2,
        ),
        record("evt-2026-000003", "Unrelated vulnerability patch released", hours=1),
    ]
    (group,) = plan_merges(records, WEIGHTS)
    # Enriched beats prominent: its summary was paid for.
    assert group.winner == "evt-2026-000002"
    assert group.losers == ("evt-2026-000001",)
    assert group.methods == ("weighted",)
    assert group.title is None


def test_without_enrichment_the_most_prominent_then_the_oldest_wins():
    a = record("evt-2026-000001", "Acme VPN gateway flaw", cves=("CVE-2026-1001",))
    b = record("evt-2026-000002", "Acme fixes gateway", hours=5, cves=("CVE-2026-1001",))
    assert plan_merges([a, b], WEIGHTS)[0].winner == "evt-2026-000001"
    b = record(
        "evt-2026-000002",
        "Acme fixes gateway",
        hours=5,
        cves=("CVE-2026-1001",),
        prominence=0.3,
    )
    (group,) = plan_merges([a, b], WEIGHTS)
    assert (group.winner, group.methods) == ("evt-2026-000002", ("cve_set",))


def test_a_generic_winner_takes_the_groups_first_real_headline():
    kev = record(
        "evt-2026-000001",
        "CISA Adds One Known Exploited Vulnerability to Catalog",
        cves=("CVE-2026-1001",),
        enriched=True,
    )
    story = record(
        "evt-2026-000002",
        "Acme gateway flaws exploited",
        hours=2,
        cves=("CVE-2026-1001", "CVE-2026-1002"),
    )
    (group,) = plan_merges([kev, story], WEIGHTS)
    assert group.winner == "evt-2026-000001"
    assert group.title == "Acme gateway flaws exploited"


def test_groups_join_transitively_and_one_registers_items_stay_apart():
    records = [
        record("evt-2026-000001", "Acme gateway advisory", cves=("CVE-2026-1001",)),
        record("evt-2026-000002", "Acme gateway exploited", hours=3, cves=("CVE-2026-1001",)),
        record(
            "evt-2026-000003",
            "Acme gateway exploited widely",
            hours=4,
            cves=("CVE-2026-1001", "CVE-2026-1002"),
        ),
        # Two of one register's advisories naming the same CVE are two items by definition.
        record(
            "evt-2026-000010",
            "Siemens SCALANCE switches",
            cves=("CVE-2026-2001",),
            registers=("ics",),
        ),
        record(
            "evt-2026-000011", "Rockwell FactoryTalk", cves=("CVE-2026-2001",), registers=("ics",)
        ),
    ]
    (group,) = plan_merges(records, WEIGHTS)
    assert group.winner == "evt-2026-000001"
    assert set(group.losers) == {"evt-2026-000002", "evt-2026-000003"}


def test_the_duplicate_rate_counts_live_pairs_and_drops_once_merged():
    records = [
        record("evt-2026-000001", "KillSec claims Medibank ransomware attack"),
        record("evt-2026-000002", "Medibank confirms KillSec breach", hours=30),
        record("evt-2026-000003", "Acme patch", cves=("CVE-2026-1001",)),
        # Shares a CVE but tells another story: counted as possible, not merged.
        record(
            "evt-2026-000004",
            "Roundup of the week",
            hours=1,
            cves=("CVE-2026-1001", "CVE-2026-1005", "CVE-2026-1006", "CVE-2026-1007"),
        ),
        record("evt-2026-000005", "Something else entirely", hours=2),
    ]
    live = {r.event_id for r in records}
    before = possible_duplicates(records, live, WEIGHTS)
    assert before.live == 5
    assert before.in_pairs == 4
    assert {(a, b) for a, b, *_ in before.pairs} == {
        ("evt-2026-000001", "evt-2026-000002"),
        ("evt-2026-000003", "evt-2026-000004"),
    }

    groups = plan_merges(records, WEIGHTS)
    assert [g.losers for g in groups] == [("evt-2026-000002",)]
    merged = after_merges(records, groups)
    assert [r.event_id for r in merged] == [
        "evt-2026-000001",
        "evt-2026-000003",
        "evt-2026-000004",
        "evt-2026-000005",
    ]
    assert "killsec" in merged[0].keys.tokens and "confirms" in merged[0].keys.tokens
    after = possible_duplicates(merged, live - {"evt-2026-000002"}, WEIGHTS)
    assert (after.live, after.in_pairs) == (4, 2)
    assert after.rate == 0.5
    assert possible_duplicates([], set(), WEIGHTS).rate == 0.0
