"""Static assertions on the Task 15 site additions: map, replay, detail, history."""

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def test_world_atlas_is_vendored_and_small():
    p = Path("site/assets/vendor/countries-110m.json")
    p = ROOT / p
    assert p.exists() and p.stat().st_size < 200_000


def test_map_uses_iso_numeric_ids_for_filtering():
    assert 'data-n3' in read("site/assets/map.js")


def test_map_projection_is_centred_on_australia():
    assert "rotate([-150" in read("site/assets/map.js")


def test_map_has_a_keyboard_accessible_equivalent():
    assert "<select" in read("site/index.html") and "country" in read("site/index.html")


# The pipeline left the dashboard for the System page, which pages.js draws. Wherever it is
# drawn, it is a replay of the last collection labelled as one, over the stages the spec names;
# hud.js no longer draws it, so a second copy cannot drift from the first.
def test_a_drawn_pipeline_is_a_labelled_replay_of_the_spec_stages():
    hud = read("site/assets/hud.js")
    assert "export function renderPipeline" not in hud, "the dashboard draws the pipeline again"
    for rel in ("site/index.html", "site/assets/hud.js", "site/assets/pages.js"):
        text = read(rel)
        if "pipe-pulse" not in text:
            continue
        assert "COLLECTION REPLAY" in text, rel
        for s in ("SOURCES", "COLLECT", "MATCH", "VERIFY", "ENRICH", "CROSS-REF", "SCORE", "PUBLISH"):
            assert s in text, (rel, s)


def test_ai_suggested_mitre_is_labelled_in_event_detail():
    assert "AI SUGGESTED" in read("site/event.html") or "AI SUGGESTED" in read("site/assets/hud.js")


def test_unknown_cvss_renders_as_unknown_not_zero():
    js = read("site/assets/hud.js")
    assert "unknown" in js.lower() and "cvss ?? 0" not in js.replace(" ", "")


def test_canvas_loop_pauses_when_hidden_and_offscreen():
    js = read("site/assets/hud.js")
    assert "visibilitychange" in js and "IntersectionObserver" in js


# The scrolling headline banner was removed: perpetual motion above the fold competed with
# the content and could not be read at a glance. "Top signals" replaces it as a static list
# on the Dashboard, so there is nothing left to pause.
def test_headline_banner_does_not_move():
    for rel in ("site/index.html", "site/assets/hud.js", "site/assets/hud.css"):
        assert "ticker" not in read(rel).lower(), rel


def test_top_signals_is_a_static_list():
    assert 'id="headline-list"' in read("site/index.html")
    js = read("site/assets/hud.js")
    assert "export function renderHeadlines" in js


# Node.append() stringifies a null child, so `cond ? node : null` printed a literal "null"
# paragraph under the event summary whenever why_it_matters was absent. The local append()
# helper drops null/undefined/false, so optional rows have to go through it.
def test_absent_detail_fields_render_nothing_not_the_word_null():
    js = read("site/assets/hud.js")
    fn = js.split("export function renderEventDetail", 1)[1].split("\nexport function", 1)[0]
    optional = [ln for ln in fn.splitlines() if ln.strip() == ": null,"]
    assert optional, "expected optional detail rows"
    assert "append(root, [" in fn


def _section_ids(js: str) -> list[str]:
    block = js.split("export const SECTIONS", 1)[1].split("];", 1)[0]
    return re.findall(r"\{\s*id: '([\w-]+)',\s*match:", block)


def test_every_view_is_a_sibling_under_main():
    """Guards the reason the old tabs looked broken.

    The panels used to be split: AUSTRALIA NOW above, the rest below the gauges, the world
    map and the filter panel. Selecting a tab swapped a panel ~1900px down the page, so the
    viewport did not visibly change and the tab read as dead. Every view the header tabs link to
    has to be a direct child of the one <main>, and the tabs have to link to every view, in the
    order VIEWS declares, each as a real ?view= address so it opens in a new tab too.
    """
    html = read("site/index.html")
    js = read("site/assets/hud.js")
    declared = re.findall(r"'([\w-]+)'", re.search(r"export const VIEWS = \[([^\]]*)\]", js).group(1))
    assert declared == ["dashboard", "events", "crew", "system", "sources"], declared
    tabs = html.split('<nav class="tabs"', 1)[1].split("</nav>", 1)[0]
    assert re.findall(r'data-view-link="([\w-]+)"', tabs) == declared
    for name in declared:
        assert f'data-view-link="{name}" href="?view={name}&amp;scope=au"' in tabs, name
    assert html.count("data-view-link=") == len(declared), "a view link outside the tabs"
    main = html.split('<main id="main"', 1)[1].split("</main>", 1)[0]
    for name in declared:
        assert re.search(rf'\n    <section class="view" id="view-{name}"', main), name
    assert main.count('class="view"') == len(declared), "a view the tabs cannot reach"


