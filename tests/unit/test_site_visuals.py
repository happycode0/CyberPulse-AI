"""Static assertions on the Task 15 site additions: map, replay, detail, history."""

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


def test_ticker_is_pausable():
    assert "aria-hidden" in read("site/index.html") and "pause" in read("site/assets/hud.js").lower()
