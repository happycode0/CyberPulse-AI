# Stage 4 — Paperclip + first agents

[← Stage 3 — Correlation](stage-3-correlation.md) · [Wiki home](README.md) ·
[4a — Paperclip setup →](stage-4a-paperclip-setup.md)

**Status: ▶ in progress — you are here.** Paperclip is running and claimed; the next step is
yours: the board-approval toggle and the company's mission and budget (4a steps 4–5).
Plan: [PLAN.md §9, Stage 4](../../PLAN.md#9-stages) · Build record:
[runbook Part 6](../vm200-runbook.md)

---

## What this stage gives you

**Paperclip** is the control panel for the AI crew: issues, assignments, schedules, budgets and
approvals for all 16 agents, in one dashboard on your home network. It only adds judgment. The
worker keeps collecting and publishing even with Paperclip stopped.

## This stage's pages, in order

| | Page | Use it for |
|---|---|---|
| 4a | [Paperclip setup](stage-4a-paperclip-setup.md) | The steps: open, claim, harden, company, agents, routines, first test |
| 4b | [The crew](stage-4b-the-crew.md) | What to type for each of the 16 agents. You need it at 4a step 6 |
| 4c | [How the crew works together](stage-4c-how-the-crew-works.md) | How work moves between agents, and what stops runaway cost or code |

## What is done

| Piece | State |
|---|---|
| Paperclip `server` service in `docker-compose.yml`, release 2026.1001.0 pinned by digest | ✅ |
| Listening on `192.168.128.39:3100` only (home network, never `0.0.0.0`) | ✅ checked 2026-10-02 |
| Its own `paperclip` database in the shared Postgres, tables created | ✅ |
| `.env` Paperclip section: 3 secrets and 8 settings | ✅ |
| Health: `ok`, `authenticated` / `private` | ✅ since 2026-10-02 20:15 Sydney |
| Claimed: your account is the only one and holds `instance_admin` | ✅ 2026-10-02 20:21 Sydney |
| Sign-up off (`PAPERCLIP_AUTH_DISABLE_SIGN_UP=true`); a test sign-up is refused | ✅ 2026-10-02 |
| Company `CyberPulse` created (issue prefix `CYB`) | ✅ name only — mission and budget still empty |

## What's left, in order

| # | Who | Step | Where |
|---|---|---|---|
| 1–2 | ✅ | Claim it; turn sign-up off | [4a steps 2–3](stage-4a-paperclip-setup.md#2--open-it) |
| 3 | 🔴 **next** | Import the crew package (16 agents, routines, mission) in one go, then the US$12 budget and the connection settings. Board approval is on | [4a steps 4–7](stage-4a-paperclip-setup.md#4--harden-it--the-six-toggles-runbook-part-6) |
| 4 | 🟢 | OpenCode in the container, using the OpenRouter key | — |
| 5 | 🟢 | Stage 2's money pieces: ledger, price guard, degradation | [Stage 2](stage-2-ground-truth.md#whats-left--all-claude) |
| 6 | 🟢 | The worker's ops API and its token; the `http` agents' URL and auth; max daily runs | — |
| 7 | 🔴 | First test ticket for MORPHEUS | [4a step 8](stage-4a-paperclip-setup.md#8--first-test--one-ticket-one-agent) |

Step 3 works now. MORPHEUS and ZION do real work only after 4–6, which is why only their
routines (and ROGUE's) are switched on at first.

**A change from the plan, on purpose:** PLAN.md puts the dashboard behind NetBird. You chose the
home network instead, so it is published on the LAN address only, with login required. NetBird
can be added later ([Stage 7](stage-7-hardening.md)) without changing the agents.

## How to check it

```bash
cd ~/CyberPulse-AI
docker compose ps server                                    # "Up … (healthy)"
curl -s http://192.168.128.39:3100/api/health; echo         # "status":"ok"
```

## Done when

- [ ] Agents are visible in Paperclip, waking on schedule and spending within budget
- [ ] The pipeline survives Paperclip being stopped. Test it: `docker compose stop server`, wait
      for the next 15-minute run, check the worker still logs `done:` and `published`, then
      `docker compose start server`

---

[← Stage 3](stage-3-correlation.md) · [Wiki home](README.md) ·
**Next:** [4a — Paperclip setup →](stage-4a-paperclip-setup.md)
