// CyberPulse-AI: the SYSTEM and SOURCES views. A placeholder that keeps hud.js's contract, so the
// shell can be built and tested before the pages themselves land:
//   renderSystemPage(host, { status, health, crew, live }) -> { label, led }
//   renderSourcesPage(host, { health, live }) -> { label, led }
// Any argument may be null. Each clears `host` and refills it; `label` goes in the view head's
// count and `led` ('ok' | 'warn' | 'fail' | 'idle') on its status light.

function placeholder(host, text) {
  if (!host || typeof document === 'undefined') return;
  const p = document.createElement('p');
  p.className = 'hint';
  p.textContent = text;
  host.replaceChildren(p);
}

export function renderSystemPage(host) {
  placeholder(host, 'How a collection runs, which steps are code and which are AI, and the last completed run will be drawn here.');
  return { label: 'AWAITING DATA', led: 'idle' };
}

export function renderSourcesPage(host) {
  placeholder(host, 'The health, standing and reputation of every source will be drawn here.');
  return { label: 'AWAITING DATA', led: 'idle' };
}
