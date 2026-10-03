from datetime import UTC, date, datetime, timedelta

import pytest
from pydantic import ValidationError

from worker.pipeline.trends import (
    ACTIVITY_DAYS,
    EVENTS_PER_TREND,
    MAX_CVES,
    MAX_TOPICS,
    Matcher,
    Report,
    Story,
    TrendsConfig,
    Windows,
    compute_trends,
    load_trends_config,
    words,
)
from worker.publish.validate import validate_payload

NOW = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)
LONG_AGO = NOW - timedelta(days=30)
CONFIG = load_trends_config()


def ago(hours: float) -> datetime:
    return NOW - timedelta(hours=hours)


def report(headline: str, hours: float, n: int = 1) -> Report:
    return Report(f"evt-2026-{n:06d}", headline, ago(hours))


def story(n: int = 1, *, hours: float = 1, prominence: float | None = 0.5, **kw) -> Story:
    fields = {"critical_or_high": False, "au": False} | kw
    return Story(f"evt-2026-{n:06d}", ago(hours), prominence, **fields)


def trends(reports, stories=(), kev_added=None, *, since=LONG_AGO, config=CONFIG):
    return compute_trends(
        reports, stories, kev_added or {}, config, now=NOW, collecting_since=since
    )


def topic(result: dict, key: str) -> dict:
    return next(t for t in result["topics"] if t["key"] == key)


def config(*topics: dict) -> TrendsConfig:
    return TrendsConfig.model_validate({"version": "1", "topics": list(topics)})


# --- matching ---------------------------------------------------------------------------------


def test_words_drop_case_possessives_and_punctuation():
    assert words("Fortinet's FortiGate: D-Link zero-day!") == (
        "fortinet", "fortigate", "dlink", "zeroday",
    )
    assert words("Microsoft’s patch") == ("microsoft", "patch")


def test_a_term_of_several_words_matches_only_those_words_in_a_row():
    m = Matcher(CONFIG.topics)
    assert m.topics("Salt Typhoon in more telcos") == {"salt-typhoon"}
    assert m.topics("Salt prices climb as the typhoon nears") == set()


def test_a_term_matches_whole_words_only():
    m = Matcher(CONFIG.topics)
    assert m.topics("Applebee's loyalty scheme and the oracles of finance") == set()
    assert m.topics("Apple patches WebKit") == {"apple"}


def test_a_headline_can_name_several_topics():
    m = Matcher(CONFIG.topics)
    assert m.topics("LockBit ransomware hits Citrix NetScaler users") == {
        "lockbit", "ransomware", "citrix",
    }


def test_every_committed_term_matches_its_own_topic():
    m = Matcher(CONFIG.topics)
    assert len(CONFIG.topics) > 100
    for t in CONFIG.topics:
        for term in t.terms:
            assert t.key in m.topics(f"News: {term} today"), (t.key, term)


@pytest.mark.parametrize(
    "headline,key",
    [
        ("Anthropic's Model Context Protocol gets a security review", "mcp"),
        ("Rogue AI agents and who is liable", "ai-agents"),
        ("Google ships Gemini 4", "gemini"),
        ("Meta's Llama licence changes", "llama"),
        ("Mistral raises again", "mistral"),
        ("DeepSeek's new model tops the charts", "deepseek"),
        ("Researchers show data poisoning of a code model", "model-poisoning"),
        ("Commission starts enforcing AI Act rules", "eu-ai-act"),
        ("Canberra weighs mandatory AI guardrails", "ai-regulation"),
    ],
)
def test_the_ai_topics_are_counted(headline, key):
    by_key = {t.key: t for t in CONFIG.topics}
    assert by_key[key].kind == "ai"
    assert key in Matcher(CONFIG.topics).topics(headline)


@pytest.mark.parametrize(
    "topics,message",
    [
        (
            [{"key": "a", "label": "A", "kind": "vendor", "terms": ["a"]},
             {"key": "a", "label": "B", "kind": "vendor", "terms": ["b"]}],
            "unique",
        ),
        (
            [{"key": "a", "label": "A", "kind": "vendor", "terms": ["x"]},
             {"key": "b", "label": "B", "kind": "actor", "terms": ["x"]}],
            "belongs to both",
        ),
        ([{"key": "a", "label": "A", "kind": "threat", "terms": ["Zero-Day"]}], "headline reads"),
        ([{"key": "a", "label": "A", "kind": "product", "terms": ["a"]}], "kind"),
        ([{"key": "A b", "label": "A", "kind": "vendor", "terms": ["a"]}], "key"),
        ([{"key": "a", "label": "A", "kind": "vendor", "terms": []}], "terms"),
        ([{"key": "a", "label": "A", "kind": "vendor", "terms": ["a"], "weight": 2}], "weight"),
    ],
)
def test_config_rejects_a_bad_topic(topics, message):
    with pytest.raises(ValidationError, match=message):
        config(*topics)


