"""The publisher's holds (worker/publish/hold.py), on titles from the site's week of 2026-10-21."""

from datetime import UTC, datetime

from worker.discovery.gate import DiscoveryConfig
from worker.models import Event
from worker.publish.hold import Hold, holds

NOW = datetime(2026, 10, 27, 12, 0, tzinfo=UTC)
ON_BEAT = DiscoveryConfig.load().gate.beat_re()


def event(n: int, title: str, *sources: str, summary: str = "", **overrides) -> Event:
    fields = {
        "event_id": f"evt-2026-{n:06d}",
        "title": title,
        "summary": summary or title,
        "first_seen": NOW,
        "last_seen": NOW,
        # Seeded from a discovered source's beat (worker/pipeline/assemble.py).
        "domains": ["cybersecurity"],
        "sources": [
            {"source_id": s, "url": f"https://{s}.example/{n}", "evidence_class": "COMMUNITY"}
            for s in sources
        ],
    }
    return Event(**(fields | overrides))


def held(*events: Event) -> dict[str, Hold]:
    return holds(events, on_beat=ON_BEAT, min_share=0.5)


GUARDIAN = "found_theguardian_com"
SCWORLD = "found_scworld_com"


def guardian_week() -> list[Event]:
    """Mostly off the beat: one security story among four."""
    return [
        event(1, "Australia collapse in chaotic fashion to leave South Africa in sight of victory",
              GUARDIAN),
        event(2, "How to turn brown bananas into the best vegan bread of the bunch – recipe",
              GUARDIAN),
        event(3, "Clayface review – flesh-stretching DC body horror is very gory", GUARDIAN),
        event(4, "Medibank hackers leak more customer records", GUARDIAN),
    ]


def test_a_mostly_off_beat_discovered_source_loses_only_its_off_beat_items():
    assert held(*guardian_week()) == {
        "evt-2026-000001": Hold.OFF_BEAT,
        "evt-2026-000002": Hold.OFF_BEAT,
        "evt-2026-000003": Hold.OFF_BEAT,
    }


def test_a_mostly_on_beat_discovered_source_keeps_every_item():
    # SC World's identity explainer names no beat word; its feed is otherwise all security.
    week = [
        event(1, "Least privilege in multi-cloud environments", SCWORLD,
              summary="Least privilege in multi-cloud environments"),
        event(2, "Ransomware gang claims a council", SCWORLD),
        event(3, "Phishing kit targets Okta users", SCWORLD),
        event(4, "Moving images: How a runtime bill of materials gives dependency visibility",
              SCWORLD),
    ]
    assert held(*week) == {}


def test_a_cve_puts_an_item_on_the_beat():
    week = guardian_week()
    week[0] = week[0].model_copy(update={"cves": [{"id": "CVE-2026-1234"}]})
    assert "evt-2026-000001" not in held(*week)


def test_a_curated_source_is_never_held_for_its_beat():
    week = guardian_week()
    week[1] = event(2, week[1].title, GUARDIAN, "abc_news")
    week.append(event(5, "Socceroos qualify for the World Cup", "abc_news"))
    assert set(held(*week)) == {"evt-2026-000001", "evt-2026-000003"}


def test_triage_putting_a_discovered_item_on_neither_desk_holds_it():
    # "Photos of the day", which a security site's feed also carried; triage found no desk.
    week = [
        event(1, "Ransomware gang claims a council", SCWORLD),
        event(2, "Protests, storms and a pumpkin regatta: photos of the day – Monday", SCWORLD,
              summary="Our security desk's pick of the day's pictures", domains=[]),
    ]
    assert held(*week) == {"evt-2026-000002": Hold.OFF_BEAT}
    # The same verdict on a story a curated source carries changes nothing.
    week[1] = event(2, week[1].title, SCWORLD, "abc_news", domains=[])
    assert held(*week) == {}


def test_a_directory_listing_is_held_whoever_carries_it():
    listing = event(1, "plex-mcp-server by vladimir-tutin — ★ 150", "found_agentskillshub_top",
                    summary="An MCP server for security teams")
    curated = event(2, "envsitter-guard by boxpositron — ★ 83", "abc_news")
    assert held(listing, curated) == {
        "evt-2026-000001": Hold.LISTING,
        "evt-2026-000002": Hold.LISTING,
    }
    # A star in a story's title is not a listing.
    assert held(event(3, "GitHub stars are for sale — ★ inflation hits open source",
                      "abc_news")) == {}


def test_an_event_with_no_sources_is_held():
    assert held(event(1, "Ransomware gang claims a council")) == {
        "evt-2026-000001": Hold.NO_SOURCES
    }
