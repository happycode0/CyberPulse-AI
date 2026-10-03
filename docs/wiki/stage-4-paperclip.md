# Stage 4 — Paperclip + first agents

[← Stage 3 — Correlation](stage-3-correlation.md) · [Wiki home](README.md) ·
[4a — Paperclip setup →](stage-4a-paperclip-setup.md)

**Status: ▶ in progress — you are here.** Paperclip is running with the whole crew imported, the
AI agents pass their smoke test, and the worker's ops API answers the `http` agents (row 6
below). Every agent and routine stays paused until you resume them.
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
| Company `CyberPulse` created (issue prefix `CYB`) | ✅ |
| Crew imported from the package: 16 agents, 10 routines (paused) | ✅ 2026-10-02 |
| Mission, US$12 company budget, the 11 agent budgets | ✅ 2026-10-03 |
| Smoke test: each AI agent replies, follows the house rules and closes its own ticket | ✅ 9 of 11 on 2026-10-03; TACHIKOMA and RIPPERDOC run when their daily cap resets |
| The worker's ops API on port 8700, compose network only: the five `http` agents' wakes, and token-guarded reads for the AI agents | ✅ 2026-10-03 |

## What's left, in order

| # | Who | Step | Where |
|---|---|---|---|
| 1–2 | ✅ | Claim it; turn sign-up off | [4a steps 2–3](stage-4a-paperclip-setup.md#2--open-it) |
| 3 | ✅ | Import the crew package, then the budgets and the mission | [4a steps 4–7](stage-4a-paperclip-setup.md#4--harden-it--the-six-toggles-runbook-part-6) |
| 4 | ✅ | OpenCode in the container, using the one OpenRouter key through the connection | — |
| 5 | ✅ | Stage 2's money pieces: ledger, price guard, degradation | [Stage 2](stage-2-ground-truth.md#how-it-was-built--all-claude) |
| 6 | ✅ | The worker's ops API and its token; the `http` agents' URL | [below](#the-workers-ops-api) |
| 7 | ✅ | First test tickets, one per AI agent | [4a step 8](stage-4a-paperclip-setup.md#8--first-test--one-ticket-one-agent) |
| 8 | 🔴 **you** | Resume agents and routines: MORPHEUS, ZION and ROGUE first | [4a step 7](stage-4a-paperclip-setup.md) |

MORPHEUS and ZION can do real work now that 5 and 6 are done. Every routine is still paused.
Resume only theirs, and ROGUE's, at first.

**A change from the plan, on purpose:** PLAN.md puts the dashboard behind NetBird. You chose the
home network instead, so it is published on the LAN address only, with login required. NetBird
can be added later ([Stage 7](stage-7-hardening.md)) without changing the agents.

## The worker's ops API

The worker serves it on port 8700 inside its container (`worker/ops_api.py`). Only the Paperclip
server, on the same compose network, can reach it. `docker-compose.yml` publishes no worker
port, and never should: Docker-published ports bypass the host firewall.

**Wakes**: `POST /ops/agents/<callsign>/wake`, for the five `http` agents. The worker already
runs each of their jobs on its own schedule, so a wake runs nothing. It answers **200** when the
job is working and **503** when it is not, and Paperclip marks the run succeeded or failed. The
body says what was checked. A wake needs no token, so the agents' package carries none.

| Agent | Job | 200 means |
|---|---|---|
| ROGUE | `cost-reconcile` | The key's budget can be read, and every ledger call this month has a billed cost. The summary also gives `outside_ledger_usd`: spend on the key that the ledger never saw, which is the Paperclip agents' own calls |
| LIBRARIAN | `groundtruth` | A ground-truth pass completed in the last 7 hours (it runs every 6) |
| SERAPH | `source-verify` | A source was checked in the last 30 minutes; the summary counts each enabled source's latest status and lifecycle state |
| PROWL | `correlation-report` | A collection finished in the last 30 minutes; the summary gives its counts and the merges in the last 24 hours |
| LINK | `publish` | `data/system-status.json` was written in the last 30 minutes |

LIBRARIAN's wake answers 503 until the first ground-truth pass after the deploy has been
recorded. Passes run at :25 past every sixth hour UTC.

**Reads**: `GET /ops/...`, for the AI agents. They need `Authorization: Bearer
$CYBERPULSE_OPS_TOKEN`. The agents inherit that variable, and `CYBERPULSE_OPS_URL`, from the
server's environment. Without the token in `.env`, or with one under 32 characters, the reads
answer 503 and only the wakes work.

| Read | Gives |
|---|---|
| `/ops` | This list |
| `/ops/digest?hours=24` | Event counts (new, archived on arrival, updated, merged, standing), the most prominent events, escalation candidates, source-health changes, runs per lane and this month's cost. This is MORPHEUS's morning read |
| `/ops/events?status=live&severity=&since=&q=&limit=50` | Events, most prominent first |
| `/ops/events/<event_id>` | One event, as the site publishes it |
| `/ops/sources` | Source health, as `source-health.json` |
| `/ops/runs?limit=20` | The latest collection runs |
| `/ops/cost?month=YYYY-MM` | The ledger by stage, model, agent and outcome, and the key's own usage |
| `/ops/jobs` | Every wake's verdict, and the latest ground-truth and enrichment passes |

Every read runs in a read-only transaction with a 15-second limit. Every response, wakes
included, goes through the publisher's secret scan; a response that fails it is withheld with a
500. Paperclip puts a bearer token for its own API in each wake's body. The worker reads only
`job` and the run id from that body, and never logs or stores the rest.

## How to check it

```bash
cd ~/CyberPulse-AI
docker compose ps server worker                             # both "Up … (healthy)"
curl -s http://192.168.128.39:3100/api/health; echo         # "status":"ok"
# A wake, from where Paperclip calls it:
docker compose exec -T server curl -sS -X POST http://worker:8700/ops/agents/link/wake \
  -H 'content-type: application/json' -d '{"job":"publish"}' </dev/null; echo
# A read without the token is refused (401):
docker compose exec -T server curl -s -o /dev/null -w '%{http_code}\n' http://worker:8700/ops/digest </dev/null
# And nothing outside the compose network reaches it (connection refused):
curl -s -m 3 http://192.168.128.39:8700/ops/health || echo "not reachable: good"
```

## Done when

- [ ] Agents are visible in Paperclip, waking on schedule and spending within budget
- [ ] The pipeline survives Paperclip being stopped. Test it: `docker compose stop server`, wait
      for the next 15-minute run, check the worker still logs `done:` and `published`, then
      `docker compose start server`

---

[← Stage 3](stage-3-correlation.md) · [Wiki home](README.md) ·
**Next:** [4a — Paperclip setup →](stage-4a-paperclip-setup.md)