def test_every_section_the_js_renders_has_a_home_in_the_markup():
    """Thirteen tabs became five, and five tabs became three views with one Events list.

    The three domain sections became the scope and the beat, and the other seven became feeds of
    that list. A feed nothing can turn on is as lost as a section that renders into no container,
    so every feed has to be applied by a preset in the FEEDS menu, every feed preset needs its
    link, count and widen link there, and the list renders into exactly one body: a second would
    be a container nothing fills. Each is invisible in a diff, hence this.

    AUSTRALIA NOW and GLOBAL CYBER are presets still, so their old links resolve, but not menu
    items: each is exactly a SCOPE choice, and listing it twice made the menu read as two filters.
    """
    html = read("site/index.html")
    js = read("site/assets/hud.js")
    ids = _section_ids(js)
    assert len(ids) == 7, ids
    presets = js.split("export const PRESETS = [", 1)[1].split("\n];", 1)[0]
    fed = set()
    for feeds in re.findall(r"feedPreset\('[\w-]+', '[^']+', \[([^\]]*)\]\)", presets):
        fed.update(re.findall(r"'([\w-]+)'", feeds))
    assert set(ids) <= fed, f"no preset applies: {sorted(set(ids) - fed)}"
    menu = html.split('id="feed-menu"', 1)[1].split("</details>", 1)[0]
    preset_ids = re.findall(r"(?:\{ id: |feedPreset\()'([\w-]+)'", presets)
    assert len(preset_ids) == 9, preset_ids
    scoped = {"australia-now", "global-cyber"}
    assert scoped <= set(preset_ids), "a scope preset went, and its old links with it"
    for preset in preset_ids:
        if preset in scoped:
            assert f'data-preset="{preset}"' not in html, f"{preset} is back in the markup"
            continue
        assert f'data-preset="{preset}"' in menu, preset
        assert f'data-preset-count="{preset}"' in menu, preset
        assert f'data-preset-widen="{preset}"' in menu, preset
    assert html.count("data-preset=") == len(preset_ids) - len(scoped), "a preset outside the menu"
    assert html.count("data-section-body=") == 1 and 'data-section-body="events"' in html
    assert 'data-count-for="events"' in html


def test_every_old_anchor_opens_the_view_that_now_holds_it():
    """No #sec-* id is a place on the page any more, and old links still carry all of them.

    Bookmarks, event.html and history.html before this change, and other people's pages link to
    the sections the dashboard used to have. Each has to open the view and scope that show what
    it used to; the failure mode if one does not is a link that quietly opens the dashboard,
    which looks like a working link to the wrong place rather than a broken one.
    """
    js = read("site/assets/hud.js")
    block = js.split("export const LEGACY_ANCHORS = {", 1)[1].split("\n};", 1)[0]
    mapped = set(re.findall(r"'(sec-[\w-]+)':", block))
    old = {"sec-australia-now", "sec-global-cyber", "sec-ai-cyber", "sec-overview", "sec-trends",
           "sec-the-crew", "sec-system", "sec-world-map", "sec-sources",
           "sec-source-health"} | {f"sec-{i}" for i in _section_ids(js)}
    assert old <= mapped, f"unmapped: {sorted(old - mapped)}"
    assert "'sec-global-cyber': { view: 'events', scope: 'global' }" in block
    # THE CREW & SYSTEM split three ways, so each half of the old view's anchors opens its own.
    for anchor, view in (("sec-the-crew", "crew"), ("sec-system", "system"),
                         ("sec-sources", "sources"), ("sec-source-health", "sources"),
                         ("source-health", "sources")):
        assert f"'{anchor}': {{ view: '{view}' }}" in block, anchor
    parse = js.split("export function parseLocation", 1)[1].split("\n}\n", 1)[0]
    assert "Object.hasOwn(LEGACY_ANCHORS, anchor)" in parse, "an old anchor is not looked up"
    views = js.split("export function initViews", 1)[1]
    assert "if (legacy) window.history.replaceState" in views, "the old address is not rewritten"
    assert "addEventListener('hashchange'" in views, "an in-page old anchor is not followed"


