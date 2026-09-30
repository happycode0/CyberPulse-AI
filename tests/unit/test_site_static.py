"""Static assertions on the public site's files. No browser is needed."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SITE = ROOT / "site"


def read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def test_css_defines_every_palette_token():
    css = read("site/assets/hud.css").replace(" ", "")
    for t in (
        "--bg-void", "--bg-panel", "--bg-raised", "--line", "--line-strong", "--text",
        "--text-dim", "--text-muted", "--cyan", "--teal", "--magenta",
        "--sev-critical", "--sev-high", "--sev-medium", "--sev-low", "--sev-info",
    ):
        assert f"{t}:" in css, t


def test_palette_hex_values_match_the_spec():
    css = read("site/assets/hud.css").replace(" ", "")
    expected = {
        "--bg-void": "#05070A", "--bg-panel": "#0B1118", "--bg-raised": "#111C27",
        "--line": "#16283A", "--line-strong": "#41718A", "--text": "#E6F1F5",
        "--text-dim": "#9DB2C0", "--text-muted": "#7C93A3", "--cyan": "#00E5FF",
        "--teal": "#0EB0C2", "--magenta": "#FF2A6D", "--sev-critical": "#FF3B5C",
        "--sev-high": "#FF8A1F", "--sev-medium": "#FFD23F", "--sev-low": "#3DDC97",
        "--sev-info": "#4FC3F7",
    }
    for token, value in expected.items():
        assert f"{token}:{value}" in css, token


def test_every_keyframes_animation_is_gated_on_reduced_motion():
    css = read("site/assets/hud.css")
    names = re.findall(r"@keyframes\s+([\w-]+)", css)
    assert names, "expected at least one animation"
    for name in names:
        gated = rf"prefers-reduced-motion:\s*no-preference[^}}]*?animation[^;]*{name}"
        assert re.search(gated, css, re.S), name


def test_no_animation_declared_outside_a_motion_gate():
    css = read("site/assets/hud.css")
    depth_gate = None
    depth = 0
    for line in css.splitlines():
        if "@media" in line and "prefers-reduced-motion" in line and "no-preference" in line:
            depth_gate = depth
        if re.search(r"(^|[\s;{])animation(-name)?\s*:", line) and "none" not in line:
            assert depth_gate is not None, f"ungated animation: {line.strip()}"
        depth += line.count("{") - line.count("}")
        if depth_gate is not None and depth <= depth_gate:
            depth_gate = None


def test_site_never_says_live():
    pages = list(SITE.rglob("*.html"))
    assert pages
    for p in pages:
        assert "live" not in p.read_text().lower().replace("live.json", "")
    for p in (SITE / "assets").glob("hud.*"):
        text = p.read_text().lower().replace("live.json", "")
        assert not re.search(r"\blive\b", text), p


def test_index_declares_last_completed_collection():
    html = read("site/index.html")
    assert "LAST COMPLETED COLLECTION" in html
    assert "COLLECTION REPLAY" in html or "COLLECTION REPLAY" in read("site/assets/hud.js")


def test_index_lists_every_section():
    html = read("site/index.html")
    for name in (
        "AUSTRALIA NOW", "GLOBAL CYBER", "AI + CYBER", "ACTIVE EXPLOITATION",
        "DEVELOPING EVENTS", "EMERGING THREATS", "THREAT ACTORS", "VULNERABILITIES",
        "RESEARCH", "POLICY / REGULATION", "THE CREW", "SYSTEM",
    ):
        assert name in html, name


def test_csp_meta_is_present_and_forbids_inline_script():
    html = read("site/index.html")
    assert 'http-equiv="Content-Security-Policy"' in html
    assert "unsafe-inline" not in html and "unsafe-eval" not in html
    assert "<script>" not in html and not re.search(r"<script(?![^>]*\bsrc=)[^>]*>", html)
    assert not re.search(r"\son\w+\s*=", html)


def test_csp_locks_down_origins():
    html = read("site/index.html")
    csp = re.search(r'http-equiv="Content-Security-Policy"\s+content="([^"]+)"', html).group(1)
    for directive in ("default-src 'none'", "script-src 'self'", "font-src 'self'",
                      "connect-src 'self'", "object-src 'none'", "base-uri 'none'"):
        assert directive in csp, directive
    assert "*" not in csp and "http:" not in csp


def test_no_external_origins_are_referenced():
    for p in list(SITE.rglob("*.html")) + list(SITE.rglob("*.js")):
        if "vendor" in p.parts:
            continue
        # xmlns URIs are namespace identifiers, not requests.
        text = p.read_text().replace("http://www.w3.org/2000/svg", "")
        assert not re.search(r"https?://(?!localhost)", text), p


def test_css_references_no_external_origin():
    css = read("site/assets/hud.css")
    assert not re.search(r"https?://", css)
    assert "@import" not in css


def test_fonts_ship_with_their_licence():
    fonts = SITE / "assets" / "fonts"
    licence = (fonts / "OFL.txt").read_text()
    assert licence.count("SIL OPEN FONT LICENSE Version 1.1") >= 3
    for family in ("Orbitron", "Chakra Petch", "JetBrains Mono"):
        assert family in licence, family
    woff2 = list(fonts.glob("*.woff2"))
    assert woff2
    for f in woff2:
        assert f.read_bytes()[:4] == b"wOF2", f


def test_every_font_face_file_exists():
    css = read("site/assets/hud.css")
    files = re.findall(r"url\(['\"]?(fonts/[\w.-]+\.woff2)", css)
    assert files
    for rel in files:
        assert (SITE / "assets" / rel).is_file(), rel


def test_font_preloads_point_at_real_files():
    html = read("site/index.html")
    hrefs = re.findall(r'rel="preload"[^>]*href="([^"]+\.woff2)"', html)
    assert hrefs
    for href in hrefs:
        assert (SITE / href).is_file(), href
    assert 'crossorigin' in html


def test_vendor_directory_exists_for_task_15():
    assert (SITE / "assets" / "vendor").is_dir()


def test_severity_is_conveyed_by_more_than_colour():
    js = read("site/assets/hud.js")
    assert "data-severity-shape" in js or "severityShape" in js
    for glyph in "◆▲●■○":
        assert glyph in js, glyph


def test_hud_js_exports_the_contract():
    js = read("site/assets/hud.js")
    for fn in ("loadData", "renderSections", "applyFilters", "renderPipeline",
               "renderIndex", "initFxToggle"):
        assert re.search(rf"export\s+(async\s+)?function\s+{fn}\b", js), fn


def test_hud_js_formats_times_in_sydney():
    js = read("site/assets/hud.js")
    assert "Australia/Sydney" in js and "en-AU" in js


def test_hud_js_persists_fx_choice_and_avoids_html_injection():
    js = read("site/assets/hud.js")
    assert "localStorage" in js and "fx-off" in js
    for banned in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval("):
        assert banned not in js, banned


def test_css_has_reduced_motion_and_fx_off_escape_hatches():
    css = read("site/assets/hud.css")
    assert ".fx-off" in css
    assert "content-visibility" in css
    assert "clip-path" in css


def test_css_has_no_flicker_and_animates_only_cheap_properties():
    css = read("site/assets/hud.css")
    assert "flicker" not in css.lower()
    for block in re.findall(r"@keyframes\s+[\w-]+\s*\{(.*?)\n\}", css, re.S):
        for prop in re.findall(r"([\w-]+)\s*:", block):
            assert prop in {"transform", "opacity", "stroke-dashoffset", "stroke-dasharray"}, prop
