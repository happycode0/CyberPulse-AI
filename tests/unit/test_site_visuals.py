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
    assert len(targets) >= 12, targets
    panels = html.split('<div class="tab-panels">', 1)[1]
    for target in targets:
        assert f'id="{target}"' in panels, target
