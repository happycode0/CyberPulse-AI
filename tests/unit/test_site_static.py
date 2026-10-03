"""Static assertions on the public site's files. No browser is needed."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SITE = ROOT / "site"


def read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


# The slate-and-cyan theme (PLAN.md 8.1). The spec hues are the tokens; --sev-critical-text is
# the one text tint the contrast floor forced (the spec red is 3.56:1 as badge text).
PALETTE = {
    "--bg-page": "#0F172A", "--bg-surface": "#1E293B", "--line": "#334155",
    "--line-strong": "#64748B", "--text-head": "#F8FAFC", "--text": "#E2E8F0",
    "--text-dim": "#CBD5E1", "--text-muted": "#94A3B8", "--accent": "#06B6D4",
    "--beat-ai": "#A78BFA", "--ai-inference": "#F472B6",
    "--sev-critical": "#EF4444", "--sev-critical-text": "#F26B6B", "--sev-high": "#F59E0B",
    "--sev-medium": "#FBBF24", "--sev-low": "#10B981", "--sev-info": "#38BDF8",
}
BACKGROUNDS = ("--bg-page", "--bg-surface")


def test_css_defines_every_palette_token():
    css = read("site/assets/hud.css").replace(" ", "")
    for t in PALETTE:
        assert f"{t}:" in css, t


def test_palette_hex_values_match_the_spec():
    css = read("site/assets/hud.css").replace(" ", "")
    for token, value in PALETTE.items():
        assert f"{token}:{value}" in css, token


def _relative_luminance(hex_colour: str) -> float:
    channels = []
    for i in (1, 3, 5):
        c = int(hex_colour[i:i + 2], 16) / 255
        channels.append(c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4)
    r, g, b = channels
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _contrast(a: str, b: str) -> float:
    la, lb = _relative_luminance(a), _relative_luminance(b)
    return (max(la, lb) + 0.05) / (min(la, lb) + 0.05)


def _over(hex_colour: str, background: str, alpha: float) -> str:
    """`hex_colour` at `alpha` composited over `background`, as a hex."""
    mixed = (
        round(int(hex_colour[i:i + 2], 16) * alpha + int(background[i:i + 2], 16) * (1 - alpha))
        for i in (1, 3, 5)
    )
    return "#" + "".join(f"{c:02X}" for c in mixed)


# PLAN.md 8.1 publishes a ratio per token and rests accessibility claims on them: 4.5 for
# anything that carries text (WCAG 1.4.3 at small sizes) and 3.0 for --line-strong, which draws
# control borders, and for the severity hues used as fills and borders (WCAG 1.4.11). Pinning
# the hexes alone would let a later retheme keep the table and lose the compliance, so the
# thresholds are computed here rather than trusted.
def test_palette_meets_the_contrast_ratios_the_plan_claims():
    # Measured on *both* backgrounds. The site has exactly two (page and surface) and the
    # surface is the lighter, so the worse case for every foreground; an earlier version checked
    # one background only and missed a control border at 2.8:1 on the other.
    minimums = {"--line-strong": 3.0, "--sev-critical": 3.0, "--text": 7.0, "--text-head": 7.0}
    for token in ("--text-dim", "--text-muted", "--accent", "--beat-ai", "--ai-inference",
                  "--sev-critical-text", "--sev-high", "--sev-medium", "--sev-low", "--sev-info"):
        minimums[token] = 4.5
    for background in BACKGROUNDS:
        for token, floor in minimums.items():
            ratio = _contrast(PALETTE[token], PALETTE[background])
            assert ratio >= floor, f"{token} is {ratio:.2f}:1 on {background}, needs {floor}:1"


# A severity badge is the spec's: its hue at 10% behind text in the hue. The text is therefore
# read on the tint, not on the surface, and the tint is lighter, so it is measured there. This
# is the check that forced --sev-critical-text: #EF4444 is 3.56:1 on its own tint.
BADGES = {
    "critical": ("--sev-critical", "--sev-critical-text"),
    "high": ("--sev-high", "--sev-high"),
    "medium": ("--sev-medium", "--sev-medium"),
    "low": ("--sev-low", "--sev-low"),
    "info": ("--sev-info", "--sev-info"),
    "unknown": ("--text-muted", "--text-muted"),
}


def test_badge_text_clears_4_5_on_its_own_tint():
    css = read("site/assets/hud.css")
    for name, (hue, text) in BADGES.items():
        rule = re.search(rf'\[data-severity="{name}"\]\s*\{{([^}}]*)\}}', css).group(1)
        assert f"--sev-text: var({text})" in rule, f"{name} badge text is not {text}"
        assert re.search(r"--sev-tint:\s*rgba\([^)]*,\s*0\.1\)", rule), f"{name} tint is not 10%"
    pairs = [*BADGES.values(), ("--beat-ai", "--beat-ai")]
    for hue, text in pairs:
        for background in BACKGROUNDS:
            tint = _over(PALETTE[hue], PALETTE[background], 0.1)
            ratio = _contrast(PALETTE[text], tint)
            assert ratio >= 4.5, f"{text} is {ratio:.2f}:1 on the {hue} tint over {background}"


# The spec red stays the token for fills and borders, where 3:1 is the bar, but it is never
# text: anything written in red uses the tint.
def test_the_spec_red_is_never_used_as_text():
    css = re.sub(r"/\*.*?\*/", "", read("site/assets/hud.css"), flags=re.DOTALL)
    assert not re.search(r"(?<![-\w])color:\s*var\(--sev-critical\)", css)
    assert not re.search(r"--sev-text:\s*var\(--sev-critical\)", css)


# A country with no events fills in --map-none and the ramp climbs from there. This started as
# --line, and lifting --line for the brighter backgrounds pushed it past the old tier-1 hex, so
# one event rendered *quieter* than none and the legend read backwards. Luminance order is the
# whole meaning of a ramp, so it is asserted rather than eyeballed.
def test_the_map_load_ramp_ascends_in_luminance():
    css = read("site/assets/hud.css")
    tiers = [re.search(r"--map-none:\s*(#[0-9A-Fa-f]{6})", css).group(1)]
    for n in (1, 2, 3, 4):
        value = re.search(rf'\[data-load="{n}"\][^{{]*\{{\s*--map-load:\s*([^;]+);', css).group(1).strip()
        token = re.fullmatch(r"var\((--[\w-]+)\)", value)
        tiers.append(PALETTE[token.group(1)] if token else value)
    lums = [_relative_luminance(c) for c in tiers]
    assert lums == sorted(lums), f"ramp is not monotonic: {list(zip(tiers, lums))}"


def test_every_keyframes_animation_is_gated_on_reduced_motion():
    css = read("site/assets/hud.css")
    names = re.findall(r"@keyframes\s+([\w-]+)", css)
    assert names, "expected at least one animation"
    for name in names:
        gated = rf"prefers-reduced-motion:\s*no-preference[^}}]*?animation[^;]*{name}"
        assert re.search(gated, css, re.DOTALL), name


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


# Everything the page used to show by section is still named on it: the geographic presets and
# intelligence feeds in the sidebar, the dashboard blocks, the crew view, and the scope and beat
# controls in the header.
def test_index_lists_every_section():
    html = read("site/index.html")
    for name in (
        "AUSTRALIA NOW", "GLOBAL CYBER", "AI + CYBER", "ACTIVE EXPLOITATION",
        "DEVELOPING EVENTS", "EMERGING THREATS", "THREAT ACTORS", "VULNERABILITIES",
        "POLICY &amp; RESEARCH", "TRENDS", "THE CREW", "SYSTEM", "DASHBOARD", "EVENTS",
        "WORLD MAP", "SEVERITY DISTRIBUTION", "TOP SIGNALS",
    ):
        assert name in html, name
    for scope in ("au", "global", "all"):
        assert re.search(rf'name="scope" value="{scope}"', html), scope
    assert re.search(r'name="scope" value="au" checked', html), "AUSTRALIA is not preselected"
    for beat in ("cyber", "ai"):
        assert f'data-beat-toggle="{beat}"' in html, beat


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
    # Derived from the stylesheet rather than a hardcoded family list, so dropping or adding a
    # face cannot leave this test asserting a licence for a font the site no longer serves —
    # or, worse, pass while a newly added one ships unlicensed.
    fonts = SITE / "assets" / "fonts"
    css = (SITE / "assets" / "hud.css").read_text()
    families = set(re.findall(r'@font-face\s*\{[^}]*?font-family:\s*"([^"]+)"', css))
    assert families, "no @font-face rule found in hud.css"
    licence = (fonts / "OFL.txt").read_text()
    assert licence.count("SIL OPEN FONT LICENSE Version 1.1") >= len(families)
    for family in families:
        assert family in licence, family
    # Every served file exists, and every shipped file is served: an unreferenced woff2 is dead
    # weight in a repository and a face with no file is a silent fallback to a system font.
    served = {u for u in re.findall(r'src:\s*url\("fonts/([^"]+)"', css)}
    shipped = {f.name for f in fonts.glob("*.woff2")}
    assert served, "no @font-face src found in hud.css"
    assert served == shipped, f"served but missing: {served - shipped}; shipped but unused: {shipped - served}"
    for f in fonts.glob("*.woff2"):
        assert f.read_bytes()[:4] == b"wOF2", f


def test_vendored_libraries_ship_with_their_licence():
    vendor = SITE / "assets" / "vendor"
    for name in ("d3-geo", "d3-array", "topojson-client"):
        text = (vendor / f"ISC-{name}.txt").read_text()
        assert "Permission to use, copy, modify, and/or distribute this software" in text, name
    for name in ("d3-geo.min.js", "topojson-client.min.js"):
        head = (vendor / name).read_text()[:500]
        assert "ISC licence" in head and "esbuild" in head, name


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


def test_the_site_roster_covers_every_agent_the_plan_specifies():
    # PLAN.md 4 is the authoritative roster, so the site's CREW array is checked against it
    # rather than against a number written here. The site once shipped fifteen cards while the
    # plan specified sixteen agents, and nothing caught it: RIPPERDOC, the model scout, was
    # simply absent from the page that claims to list the crew. The crew is eight since
    # October 2026; the set comparison holds whatever the number is.
    plan = read("PLAN.md")
    section = plan[plan.index("### 4.1"):plan.index("### 4.4")]
    specified = set(re.findall(r"^#### ([A-Z]+) \u2014", section, re.MULTILINE))
    js = read("site/assets/hud.js")
    roster = js[js.index("export const CREW = ["):js.index("\n];", js.index("export const CREW = ["))]
    shipped = re.findall(r"callsign: '([A-Z]+)'", roster)
    assert specified, "found no agents in PLAN.md section 4"
    assert set(shipped) == specified, (
        f"missing from the site: {sorted(specified - set(shipped))}; "
        f"on the site but not in the plan: {sorted(set(shipped) - specified)}"
    )
    assert len(shipped) == len(set(shipped)), "a callsign appears twice in CREW"


def test_every_agent_has_its_own_portrait_and_a_published_job():
    # Two lookups are keyed by hand off CREW, and a typo in either degrades silently: a missing
    # BOT_PARTS key renders the shared shell with no accessory, and a missing CREW_JOBS key falls
    # back to the short beat. Both are quiet failures, so they are asserted instead.
    js = read("site/assets/hud.js")
    roster = js[js.index("export const CREW = ["):js.index("\n];", js.index("export const CREW = ["))]
    entries = re.findall(r"callsign: '([A-Z]+)', face: '(\w+)'", roster)
    assert len(entries) == len(re.findall(r"callsign: '", roster)), "an entry is missing its face"
    faces = [f for _, f in entries]
    assert len(faces) == len(set(faces)), "two agents share a portrait accessory"
    parts_block = js[js.index("const BOT_PARTS = {"):js.index("\n};", js.index("const BOT_PARTS = {"))]
    drawn = set(re.findall(r"^  (\w+): \(\) =>", parts_block, re.MULTILINE))
    assert set(faces) == drawn, f"undrawn: {sorted(set(faces) - drawn)}; unused: {sorted(drawn - set(faces))}"
    jobs_block = js[js.index("const CREW_JOBS = {"):js.index("\n};", js.index("const CREW_JOBS = {"))]
    described = set(re.findall(r"^  ([A-Z]+): \{", jobs_block, re.MULTILINE))
    assert {c for c, _ in entries} == described, (
        f"no job published for: {sorted({c for c, _ in entries} - described)}"
    )


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


# PLAN.md 8.4 allows exactly two keyframes to animate SVG stroke-dashoffset: the EKG pulse
# line and the pipeline dash-trace (8.3 items 2 and 8). A travelling dash cannot be done any
# other way. Every other keyframe is held to transform/opacity.
DASH_KEYFRAMES = {"ekg-travel", "trace-flow"}
CHEAP = {"transform", "opacity"}


def test_css_has_no_flicker_and_animates_only_cheap_properties():
    css = read("site/assets/hud.css")
    assert "flicker" not in css.lower()
    blocks = re.findall(r"@keyframes\s+([\w-]+)\s*\{(.*?)\n\}", css, re.DOTALL)
    assert {name for name, _ in blocks} >= DASH_KEYFRAMES
    for name, block in blocks:
        allowed = CHEAP | ({"stroke-dashoffset"} if name in DASH_KEYFRAMES else set())
        for prop in re.findall(r"([\w-]+)\s*:", block):
            assert prop in allowed, f"{name} animates {prop}"


def test_fx_toggle_label_is_fixed_and_state_is_aria_pressed():
    html = read("site/index.html")
    button = re.search(r'<button[^>]*id="fx-toggle"[^>]*>(.*?)</button>', html, re.DOTALL)
    assert button and button.group(1).strip() == "FX OFF"
    js = read("site/assets/hud.js")
    assert "FX ON" not in html and "FX ON" not in js
    assert "textContent" not in js[js.index("export function initFxToggle"):js.index("export function initFxField")]


def test_event_cards_never_use_the_event_id_as_a_dom_id():
    js = read("site/assets/hud.js")
    assert not re.search(r"\bid:\s*event\.event_id", js)
    assert "data-event-id" in js
    assert not re.search(r"href:\s*`#\$\{e\.event_id\}`", js)


# PLAN.md 8.3 item 13 and the 10.x acceptance criteria treat the history page as a product
# surface, so a visitor landing on the dashboard has to be able to find it.
def test_history_page_is_reachable_from_the_dashboard():
    html = read("site/index.html")
    # In the sticky header, so it is one click away from every view, not only from the one
    # whose sidebar happens to be open.
    header = html.split('<header class="site-header"', 1)[1].split("</header>", 1)[0]
    assert 'href="history.html"' in header, "the header links to history.html"
    sidebar = html.split('<nav class="sidebar"', 1)[1].split("</nav>", 1)[0]
    assert "history.html" not in sidebar, "the sidebar holds views and presets, not pages"


# The map counts events per ISO id in aria-label, so the click, the paired <select> and the
# filter all have to work from the same set of country tokens, or they drift apart.
def test_map_click_filters_by_every_token_for_the_country():
    js = read("site/assets/map.js")
    hud = read("site/assets/hud.js")
    assert "n3Tokens.get(n3)[0]" not in js, "the click must not stop at the first token"
    assert "new Set(tokens)" in js, "the click selects every token of the ISO id"
    assert "n3Tokens.has(value)" in js, "the <select> is keyed by ISO id, not by single tokens"
    assert "state.selected.country = new Set([token])" not in hud, (
        "the country filter must accept the whole token set the map hands it"
    )


# A browser requests /favicon.ico on its own whenever no icon is declared, so every page was
# logging a 404 that no amount of reading the code would explain. Declaring one SVG stops the
# request outright and serves every size from one file; `img-src 'self'` already allows it.
def test_every_page_declares_a_self_hosted_favicon():
    icon = SITE / "assets" / "favicon.svg"
    assert icon.is_file(), "site/assets/favicon.svg is missing"
    svg = icon.read_text(encoding="utf-8")
    for page in SITE.glob("*.html"):
        html = page.read_text(encoding="utf-8")
        href = re.search(r'<link rel="icon"[^>]*href="([^"]+)"', html)
        assert href, f"{page.name} declares no favicon, so the browser will ask for favicon.ico"
        assert (SITE / href.group(1)).is_file(), href.group(1)
    # The mark is the wordmark's, so it has to track the palette rather than drift from it.
    for token in ("--bg-page", "--accent"):
        assert PALETTE[token] in svg, f"favicon does not use {token}"
