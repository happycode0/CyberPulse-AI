from datetime import UTC, datetime, timedelta

from worker.models import EvidenceClass, MaterialChange, RawItem, SourceConfig
from worker.pipeline.assemble import (
    build_new_event,
    clean_summary,
    evidence_class_for,
    plan_update,
)
from worker.pipeline.normalise import normalise

NOW = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)


def source(sid="s1", *, category="advisory", region="au", name="Source One"):
    return SourceConfig(
        id=sid, name=name, type="rss", region=region, category=category, source_class="feed",
        priority=1, lane="fast", enabled=True, url="https://example.org/feed", parser="rss",
        expected_frequency="daily",
    )


def item(title="Acme VPN flaw", url="https://example.org/a", *, published=NOW - timedelta(days=2),
         summary=None, fetched=NOW):
    raw = RawItem(source_id="s1", url=url, title=title, raw_summary=summary,
                  published=published, fetched_at=fetched, payload_hash="h")
    return normalise(raw, now=NOW)


def test_first_seen_and_last_material_update_come_from_the_publication_date_not_fetch_time():
    e = build_new_event(item(published=NOW - timedelta(days=5)), source(), "evt-2026-000001", now=NOW)
    assert e.first_seen == NOW - timedelta(days=5)
    assert e.last_material_update == e.first_seen
    assert e.last_seen == NOW


def test_undated_item_falls_back_to_fetch_time_and_publishes_no_source_date():
    e = build_new_event(item(published=None), source(), "evt-2026-000001", now=NOW)
    assert e.first_seen == NOW and e.sources[0].published is None


def test_new_event_is_flagged_for_enrichment_and_starts_with_a_new_fact_entry():
    e = build_new_event(item(), source(), "evt-2026-000001", now=NOW)
    assert e.pending_enrichment is True
    assert [t.type for t in e.timeline] == [MaterialChange.NEW_FACT]
    assert e.sources[0].independent is True and e.last_independent_confirmation is None


def test_au_flag_follows_source_region_but_relevance_is_left_unscored():
    au = build_new_event(item(), source(region="au"), "evt-2026-000001", now=NOW)
    us = build_new_event(item(), source(region="us"), "evt-2026-000002", now=NOW)
    assert au.au.directly_reported_in_au and au.au.reasons and au.au.relevance is None
    assert not us.au.directly_reported_in_au and us.au.reasons == []


def test_cves_from_the_item_are_attached_without_scores():
    e = build_new_event(item(title="CVE-2026-0001 exploited"), source(), "evt-2026-000001", now=NOW)
    assert [c.id for c in e.cves] == ["CVE-2026-0001"] and e.cves[0].cvss is None


def test_evidence_class_by_category():
    assert evidence_class_for(source(category="advisory")) is EvidenceClass.AUTHORITATIVE
    assert evidence_class_for(source(category="vendor_advisory")) is EvidenceClass.VENDOR
    assert evidence_class_for(source(category="news")) is EvidenceClass.NEWS
    assert evidence_class_for(source(category="mystery")) is EvidenceClass.COMMUNITY


def test_summary_strips_markup_decodes_entities_and_caps_length():
    assert clean_summary("<p>Patch &amp; mitigate <b>now</b></p>\n", "t") == "Patch & mitigate now"
    assert clean_summary(None, "fallback") == "fallback"
    assert clean_summary("<br/>", "fallback") == "fallback"
    long = clean_summary("word " * 500, "t")
    assert len(long) <= 600 and long.endswith("\u2026")


def base_event():
    return build_new_event(item(published=NOW - timedelta(days=2)), source(), "evt-2026-000001",
                           now=NOW - timedelta(days=1))


def test_older_source_pulls_first_seen_back():
    e = base_event()
    u = plan_update(e, item(url="https://o.example/x", published=NOW - timedelta(days=4)),
                    source("s2"), now=NOW)
    assert u.first_seen == NOW - timedelta(days=4)
    assert u.last_material_update >= u.first_seen


def test_repeat_by_another_outlet_adds_evidence_but_does_not_refresh_prominence_clock():
    e = base_event()
    u = plan_update(e, item(url="https://o.example/x", published=NOW - timedelta(hours=1)),
                    source("s2", name="Other"), now=NOW)
    assert u.last_material_update == e.last_material_update
    assert u.last_independent_confirmation == NOW - timedelta(hours=1)
    assert u.source.independent is True and u.new_cves == []
    assert [t.type for t in u.timeline] == [MaterialChange.NEW_EVIDENCE]


def test_new_cve_is_material_and_moves_last_material_update():
    e = base_event()
    u = plan_update(e, item(title="Acme VPN flaw CVE-2026-0042", url="https://o.example/x",
                            published=NOW - timedelta(hours=3)), source("s2"), now=NOW)
    assert u.new_cves == ["CVE-2026-0042"]
    assert u.last_material_update == NOW - timedelta(hours=3) > e.last_material_update
    assert MaterialChange.NEW_CVE in [t.type for t in u.timeline]


def test_same_source_republishing_is_not_an_independent_confirmation():
    e = base_event()
    u = plan_update(e, item(url="https://example.org/a-v2"), source("s1"), now=NOW)
    assert u.source.independent is False
    assert u.last_independent_confirmation is None and u.timeline == []


def test_update_never_leaves_last_material_update_unset():
    e = base_event().model_copy(update={"last_material_update": None})
    u = plan_update(e, item(url="https://o.example/x"), source("s2"), now=NOW)
    assert u.last_material_update == e.first_seen
