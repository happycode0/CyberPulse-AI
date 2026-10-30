"""TODAY'S BRIEF on the dashboard: data/daily-report.json drawn by hud.js `renderBrief`, run for
real in node against a small fake DOM."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
HUD = ROOT / "site" / "assets" / "hud.js"
INDEX = ROOT / "site" / "index.html"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")

DRAW = r"""
const [hudPath, docPath] = process.argv.slice(2);
const m = await import(hudPath);
class FakeNode {}
class FakeText extends FakeNode {
  constructor(t) { super(); this.data = String(t); }
  get textContent() { return this.data; }
}
class FakeEl extends FakeNode {
  constructor(tag) { super(); this.tagName = tag.toUpperCase(); this.childNodes = []; this.attrs = {}; }
  setAttribute(k, v) { this.attrs[k] = v; }
  set className(v) { this.attrs.class = v; }
  get textContent() { return this.childNodes.map((c) => c.textContent).join(''); }
  set textContent(v) { this.childNodes = [new FakeText(v)]; }
  append(...nodes) { for (const n of nodes) this.childNodes.push(n instanceof FakeNode ? n : new FakeText(n)); }
  replaceChildren(...nodes) { this.childNodes = []; this.append(...nodes); }
}
const hosts = {};
globalThis.Node = FakeNode;
globalThis.document = {
  createElement: (t) => new FakeEl(t),
  createElementNS: (_, t) => new FakeEl(t),
  createTextNode: (t) => new FakeText(t),
  getElementById: (id) => (hosts[id] ||= new FakeEl('div')),
};
const links = [];
const walk = (n) => { if (n.tagName === 'A') links.push(n.attrs.href); (n.childNodes || []).forEach(walk); };
const { readFileSync } = await import('node:fs');
const doc = JSON.parse(readFileSync(docPath, 'utf8'));
m.renderBrief(doc);
walk(hosts['daily-brief']);
process.stdout.write(JSON.stringify({
  brief: hosts['daily-brief'].textContent, count: hosts['brief-count']?.textContent ?? null, links,
}));
"""

REPORT = {
    "generated_at": "2026-10-31T21:00:00Z",
    "pipeline_version": "1.0.0",
    "report": {
        "date": "2026-11-01",
        "written_at": "2026-10-31T20:40:00Z",
        "window_hours": 24,
        "summary": "A quiet day for Australia apart from one retail breach.",
        "items": [{"event_id": "evt-2026-000002", "title": "Retailer breach exposes customers",
                   "severity": "high", "why": "Australian customers' data was taken."}],
        "gaps": [{"topic": "health", "kind": "au_sector", "quiet_days": 9.2}],
        "model": "deepseek/deepseek-v4-flash",
    },
}


def draw(tmp_path, doc):
    (tmp_path / "draw.mjs").write_text(DRAW, encoding="utf-8")
    (tmp_path / "doc.json").write_text(json.dumps(doc), encoding="utf-8")
    done = subprocess.run(
        [NODE, str(tmp_path / "draw.mjs"), str(HUD), str(tmp_path / "doc.json")],
        capture_output=True, text=True, timeout=60, check=False,
    )
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def test_the_brief_shows_the_report_and_says_who_wrote_it(tmp_path):
    out = draw(tmp_path, REPORT)
    text = out["brief"]
    assert "A quiet day for Australia apart from one retail breach." in text
    assert "Retailer breach exposes customers" in text and "HIGH" in text
    assert "Australian customers' data was taken." in text
    assert "Australian health sector" in text and "9 days" in text
    assert "AI-WRITTEN" in text and "deepseek/deepseek-v4-flash" in text
    assert out["links"] == ["event.html?id=evt-2026-000002"]
    assert out["count"] == "1 NOV"
    assert "null" not in text and "undefined" not in text


@pytest.mark.parametrize("doc", [{"report": None}, None, {}])
def test_no_report_says_so_rather_than_inventing_one(tmp_path, doc):
    out = draw(tmp_path, doc)
    assert "No report yet" in out["brief"] and out["count"] == "NO REPORT YET"
    assert out["links"] == []


def test_the_dashboard_has_the_brief_panel():
    html = INDEX.read_text(encoding="utf-8")
    assert 'id="daily-brief"' in html and 'id="brief-count"' in html
    assert "TODAY'S BRIEF" in html
