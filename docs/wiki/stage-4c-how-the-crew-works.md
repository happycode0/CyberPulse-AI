# How the crew works together

[The crew](stage-4b-the-crew.md) · [Wiki home](README.md)

The [crew page](stage-4b-the-crew.md) says what each agent is. This page shows how the work moves
between them — who starts it, who hands it to whom, and where it stops.

---

## The one rule that shapes everything

**Python does the work that never needs judgment. Agents only do the judgment.**

The worker container collects, de-duplicates, scores, fetches ground truth and publishes **on
its own schedule, with no AI and no Paperclip**. If Paperclip is stopped, upgraded or broken,
the site keeps updating every hour (PLAN.md §2.3). Agents add judgment on top: Australian
relevance, campaign links, follow-ups, quality checks.

```text
               ┌──────────────── PAPERCLIP (judgment) ────────────────┐
               │  issues · assignments · routines · budgets · approvals │
               └───────▲───────────────────────────────────┬──────────┘
         run summaries │ digests, incidents        verdicts │ comments, tasks
                       │                                   ▼
┌──────────────────────┴──────── WORKER (no AI) ───────────────────────────┐
│ FAST every hour · NORMAL every 4 h · ground truth every 6 h · publish    │
└──────────────────────┬───────────────────────────────────────────────────┘
                       ▼
          Postgres ──► data/*.json ──► GitHub Pages (the public site)
```

## How agents talk to each other

There is no chat between agents. Everything is a **Paperclip issue**:

- **A routine** (a scheduled job) creates an issue and assigns it — that is what wakes an agent.
- **Handing off** means @-mentioning the next agent in a comment, or creating a new issue
  assigned to them.
- **Every run ends with a comment** saying what was done and who owns the next step (house rule 9).
- **The data** comes from the worker's ops API, never from the database directly.

So the whole history of every decision is in the issue log. You read it the same way the agents do.

**Issue title prefixes**, so you can filter the board:

| Prefix | Created by | Assigned to |
|---|---|---|
| `[EDITORIAL]` | a routine, every morning | MORPHEUS |
| `[FOLLOW-UP]` | a routine the worker fires when tasks with news are due | DECKARD |
| `[DISCOVERY]` | a routine the worker fires when found sites have no feed | TACHIKOMA |
| `[GAP]` | MORPHEUS | TACHIKOMA |
| `[INCIDENT]` | the watchdog's routine, for a kind code can fix | TELETRAAN |
| `[ENGINEERING]` | MORPHEUS, once WHEELJACK has a repository | WHEELJACK |