# --- windows ----------------------------------------------------------------------------------


def test_windows_are_the_last_day_and_the_six_before_it():
    w = Windows(now=NOW, collecting_since=LONG_AGO)
    assert (w.recent_start, w.baseline_start, w.baseline_end) == (ago(24), ago(168), ago(24))
    assert (w.baseline_hours, w.warming_up) == (144, False)
    assert [w.where(ago(h)) for h in (0, 24, 24.01, 168, 168.01, -1)] == [
        "recent", "recent", "baseline", "baseline", None, None,
    ]


def test_windows_are_clipped_to_when_collection_began():
    w = Windows(now=NOW, collecting_since=ago(80))
    assert (w.baseline_start, w.baseline_hours, w.warming_up) == (ago(80), 56, False)
    assert w.where(ago(81)) is None

    young = Windows(now=NOW, collecting_since=ago(12))
    assert (young.recent_start, young.baseline_hours, young.warming_up) == (ago(12), 0, True)
    assert young.where(ago(13)) is None


# --- states -----------------------------------------------------------------------------------


def _reports(headline: str, recent: int, baseline: int) -> list[Report]:
    """`recent` reports spread over the last day, `baseline` over the six before it."""
    rs = [report(headline, 1 + 22 * i / max(recent, 1), n=i + 1) for i in range(recent)]
    rs += [
        report(headline, 25 + 142 * i / max(baseline, 1), n=100 + i) for i in range(baseline)
    ]
    return rs


@pytest.mark.parametrize(
    "recent,baseline,state",
    [
        (3, 0, "new"),
        (2, 0, "steady"),
        (4, 6, "rising"),
        (3, 6, "rising"),
        (2, 6, "steady"),
        (0, 6, "falling"),
        (0, 12, "falling"),
        (1, 12, "steady"),
        (0, 2, "steady"),
    ],
)
def test_velocity_states(recent, baseline, state):
    t = topic(trends(_reports("Akira hits another firm", recent, baseline)), "akira")
    assert (t["recent"], t["baseline"], t["state"]) == (recent, baseline, state)


def test_ratio_compares_the_last_day_with_the_daily_baseline_softened_by_one():
    t = topic(trends(_reports("Akira hits another firm", 4, 6)), "akira")
    assert (t["baseline_per_day"], t["ratio"]) == (1.0, 2.5)


def test_with_under_two_days_of_baseline_nothing_rises_or_falls():
    result = trends(_reports("Akira hits another firm", 5, 0), since=ago(36))
    assert topic(result, "akira")["state"] == "warming_up"
    assert result["coverage"] == {
        "collecting_since": ago(36).isoformat(),
        "recent_hours": 24,
        "baseline_hours": 12,
        "baseline_hours_wanted": 144,
        "baseline_hours_needed": 48,
        "warming_up": True,
    }


def test_a_report_from_before_collection_began_is_not_counted():
    result = trends([report("Citrix bug", 90), report("Citrix bug", 2)], since=ago(80))
    t = topic(result, "citrix")
    assert (t["recent"], t["baseline"]) == (1, 0)
    assert sum(d["reports"] for d in result["activity"]) == 1


def test_no_runs_yet_means_nothing_is_counted():
    result = trends([report("Citrix bug", 2)], [story(hours=2)], {NOW.date(): 1}, since=None)
    assert (result["topics"], result["cves"]) == ([], [])
    assert result["coverage"]["collecting_since"] is None
    assert result["coverage"]["warming_up"] is True
    assert {d["coverage"] for d in result["activity"]} == {"none"}
    assert result["activity"][-1]["kev_added"] == 1


# --- rows -------------------------------------------------------------------------------------


def test_a_topic_lists_its_most_prominent_events():
    reports = [report("Akira strikes", 2, n=n) for n in range(1, 6)]
    stories = [story(n, prominence=n / 10) for n in range(1, 5)]  # event 5 is unscored
    t = topic(trends(reports, stories), "akira")
    assert t["stories"] == 5
    assert t["event_ids"] == ["evt-2026-000004", "evt-2026-000003", "evt-2026-000002"]
    assert len(t["event_ids"]) == EVENTS_PER_TREND


def test_several_reports_on_one_event_are_one_story():
    t = topic(trends([report("Akira strikes", h) for h in (1, 2, 3)]), "akira")
    assert (t["recent"], t["stories"], t["state"]) == (3, 1, "new")


