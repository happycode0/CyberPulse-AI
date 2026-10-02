# Stage 1 — Foundation: pipeline + site

[← Stage 0 — Prerequisites](stage-0-prerequisites.md) · [Wiki home](README.md) ·
[Stage 2 — Ground truth →](stage-2-ground-truth.md)

**Status: 🟡 running, one step left.** The VM collects and builds the site's data every 15 minutes,
but that data does not reach the public site yet. Plan: [PLAN.md §9, Stage 1](../../PLAN.md#9-stages)
· Build record: [runbook Part 5](../vm200-runbook.md)

---

## What this stage gives you

The machinery that never needs an AI: collect from the sources, de-duplicate into events, score
them, and publish the result as JSON for a static site on GitHub Pages. It runs in the `worker`
container **on its own schedule, with or without Paperclip**.

## What is done

| Piece | State |
|---|---|
| Source registry, RSS/Atom and JSON collectors, normalisation, event resolution, scoring | ✅ |
| FAST lane every 15 min (11 sources) · NORMAL lane every 4 h | ✅ running on the VM |
| Publisher: builds `data/*.json`, validates against the schemas, secret-scans, **fails closed** | ✅ 532 files at the 2026-10-02 20:15 run |
| The site (`site/`): homepage, event page, history | ✅ on GitHub Pages, **still showing sample data** |
| Push of `data/` to the `data` branch (`python -m worker.publish.push`) | ✅ written; dry run passed 2026-10-02 · ⏳ not scheduled |

A typical healthy run in the worker log:

```text
run run-fast-20261002T101500Z-068e05 done: ok=7 failed=0 stale=4 items=90 new=0 updated=0 dup=90
published 532 files after the fast lane
```

`stale` sources are ones that answered but had nothing new. That is normal; see the runbook's
"Expect sources to go `degraded`" note.

## What's left

1. 🟢 **Push after each run.** The worker writes `data/` on the VM, but nothing pushes it to
   GitHub yet. The push module is built and its dry run passes. It needs calling after each
   publish.
2. 🟢 **Make a push redeploy the site.** `pages.yml` lists `data` under `on: push`, but GitHub
   runs a push-triggered workflow from the workflow file *inside the pushed commit*. The `data`
   branch holds only JSON, so a push to it never starts a deploy. The fix is for `main` to start
   the deploy itself (on a timer, or by a dispatch after each push). It must not involve putting a
   workflow file on `data`, which would need the forbidden `workflow` scope.
3. 🔴 **Approve the merge of `stage-1-foundation` into `main`.** Pages builds from `main`, and
   `main` does not have this work yet.

## How to check it

```bash
cd ~/CyberPulse-AI
docker compose ps worker                                   # "Up"
docker compose logs --since 30m worker | grep -E 'done:|published|ERROR'
```

When the last step is done: the [public site](https://happycode0.github.io/CyberPulse-AI/) shows
real events, with no "AWAITING DATA" banners.

## If it breaks

| Symptom | Fix |
|---|---|
| `password authentication failed for user "cyberpulse"` | `.env` and the database disagree on the password: [4a troubleshooting](stage-4a-paperclip-setup.md#troubleshooting) |
| `docker compose ps` shows nothing | Someone ran `docker compose down`. Run `docker compose up -d` |
| A source shows `degraded` | Often normal (runbook Part 5). Self-healing for this is [Stage 6](stage-6-self-healing.md) |

## Done when

- [x] `docker compose up` collects real events
- [ ] The site is live on GitHub Pages with genuine AU and global intelligence
- [ ] `pytest` green, re-checked before the merge into `main`

---

[← Stage 0](stage-0-prerequisites.md) · [Wiki home](README.md) ·
**Next:** [Stage 2 — Ground truth + enrichment →](stage-2-ground-truth.md)