def test_a_sub_section_jump_keeps_correcting_until_it_settles():
    """One scrollIntoView lands in the wrong place, for a reason invisible in a diff.

    The event lists above the target are content-visibility: auto with a flat 600px estimate. They
    lay out at their real heights one frame after the scroll, the content above the target shrinks,
    and the heading slides up under the sticky header. Traced at 1320px: the first scroll put it at
    120px (right), the next frame at 14px (hidden), and it took five corrections to settle. So a
    single call is wrong, and so is a loop that stops at the first frame that looks right — that is
    the frame before the one that spoils it.
    """
    views = read("site/assets/hud.js").split("export function initViews", 1)[1]
    assert "requestAnimationFrame(step)" in views, "the scroll correction is not a loop"
    assert "good < 3" in views, "stopping on one good frame stops one frame too early"


def test_the_sticky_header_height_is_measured_not_assumed():
    """scroll-margin-top cannot be a constant, because the header it clears is not one.

    .site-header measures 93px at 1320 and 222px at 375, and grows again once renderStrip() puts
    real timestamps in the status strip and the strip wraps. A fixed 120px hid every jump at 375px
    behind the header; a value sampled when initViews() runs was still 24px short of the final
    height. Only observing it is correct, so .subsection reads the measurement and the literal in
    the CSS is just the no-JS fallback.
    """
    css = read("site/assets/hud.css")
    js = read("site/assets/hud.js")
    assert "scroll-margin-top: calc(var(--header-h" in css, "the jump offset is not the measurement"
    assert "ResizeObserver(syncHeaderHeight)" in js, "a sampled header height goes stale"


def test_an_unreadable_snapshot_is_not_reported_as_zero_signals():
    """"Nothing matched" and "nothing was read" are different claims, and only one was being made.

    Measured on the published site with no data branch: all ten sections showed "0 SIGNALS" and
    "NO SIGNALS IN THIS SNAPSHOT." — a confident statement about a snapshot that had 404'd, made
    ten times over. The markup's own defaults said it too, so it was on the page before any fetch
    resolved. history.html's count and the map's count already said AWAITING DATA for this exact
    reason; the dashboard sections were the ones that did not.
    """
    js = read("site/assets/hud.js")
    block = js.split("export function renderSections", 1)[1].split("\nexport ", 1)[0]
    assert "const unread = Boolean(data.unread)" in block, "renderSections cannot tell the two apart"
    assert "unread ? 'AWAITING DATA'" in block, "an unread section still reports a count"
    assert "'UNKNOWN — NO SNAPSHOT WAS READ.'" in block, "an unread section still claims to be empty"
    assert "renderSections({ events: [], unread: true })" in js, "the failed load does not say so"
    assert ">0 SIGNALS<" not in read("site/index.html"), "the markup claims zero before any fetch"


def test_the_crew_roster_renders_even_when_the_feed_cannot_be_read():
    """The roster is presentation, not a reading of the feed, so a failed load must not hide it.

    Measured on the published site, which has no data branch yet: data/live.json 404s, loadData()
    throws, and main() returned early from the catch — so THE CREW served an empty grid underneath
    a count that still read "16 AGENTS". 0 of 16 tiles, no portraits, no personas, no status. The
    roster, the stage list and the per-agent ownership table describe what the system *is* rather
    than what the last run found; all three ship with the site and have to render on both paths.
    The org chart reads its own file and the System and Sources pages are handed null for what
    could not be read, so they belong on the same side: each says what is missing itself.

    The converse matters just as much, hence the second loop. The index and the gauges compute
    "0 events, LOW" from an empty list and the headline list would claim this snapshot has no
    headlines when the truth is that no snapshot was read. Those are fabrications, so they stay
    behind the guard: an absence is honest, an invented zero is not.
    """
    js = read("site/assets/hud.js")
    body = js.split("export async function main", 1)[1].split("\n}", 1)[0]
    unguarded, sep, guarded = body.partition("if (!data) {")
    assert sep, "main() no longer has a no-data guard, so this test cannot tell the halves apart"
    for call in ("renderStrip(", "renderCrew(", "renderCrewRun(", "renderOrg(",
                 "renderPage('system', renderSystemPage,", "renderPage('sources', renderSourcesPage,"):
        assert call in unguarded, f"{call} sits behind the no-data guard; it ships with the site"
    for call in ("renderIndex(", "renderGauges(", "renderHeadlines("):
        assert call not in unguarded, f"{call} on empty input publishes a figure nothing measured"
        assert call in guarded, f"{call} is no longer called at all"