def test_topics_are_ordered_by_last_day_then_ratio_and_capped():
    many = config(
        *({"key": f"t{i:02d}", "label": f"T{i}", "kind": "threat", "terms": [f"word{i:02d}"]}
          for i in range(MAX_TOPICS + 10))
    )
    reports = [
        report(f"word{i:02d} news", h, n=i)
        for i in range(MAX_TOPICS + 10)
        for h in range(1, i % 4 + 2)
    ]
    rows = trends(reports, config=many)["topics"]
    assert len(rows) == MAX_TOPICS
    assert [r["recent"] for r in rows] == sorted((r["recent"] for r in rows), reverse=True)
    assert rows[0] == {
        "key": "t03", "label": "T3", "kind": "threat", "recent": 4, "baseline": 0,
        "baseline_per_day": 0.0, "ratio": 5.0, "state": "new", "stories": 1,
        "event_ids": ["evt-2026-000003"],
    }


def test_a_cve_counts_every_report_on_an_event_that_names_it():
    stories = [
        story(1, cves=("CVE-2026-1111",), kev_cves=frozenset({"CVE-2026-1111"})),
        story(2, cves=("CVE-2026-2222", "CVE-2026-1111")),
    ]
    reports = [
        report("Vendor fixes flaw", 1, n=1),
        report("Patch now", 2, n=1),
        report("Old write-up", 50, n=1),
        report("Another take", 3, n=2),
        report("Unrelated", 4, n=3),
    ]
    rows = trends(reports, stories)["cves"]
    assert [(r["cve_id"], r["recent"], r["week"], r["stories"], r["kev"]) for r in rows] == [
        ("CVE-2026-1111", 3, 4, 2, True),
        ("CVE-2026-2222", 1, 1, 1, False),
    ]


def test_cves_are_capped():
    stories = [story(n, cves=(f"CVE-2026-{1000 + n}",)) for n in range(1, MAX_CVES + 6)]
    reports = [report("x", 1, n=n) for n in range(1, MAX_CVES + 6)]
    assert len(trends(reports, stories)["cves"]) == MAX_CVES


# --- activity ---------------------------------------------------------------------------------


def test_activity_marks_how_much_of_each_day_was_collected():
    days = trends([], since=ago(80))["activity"]  # collection began 2026-10-07 04:00
    assert len(days) == ACTIVITY_DAYS
    assert (days[0]["date"], days[-1]["date"]) == ("2026-09-27", "2026-10-10")
    assert [d["coverage"] for d in days[-5:]] == ["none", "partial", "full", "full", "partial"]


def test_activity_counts_stories_reports_and_kev_additions_by_day():
    stories = [
        story(1, hours=14, critical_or_high=True, au=True),  # 2026-10-09 22:00
        story(2, hours=16, ai=True),  # 2026-10-09 20:00
        story(3, hours=200, critical_or_high=True),  # a backlog item, before collection
    ]
    reports = [report("a", 14, n=1), report("b", 13, n=1), report("c", 1, n=2)]
    kev = {date(2026, 10, 9): 2, date(2026, 10, 1): 1}
    days = {d["date"]: d for d in trends(reports, stories, kev, since=ago(80))["activity"]}
    assert days["2026-10-09"] == {
        "date": "2026-10-09", "coverage": "full", "stories": 2, "reports": 2,
        "kev_added": 2, "critical_high": 1, "au_stories": 1, "ai_stories": 1,
    }
    assert days["2026-10-10"]["reports"] == 1
    # CISA's dates cover its whole catalogue, so a KEV addition counts on an uncovered day.
    assert days["2026-10-01"] | {"date": "-"} == {
        "date": "-", "coverage": "none", "stories": 0, "reports": 0,
        "kev_added": 1, "critical_high": 0, "au_stories": 0, "ai_stories": 0,
    }


# --- the published shape ----------------------------------------------------------------------


def test_the_payload_matches_the_published_schema():
    stories = [
        story(1, cves=("CVE-2026-1111",), kev_cves=frozenset({"CVE-2026-1111"})),
        story(2, ai=True),
    ]
    reports = _reports("Akira ransomware hits a Citrix shop", 4, 6)
    reports += [report("Gemini 4 jailbroken in a day", 2, n=2)]
    for since in (LONG_AGO, ago(36), None):
        payload = trends(reports, stories, {NOW.date(): 3}, since=since)
        if since is LONG_AGO:
            assert topic(payload, "gemini")["kind"] == "ai"
            assert payload["activity"][-1]["ai_stories"] == 1
        validate_payload({"generated_at": NOW.isoformat(), "pipeline_version": "1"} | payload,
                         "trends")
