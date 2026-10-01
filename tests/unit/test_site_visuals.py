"""Static assertions on the Task 15 site additions: map, replay, detail, history."""

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


def test_pipeline_replay_is_labelled_as_a_replay():
    assert "COLLECTION REPLAY" in read("site/index.html")


def test_pipeline_stages_match_the_spec():
    js = read("site/assets/hud.js")
    for s in ("SOURCES", "COLLECT", "MATCH", "VERIFY", "ENRICH", "CROSS-REF", "SCORE", "PUBLISH"):
        assert s in js


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
# inside the OVERVIEW tab, so there is nothing left to pause.
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


def test_every_tab_panel_is_a_sibling_under_the_tab_strip():
    """Guards the reason the tabs looked broken.

    The panels used to be split: AUSTRALIA NOW above, the rest below the gauges, the world
    map and the filter panel. Selecting a tab swapped a panel ~1900px down the page, so the
    viewport did not visibly change and the tab read as dead. Every target the nav links to
    has to live in the single .tab-panels container, with nothing untabbed between them.
    """
    html = read("site/index.html")
    nav = html.split('<nav class="site-nav"', 1)[1].split("</nav>", 1)[0]
    targets = re.findall(r'href="#(sec-[\w-]+)"', nav)
    assert len(targets) == 5, targets
    panels = html.split('<div class="tab-panels">', 1)[1]
    for target in targets:
        assert f'id="{target}"' in panels, target


def test_every_section_the_js_renders_has_a_home_in_the_markup():
    """Thirteen tabs became five, and the other sections moved inside two of the panels.

    A section that renders into no container is silently empty, and one that sits outside every
    panel is the opposite failure: initTabs() only hides the panels it knows about, so an
    orphaned section would show on every tab at once. Both are invisible in a diff, hence this.
    """
    html = read("site/index.html")
    js = read("site/assets/hud.js")
    ids = re.findall(r"\{\s*id: '([\w-]+)',\s*match:", js.split("export const SECTIONS", 1)[1].split("];", 1)[0])
    assert len(ids) == 10, ids
    panels = html.split('<div class="tab-panels">', 1)[1].split("</main>", 1)[0]
    for section_id in ids:
        assert f'data-section-body="{section_id}"' in panels, section_id
        assert f'data-count-for="{section_id}"' in panels, section_id
    assert html.count('data-section-body=') == len(ids), "a body with no section renders nothing"


def test_a_section_id_that_is_not_a_tab_resolves_to_its_panel():
    """Most section ids no longer have a tab, and three callers still pass them.

    renderHeadlines() builds href="#sec-<section>", revealEvent() calls activateTab('sec-...'),
    and old bookmarks carry the same ids. initTabs() has to map such an id to the panel that
    contains it; the failure mode if it does not is a link that quietly opens the first tab,
    which looks like a working link to the wrong place rather than a broken one.
    """
    js = read("site/assets/hud.js")
    tabs = js.split("export function initTabs", 1)[1]
    assert "closest('[role=\"tabpanel\"]')" in tabs, "no resolution from a section id to its panel"
    assert "scrollIntoView({ block: 'start' })" in tabs, "a sub-section target is not scrolled to"


def test_a_sub_section_jump_keeps_correcting_until_it_settles():
    """One scrollIntoView lands in the wrong place, for a reason invisible in a diff.

    The event lists above the target are content-visibility: auto with a flat 600px estimate. They
    lay out at their real heights one frame after the scroll, the content above the target shrinks,
    and the heading slides up under the sticky header. Traced at 1320px: the first scroll put it at
    120px (right), the next frame at 14px (hidden), and it took five corrections to settle. So a
    single call is wrong, and so is a loop that stops at the first frame that looks right — that is
    the frame before the one that spoils it.
    """
    tabs = read("site/assets/hud.js").split("export function initTabs", 1)[1]
    assert "requestAnimationFrame(step)" in tabs, "the scroll correction is not a loop"
    assert "good < 3" in tabs, "stopping on one good frame stops one frame too early"


def test_the_sticky_header_height_is_measured_not_assumed():
    """scroll-margin-top cannot be a constant, because the header it clears is not one.

    .site-header measures 93px at 1320 and 222px at 375, and grows again once renderStrip() puts
    real timestamps in the status strip and the strip wraps. A fixed 120px hid every jump at 375px
    behind the header; a value sampled when initTabs() runs was still 24px short of the final
    height. Only observing it is correct, so .subsection reads the measurement and the literal in
    the CSS is just the no-JS fallback.
    """
    css = read("site/assets/hud.css")
    js = read("site/assets/hud.js")
    assert "scroll-margin-top: calc(var(--header-h" in css, "the jump offset is not the measurement"
    assert "ResizeObserver(syncHeaderHeight)" in js, "a sampled header height goes stale"


def test_a_deep_link_to_a_sub_section_is_finished_after_the_first_render():
    """initTabs() runs before the data arrives, so on load the target is not where it will end up.

    Measured on a cold load of #sec-vulnerabilities: the scroll settled at 960 against a final
    heading position of 3926 — the right tab open, several screens short of the section the link
    named. main() has to come back to it once the sections hold their events.
    """
    js = read("site/assets/hud.js")
    assert "return { activate, rescrollToHash }" in js, "initTabs does not expose the deep-link fix"
    assert "rescrollToHash" in js.split("export async function main", 1)[1], "main() never calls it"