`[DIGEST]` and `[QA]` issues are gone since 2026-10-30: the desk digest repeated the worker's
own triage and brief, and VOIGHT's verdicts had nowhere to land
([4b](stage-4b-the-crew.md#the-crew-since-2026-10-30)).

---

## The six loops

### 1. Collection loop — every hour, no AI *(running today)*

```text
worker FAST lane ──► correlate: one event or a new one? ──► ground truth: CVSS / KEV / EPSS
                                                                     │
                                       validate, secret-scan, publish ◄┘
```

Nobody in Paperclip has to wake for this. The worker posts a run summary so you can see it.
SERAPH is the one face of it all in Paperclip. Its wake answers 200 when the sources,
correlation, ground truth and publishing all check out, and 503 naming any that do not. It runs
nothing itself.

### 2. Follow-up loop — when the worker says there is news

```text
worker, every hour at :15 ──► due check, nothing new since the last?  ──► records no_change itself
                          └─► tasks with news due, cap has room,
                              6 hours since the last wake?             ──► fires "[FOLLOW-UP] Sweep"
DECKARD ──► GET /ops/followup (one batch) ──► POST one report per task ──► the worker sets the status
                                          └─ new Australian exposure?  ──► MORPHEUS (immediately)
```

Until 2026-10-30 this was the desk loop: DECKARD's notes on each candidate event, three times a
day, and a follow-up sweep four times a day whether or not anything was due. The worker's
triage and brief already say what the notes said, and most sweeps found nothing.

### 3. Editorial loop — every morning, 07:30 Sydney

```text
07:30  MORPHEUS reads the overnight digest and its escalation candidates
         ├─ topic has gone quiet             ──► [GAP]       ──► TACHIKOMA
         └─ writes the daily intelligence report, naming any story to watch more closely
```

MORPHEUS opens no follow-up issues. The worker's queue schedules every check on a developing
story, less often while the checks find nothing, and DECKARD works it when the worker fires
the Follow-up routine: one batch a sweep, at most twenty reports a day.

### 4. Source loop — nightly, from 03:10 Sydney

```text
03:10 worker searches (Tavily) ──┐
TACHIKOMA proposes, when fired ──┴─► candidate ──► SERAPH's gate in the worker, every 4 h (deterministic)
                                               ├─ 6 healthy probes in a row ──► ACTIVE (community evidence)
                                               └─ 3 failures in a row       ──► rejected, with the reason
```

The agent that *finds* a source can never *activate* it. That split is the point.

The gate tests at most 20 candidates and collects at most 25 found sources at once
(`config/discovery.yaml`). A feed TACHIKOMA proposes while 20 are being tested is queued, and
each pass moves the oldest into the slots that free. A candidate ready while 25 are collected
is probed once a day, not every pass, so the new ones get the probes. Each pass retires up to
two of the weakest found sources to make room for it: collected for three weeks or more, with
under 5% of their events reported by another source too.

### 5. Money loop — always on

```text
every AI call ──► the worker's cost ledger (the real billed cost, from OpenRouter)
                    ├─ spend running ahead of plan ──► cheaper degradation tier, BEFORE spending more
                    └─ last month's summary, 11:10 on the 1st ──► Telegram ──► YOU

worker's nightly scan ──► model fails the price guard ──► dropped from its chain at once ──► Telegram
worker's Sunday test  ──► model proposal              ──► Telegram ──► YOU approve or ignore
model failing a Paperclip agent ──► TELETRAAN ──► YOU change that agent's model
```

No agent sits in this loop since RIPPERDOC was retired on 2026-10-20
([4b](stage-4b-the-crew.md#7-ripperdoc-retired-2026-10-20)): it only passed on what the worker had
measured. A proposal carries the worker's own cost figures: every call went through the price
guard, and the cost per event is what OpenRouter billed. Nobody approves their own proposal: the
worker proposes, and you decide.

Three stops are stacked on top of each other, so a runaway agent cannot run up a bill:

1. The agent's own monthly budget. Paperclip pauses it at 100%.
2. The company budget, US$12.
3. The OpenRouter key's hard limit, US$20. Nothing gets past this.

### 6. Repair loop

```text
watchdog (every 5 min) sees a signature ──► incident INC-<n> ──► Telegram (high and critical)
   └─► Incident routine ──► [INCIDENT] ──► TELETRAAN reads GET /ops/incidents, finds the cause
          └─► [ENGINEERING] INC-<n> (structured summary only, no raw content)
                 └─► WHEELJACK: fix/inc-<n>-… ► fix ► regression test ► PR
                        └─► TELETRAAN (AUDIT model, not WHEELJACK's vendor): POST /ops/incidents/<n>/verdict, PASS / FAIL
                               └─► YOU approve and merge ──► the fault clears ──► resolved after 15 min
3 FAIL verdicts ──► circuit breaker: needs_human, verdicts refused (409), everything waits for you
```

The watchdog opens and resolves incidents itself; no agent can close one.
[Watchdog incidents](../runbooks/watchdog-incidents.md) has the signatures and the setup.

---

## Who may do what

The prompt asks nicely. These are the controls that actually enforce it:

| Control | Where it is set | What it stops |
|---|---|---|
| Board approval for new hires | Company settings | An agent hiring more agents |
| Monthly budget per agent | Each agent | One agent spending everyone's money |
| Wake on demand only, no heartbeat | Each agent | Agents waking (and paying) with nothing to do |
| Max daily runs | Each agent | A loop of agents waking each other |
| Ops API token scope | Worker (Stage 4) | An agent writing ground truth, or publishing, through the ops API. Not through the server's own database URL, which `opencode_local` agents inherit: [threat model, risk 1](../threat-model.md#open-risks-ranked) |
| WHEELJACK is given only a branch token | Its environment | Pushing to `main`. Not reading other secrets: it runs with the server's environment ([risk 1](../threat-model.md#open-risks-ranked)) |
| A human merges | GitHub branch protection | Any code reaching production unreviewed |
| The publisher fails closed | Worker | A secret, or unvalidated data, reaching the public site |

## What you (the board) do

- **Approvals inbox** — new hires and budget changes.
- **Telegram** — besides the digest and alerts, the worker's model notices and each month's AI
  cost. A model proposal is yours to decide: promoting it is a reviewed edit to
  `config/models.yaml`, and doing nothing declines it.
- **The daily report** — MORPHEUS's 07:30 comment is the one thing to read each day.
- **`needs human`** — the circuit breaker or VOIGHT is asking you something.
- **Pause** — any agent, any time, from its page. Collection and publishing carry on without it.

---

[The crew](stage-4b-the-crew.md) · [Wiki home](README.md)
