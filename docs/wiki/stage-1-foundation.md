# Stage 1 — Foundation: pipeline + site

[← Stage 0 — Prerequisites](stage-0-prerequisites.md) · [Wiki home](README.md) ·
[Stage 2 — Ground truth →](stage-2-ground-truth.md)

**Status: ✅ done (2026-10-03).** The site serves real events. The VM collects, builds the site's
data and pushes it to the `data` branch every hour, and a timer on `main` redeploys the site. The
timer runs every 15 minutes; it should run hourly, a few minutes after the push (item 2). Plan: [PLAN.md §9, Stage 1](../../PLAN.md#9-stages)
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
| FAST lane every hour at :00 UTC (11 sources; every 15 min until 2026-10-04) · NORMAL lane every 4 h | ✅ running on VM 200 |
| Publisher: builds `data/*.json`, validates against the schemas, secret-scans, **fails closed** | ✅ 532 files at the 2026-10-02 20:15 run |
| The site (`site/`): homepage, event page, history | ✅ on GitHub Pages with real events since 2026-10-03 (`main` merged in PR #1) |
| Push of `data/` to the `data` branch, after every scheduled publish | ✅ (2026-10-03) |

A typical healthy run in the worker log:

```text
run run-fast-20261002T101500Z-068e05 done: ok=7 failed=0 stale=4 items=90 new=0 updated=0 dup=90
published 532 files after the fast lane
pushed 6efd49532a1680134ae6cd22c380695bec33d10a to the data branch
```

The push needs `CYBERPULSE_PUBLISH_TOKEN` in `.env` (contents: write on this repo, nothing
more). Without one the worker logs `no publish token` at start and only writes `data/`. A git
command that hangs is stopped after three minutes, and the next publish pushes again.

`stale` sources are ones that answered but had nothing new. That is normal; see the runbook's
"Expect sources to go `degraded`" note.

## What's left

1. ✅ **Push after each run** (2026-10-03).
2. ✅ **Make a push redeploy the site** (2026-10-03, PR #3). GitHub runs a push-triggered
   workflow from the workflow file *inside the pushed commit*, and the `data` branch holds only
   JSON, so a push to it never starts a deploy. `pages.yml` on `main` now also has
   `schedule: - cron: "*/15 * * * *"`. Since the FAST lane went hourly (2026-10-04), it should
   be `- cron: "12 * * * *"`: a few minutes after the :00 push, and off the top of the hour,
   when GitHub delays schedules most. That change is the owner's to commit, since it needs the
   `workflow` scope (see the [runbook](../vm200-runbook.md), Part 5e). GitHub runs schedules only from the default branch, may
   start them late when it is busy, and turns them off after 60 days without a commit to the
   repo. If that happens, the **Run workflow** button on the Actions tab's `pages` workflow
   redeploys by hand, and any commit to `main` turns the timer back on. No workflow file goes on
   `data`: that would need the forbidden `workflow` scope.
3. ✅ **Merge `stage-1-foundation` into `main`** (2026-10-03, PR #1).

## How to check it

```bash
cd ~/CyberPulse-AI
docker compose ps worker                                   # "Up"
docker compose logs --since 30m worker | grep -E 'done:|published|pushed|ERROR'
```

The [public site](https://happycode0.github.io/CyberPulse-AI/) follows the VM without anyone
touching it. With the timer at :12, each hourly push is live by about a quarter past. To see the
timer at work:

```bash
gh run list --workflow pages --limit 5    # "schedule" runs: every 15 minutes, or hourly at :12
```

## If it breaks

| Symptom | Fix |
|---|---|
| `password authentication failed for user "cyberpulse"` | `.env` and the database disagree on the password: [4a troubleshooting](stage-4a-paperclip-setup.md#troubleshooting) |
| `docker compose ps` shows nothing | Someone ran `docker compose down`. Run `docker compose up -d` |
| A source shows `degraded` | Often normal (runbook Part 5). Self-healing for this is [Stage 6](stage-6-self-healing.md) |

## Done when

- [x] `docker compose up` collects real events
- [x] The site is live on GitHub Pages with genuine AU and global intelligence
- [x] `pytest` green, re-checked before the merge into `main`

---

[← Stage 0](stage-0-prerequisites.md) · [Wiki home](README.md) ·
**Next:** [Stage 2 — Ground truth + enrichment →](stage-2-ground-truth.md)
