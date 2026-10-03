"""A merged story's beat (worker/db/merge.py `merged_tags`, docs/wiki/ai-news-beat.md)."""

from worker.db.merge import BeatTags, merged_tags
from worker.models import Beat, beat_of


def test_a_merge_unites_the_domains_of_the_stories_it_joins():
    merged = merged_tags(
        [
            BeatTags(("cybersecurity",)),
            BeatTags(("ai",), "AI_INDUSTRY", "notable"),
            BeatTags(("ai",), "AI_SECURITY", "major"),
        ]
    )
    assert merged.domains == ("cybersecurity", "ai") and beat_of(merged.domains) is Beat.BOTH
    # The first subdomain stands; the strongest significance wins.
    assert (merged.ai_subdomain, merged.ai_significance) == ("AI_INDUSTRY", "major")


def test_a_seed_gives_way_to_what_triage_found():
    merged = merged_tags(
        [
            BeatTags(("cybersecurity",), triaged=False),
            BeatTags(("ai",), "AI_INDUSTRY", "minor"),
        ]
    )
    assert merged.domains == ("ai",) and merged.ai_significance == "minor" and merged.triaged


def test_seeds_alone_are_united_too():
    merged = merged_tags(
        [BeatTags(("ai",), triaged=False), BeatTags(("cybersecurity",), triaged=False)]
    )
    assert merged.domains == ("ai", "cybersecurity") and not merged.triaged


def test_ai_tags_do_not_survive_off_the_ai_beat():
    merged = merged_tags([BeatTags(("cybersecurity",)), BeatTags((), None, None)])
    assert merged == BeatTags(("cybersecurity",))


def test_a_story_triage_found_on_neither_desk_stays_there():
    merged = merged_tags([BeatTags(()), BeatTags(("cybersecurity",), triaged=False)])
    assert merged.domains == () and beat_of(merged.domains) is Beat.OTHER
