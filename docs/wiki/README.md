# CyberPulse-AI wiki — start here

The owner's guide to building CyberPulse-AI, one stage at a time. Start on this page, then work
through the stages in order: every page links back here and on to the next one.

[`PLAN.md`](../../PLAN.md) is the full design and [`docs/vm200-runbook.md`](../vm200-runbook.md)
is the command-by-command record of how VM 200 was built. This wiki is the "what do I do next"
layer on top of both.

🔴 = you do it (accounts, approvals, anything in a browser) · 🟢 = Claude does it

---

## The stages, in order

| # | Page | What it gives you | Status |
|---|---|---|---|
| 0 | [Prerequisites](stage-0-prerequisites.md) | Accounts, keys, the VM, `.env` | ✅ done |
| 1 | [Foundation: pipeline + site](stage-1-foundation.md) | Collection every 15 min, the public site | ✅ done |
| 2 | [Ground truth + enrichment](stage-2-ground-truth.md) | KEV / CVSS / EPSS / OSV / MITRE, then AI enrichment within budget | ✅ done |
| 3 | [Correlation depth + trends](stage-3-correlation.md) | Smarter de-duplication, trends from real data | ✅ done |
| 4 | [Paperclip + first agents](stage-4-paperclip.md) | The control panel and the 16-agent crew | ▶ **you are here** |
| 4a | ↳ [Paperclip setup](stage-4a-paperclip-setup.md) | Open it, claim it, harden it, create the company, agents and routines | ▶ claimed; step 4 next |
| 4b | ↳ [The crew](stage-4b-the-crew.md) | All 16 agents: settings and the prompt to paste | reference |
| 4c | ↳ [How the crew works together](stage-4c-how-the-crew-works.md) | Who wakes whom, hand-offs, where money and code are stopped | reference |
| 5 | [Full crew + notifications](stage-5-full-crew.md) | Six more agents switched on, source discovery, Telegram | ▶ Telegram, THE CREW, follow-up and source discovery built |
| 6 | [Self-healing](stage-6-self-healing.md) | Agents that fix broken sources, behind your approval | ⬜ |
| 7 | [Hardening + operations](stage-7-hardening.md) | Backups, restore rehearsal, failure testing | ⬜ |

## You are here

```text
Stage 0  Prerequisites ............................ ✅ done
Stage 1  Foundation: pipeline + site .............. 🟡 real events on the site; redeploy timer is yours
Stage 2  Ground truth + enrichment ................ ✅ done
Stage 3  Correlation depth + trends ............... ✅ done
Stage 4  Paperclip + first agents ................. ▶ YOU ARE HERE
           ├─ Paperclip Docker service ............ ✅ running, healthy, since 2026-10-02 20:15 Sydney
           ├─ open, claim, sign-up off ............ ✅ claimed 2026-10-02 20:21 Sydney
           ├─ board-approval toggle, company ...... ✅ mission and budgets set 2026-10-03
           ├─ 16 agents + routines ................ ✅ imported, smoke-tested; all paused
           ├─ OpenCode, ops API, http agents ...... ✅ 2026-10-03
           └─ resume agents and routines .......... 🔴 you: MORPHEUS, ZION, ROGUE first
Stage 5  Full crew + notifications ................ ▶ building
           ├─ Telegram digest + critical alerts ... ✅ built; 🔴 you: the bot token
           └─ THE CREW workload (crew.json) ....... ✅ built
Stage 6  Self-healing ............................. ⬜
Stage 7  Hardening + operations ................... ⬜
```

## What is yours to do (🔴), in order

1. ~~**Claim Paperclip.**~~ ✅ Done 2026-10-02; sign-up is now off.
2. ~~**Set it up.**~~ ✅ Company, mission, budgets, 16 agents and their smoke tests, 2026-10-03.
   **Then resume** MORPHEUS, ZION and ROGUE and their routines when you are ready for them to
   spend ([Stage 4](stage-4-paperclip.md#whats-left-in-order)).
3. ~~**Approve merging `stage-1-foundation` into `main`.**~~ ✅ Merged 2026-10-03 (PR #1).
   **Then commit the Pages timer**, so the site redeploys every 15 minutes on its own
   ([Stage 1, item 2](stage-1-foundation.md#whats-left)).
4. **Provide an off-host backup target** (NAS, USB disk or PBS). The host has a single disk
   ([Stage 7](stage-7-hardening.md)).
5. **A Telegram bot** for the daily digest and alerts ([Stage 5, Telegram](stage-5-full-crew.md#telegram)).
   Later, NetBird if you want the dashboard from outside the house.
6. **A Tavily key** for the nightly source search, and TACHIKOMA's new instructions
   ([Stage 5, Source discovery](stage-5-full-crew.md#source-discovery)).

## Where things live

| Thing | Where | Notes |
|---|---|---|
| Repo (your checkout) | `C:\Users\d739962\HappyCode\CyberPulse-AI` | `main`; each stage's work lands by pull request |
| VM 200 | `ssh cyberpulse-vm` → `oxygen@192.168.128.39` | Debian 13, 4 vCPU / 12 GB / 60 GB |
| SSH key | WSL `~/.ssh/cyberpulse_vm_ed25519` | no passphrase, no backup |
| The stack | VM `~/CyberPulse-AI` | `docker compose ps` → `db`, `worker`, `server` |
| Secrets | VM `~/CyberPulse-AI/.env` | git-ignored, never committed: the repo is public |
| Paperclip dashboard | http://192.168.128.39:3100 | home network only, login required |
| Public site | https://happycode0.github.io/CyberPulse-AI/ | real events; redeploys every 15 minutes |

## Everyday commands (on the VM)

```bash
cd ~/CyberPulse-AI
docker compose ps                         # db, worker, server: all "Up … (healthy)"
docker compose logs -f worker             # collection: "done: ok=" every 15 min
docker compose logs -f server             # Paperclip
docker compose up -d                      # start whatever is stopped
```

**Never run `docker compose down -v`.** The `-v` deletes the database. A plain `docker compose down`
removes the containers, so collection stops until the next `up -d`.

---

**Next:** [Stage 0 — Prerequisites →](stage-0-prerequisites.md)