def test_a_deep_link_to_a_sub_section_is_finished_after_the_first_render():
    """initViews() runs before the data arrives, so on load the target is not where it will end up.

    Measured on a cold load of #sec-vulnerabilities: the scroll settled at 960 against a final
    heading position of 3926 — the right tab open, several screens short of the section the link
    named. main() has to come back to a block inside a view (#sec-trends) once the views hold
    their content.
    """
    js = read("site/assets/hud.js")
    assert "return { activate, rescrollToHash }" in js, "initViews does not expose the deep-link fix"
    assert "rescrollToHash" in js.split("export async function main", 1)[1], "main() never calls it"


def _main_body(js: str) -> str:
    return js.split("export async function main", 1)[1].split("\nasync function mainEvent", 1)[0]


def test_trends_have_a_home_and_a_jump_link_but_no_section_body():
    """TRENDS reads trends.json, not the event list, so it is not one of the feeds.

    Given a data-section-body it would be counted as a list with nothing to render; outside every
    view it would show on all of them. It sits inside the Dashboard with its own host, and the
    old #sec-trends link (and a #sec-trends fragment on the dashboard) still lands on it.
    """
    html = read("site/index.html")
    main = html.split('<main id="main"', 1)[1].split("</main>", 1)[0]
    dashboard = main.split('id="view-dashboard"', 1)[1].split('id="view-events"', 1)[0]
    for needle in ('id="sec-trends"', 'id="trends"', 'id="trends-count"', 'id="trends-led"'):
        assert needle in dashboard, needle
    js = read("site/assets/hud.js")
    assert "'sec-trends': { view: 'dashboard', scope: 'all', anchor: 'sec-trends' }" in js
    assert "dashboard: ['sec-trends']" in js, "#sec-trends is not an anchor inside the dashboard"
    assert 'data-section-body="trends"' not in html


def test_every_trend_state_is_a_word_and_a_glyph():
    schema = json.loads(read("schemas/trends.schema.json"))
    states = schema["$defs"]["state"]["enum"]
    js = read("site/assets/hud.js")
    block = js.split("export const TREND_STATE", 1)[1].split("};", 1)[0]
    for state in states:
        assert re.search(rf"\b{state}: \{{ glyph: '[^']+', label: '[A-Z ]+' \}}", block), state
    assert "'aria-hidden': 'true', text: info.glyph" in js, "the glyph is read out as well as the word"


def test_trends_are_read_before_the_sections_and_the_deep_link_settle():
    """EMERGING THREATS reads which events are rising, and #sec-trends is a deep-link target,
    so trends.json has to be rendered before the first refresh and the final rescroll."""
    body = _main_body(read("site/assets/hud.js"))
    readable = body.split("if (!data) {", 1)[1].split("\n  }\n", 1)[1]
    trends = readable.index("renderTrends(await getJson(`${data.base}trends.json`")
    assert trends < readable.index("refresh();\n") < readable.rindex("views?.rescrollToHash()")
    unread = body.split("if (!data) {", 1)[1].split("\n  }\n", 1)[0]
    assert "renderTrends(null, [], { unread: true })" in unread
    assert unread.index("renderTrends") < unread.index("rescrollToHash")


def test_missing_trends_are_not_reported_as_no_trends():
    js = read("site/assets/hud.js")
    block = js.split("export function renderTrends", 1)[1].split("\n}\n", 1)[0]
    assert "'UNKNOWN — NO SNAPSHOT WAS READ.'" in block
    assert "'NO TRENDS WERE PUBLISHED WITH THIS SNAPSHOT.'" in block
    assert "count.textContent = 'AWAITING DATA'" in block
    assert ">0 TOPICS<" not in read("site/index.html")


def test_a_day_before_collection_began_is_not_drawn_as_zero():
    js = read("site/assets/hud.js")
    chart = js.split("function activityChart", 1)[1].split("\nfunction ", 1)[0]
    assert "d.coverage === 'none'" in chart and "trend-day__gap" in chart
    table = js.split("function activityTable", 1)[1].split("\nfunction ", 1)[0]
    assert "d.coverage === 'none' ? '—'" in table
    css = read("site/assets/hud.css")
    assert '.trend-day[data-coverage="partial"] .trend-day__bar { fill: none;' in css


def test_emerging_threats_lists_events_the_trends_say_are_rising():
    js = read("site/assets/hud.js")
    sections = js.split("export const SECTIONS", 1)[1].split("];", 1)[0]
    emerging = sections.split("id: 'emerging-threats'", 1)[1].split("\n  },", 1)[0]
    assert "trending.has(e.event_id)" in emerging
    block = js.split("export function renderTrends", 1)[1].split("\n}\n", 1)[0]
    assert "t.state === 'new' || t.state === 'rising'" in block
    assert block.index("trending.clear()") < block.index("if (!readable)")
