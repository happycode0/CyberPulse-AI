# Paperclip setup — open it, claim it, create the crew

The production Paperclip on the VM: one control panel for all 16 agents. The steps are in order;
each one says who does it (🔴 you · 🟢 Claude).

Related pages: [the crew — settings and prompts](paperclip-crew.md) ·
[how the crew works together](how-the-crew-works.md).

---

## What exists today, and what is being built

Be clear on this before you start, because it decides which steps you can do now:

| Piece | Status |
|---|---|
| Worker collecting, scoring, ground truth, building the site | ✅ running on the VM |
| Paperclip as a Docker service (`server`) in `docker-compose.yml` | 🟢 **Stage 4 build — next** |
| OpenCode inside the Paperclip container, using the OpenRouter key | 🟢 Stage 4 build |
| The worker's ops API (the agents read data and the `http` agents call it) | 🟢 Stage 4 build |
| Routines that open `[DIGEST]` / `[INCIDENT]` issues with real data behind them | 🟢 Stage 4 build |

**Steps 1–4 work as soon as the `server` service lands. Steps 5–7 can be typed in straight
after.** The AI agents will not do useful work until the ops API exists, which is why only MORPHEUS
and ZION get their routines switched on at first.

---

## 1. 🟢 Start Paperclip

```bash
ssh cyberpulse-vm
cd ~/CyberPulse-AI
docker compose up -d server
docker compose logs -f server      # wait for "Server listening"; Ctrl-c stops watching, not the server
```

It runs in Docker, keeps its data in the shared Postgres (`paperclip` database), and restarts
itself after a reboot, like the rest of the stack. It listens on **`10.0.0.0:3100` only** —
the home network, never `0.0.0.0`, never a router port forward.

## 2. 🔴 Open it

From any browser **on your home network**:

> **http://10.0.0.0:3100**

From outside the house: not possible, on purpose. NetBird (runbook Part 6) is the way to add that
later, without opening anything to the internet.

From WSL, if the browser cannot reach the LAN address, tunnel it and open http://localhost:3100:

```bash
ssh -N -L 3100:10.0.0.0:3100 cyberpulse-vm
```

## 3. 🔴 Create your account and claim the instance — straight away

1. **Create account** with your email and a strong password.
2. Click **Claim this instance**. The first account to claim it owns it, so do this before
   anything else.
3. Tell Claude it is claimed. Claude sets `PAPERCLIP_AUTH_DISABLE_SIGN_UP=true` in `.env` and
   restarts the service, so nobody else can create an account.

## 4. 🔴 Harden it — the five toggles (runbook Part 6)

| Check | Where | Set it to |
|---|---|---|
| **Require board approval for new hires** | Company settings (after step 5) | **on** — it defaults to off, and agents can hire agents |
| Sign-up | `.env` `PAPERCLIP_AUTH_DISABLE_SIGN_UP` | `true` (step 3) |
| Secrets strict mode | `.env` `PAPERCLIP_SECRETS_STRICT_MODE` | `true` (already) |
| Telemetry | `.env` `PAPERCLIP_TELEMETRY_DISABLED` | `1` (already) |
| Announcements | `.env` `PAPERCLIP_ANNOUNCEMENTS_ENABLED` | `false` (already) |

## 5. 🔴 Create the company

**New Company**:

| Field | Type this |
|---|---|
| Name | `CyberPulse` |
| Mission | *(below)* |
| Monthly budget | US$12 |

```text
Produce an accurate, Australia-first picture of cyber security and AI security, updated
around the clock. Accuracy beats speed: every claim links to a primary source; every severity
comes from a published score (CVSS, CISA KEV, EPSS), never an opinion; anything not known is
written as "unknown", never guessed and never shown as zero. Public copy is plain Australian
English. Spend as little as the work allows, and ask the board before hiring.
```

Then open **Company settings** and turn on **Require board approval for new hires**.

## 6. 🔴 Create the 16 agents

Go to the agents list → **New Agent** (or **Add Agent**). Do them **in the order on the
[crew page](paperclip-crew.md#the-crew-at-a-glance)**, because **Reports to** needs the manager
to exist already.

For each agent, copy from its card on the crew page:

| Form field | What to put |
|---|---|
| Name | the callsign, in capitals: `MORPHEUS` |
| Title | from the card |
| Role | from the card |
| Reports to | from the card (empty for MORPHEUS) |
| Adapter type | `opencode_local` for AI agents, `http` for the zero-token ones |
| Model | from the card (AI agents only) |
| Instructions | the **house rules** block, then a blank line, then the agent's prompt |
| Monthly budget (optional) | from the card |
| Heartbeat on interval | **off** |
| Wake on demand | **on** |

**Max daily runs** and the `http` agents' URL and auth header are environment-specific. If the
form does not offer a field, leave it — Claude sets those through the API in the Stage 4 build,
along with the token values, so no secret is ever typed into a form.

Then **Pause agent** on the nine marked ⏸ on the crew page. They stay on the **Org Chart** but
never wake, so they cost nothing.

Check the **Org Chart** matches this:

```text
MORPHEUS
├── ZION · BLASTER · WINTERMUTE · TACHIKOMA · DECKARD · VOIGHT · SERAPH
├── TELETRAAN
│   ├── WHEELJACK · TRON
│   └── LIBRARIAN · PROWL · LINK
└── ROGUE
    └── RIPPERDOC
```

## 7. 🔴 Create the routines

**Routines → New routine** for each row. **Timezone `Australia/Sydney`** for every one.
**Concurrency policy `skip_if_active`** and **Catch-up policy `skip_missed`** for every one, so a
slow run never stacks up a queue and a reboot never fires a burst of missed jobs.

| Routine | Assigned to | Cron expression | Switch on |
|---|---|---|---|
| Daily editorial | MORPHEUS | `30 7 * * *` | now |
| AU desk digest | ZION | `0 */4 * * *` | now |
| Daily QA sample | VOIGHT | `0 7 * * *` | Stage 5 |
| Global desk digest | BLASTER | `10 */4 * * *` | Stage 5 |
| AI desk digest | WINTERMUTE | `20 */4 * * *` | Stage 5 |
| Follow-up | DECKARD | `45 */6 * * *` | Stage 5 |
| Source discovery | TACHIKOMA | `0 3 * * *` | Stage 5 |
| Model scan | RIPPERDOC | `0 4 * * *` | Stage 5 |
| Model gauntlet | RIPPERDOC | `0 4 * * 0` | Stage 5 |
| Monthly cost review | ROGUE | `0 9 1 * *` | now |

Create the "Stage 5" ones **paused**. Routine text — what goes in the issue it creates:

| Routine | Issue title | Issue body |
|---|---|---|
| Daily editorial | `[EDITORIAL] Daily — {{date}}` | `Run the daily editorial: overnight digest, follow-ups, gaps, VOIGHT verdicts, daily report.` |
| AU desk digest | `[DIGEST] AU desk — {{date}}` | `Review the AU candidate events in the current desk digest.` |
| Global / AI desk digest | `[DIGEST] Global desk` / `[DIGEST] AI desk` | `Review the candidate events for your beat in the current desk digest.` |
| Daily QA sample | `[QA] Daily sample` | `QA a sample of yesterday's published events.` |
| Follow-up | `[FOLLOW-UP] Sweep` | `Check every developing and monitoring event for material change.` |
| Source discovery | `[DISCOVERY] Nightly` | `Run discovery searches, plus any open [GAP] topics.` |
| Model scan / gauntlet | `[MODEL] Daily scan` / `[MODEL] Weekly gauntlet` | `Read the worker's results and report.` |
| Monthly cost review | `[COST] Monthly review` | `Reconcile the month and report to MORPHEUS.` |

If the date placeholder is not accepted, drop it — Paperclip records when each issue was created.

**What is *not* a routine, on purpose:** the 15-minute and 4-hour collection lanes, the
ground-truth sync and publishing. The worker owns those. As Paperclip routines they would create
about 35,000 issues a year of pure noise, and would stop the site whenever Paperclip was down
(PLAN.md §2.3).

## 8. 🔴 First test — one ticket, one agent

1. **New issue**: `Test: introduce yourself`, body
   `Reply in three lines: who you are, what you own, who you hand work to.`,
   **Assignee** `MORPHEUS`.
2. Open MORPHEUS's page and watch the run. It wakes because **Wake on demand** is on.
3. Check the reply follows the prompt, then check the cost on the run. It should be well under
   one cent.

If it fails with "opencode not found" or a model error, the Stage 4 OpenCode piece is not in yet —
tell Claude.

## 9. Day to day

| You want to | Do this |
|---|---|
| See what happened today | MORPHEUS's `[EDITORIAL]` comment |
| Approve a hire or proposal | **Approvals** |
| Stop one agent | its page → **Pause agent** (and **Resume agent** to undo) |
| Stop all AI spend at once | pause the company — the worker keeps collecting and publishing |
| See spend | **Budgets**, plus ROGUE's monthly `[COST]` issue |
| Wake an agent by hand | assign it an issue, or @-mention it in a comment |

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `getaddrinfo ENOTFOUND db` when started by hand with `npx` | Paperclip auto-loads `.env` from the folder it starts in, and `~/CyberPulse-AI/.env` points `DATABASE_URL` at `db`, a name that only exists inside Docker | Don't start it with `npx` from `~/CyberPulse-AI`. Use the Docker service |
| A second instance under `/root/.paperclip` | It was started with `sudo npx` | Never use `sudo` with Paperclip. Remove it with `sudo rm -rf /root/.paperclip` |
| `npm notice` / `EBADENGINE` about npm 12 or Node 22 | npm 12 needs Node 22; the VM has Node 20 | Ignore it. The Docker image brings its own Node |
| An `http` agent times out too early | That adapter reads `timeoutMs`, not the `timeoutSec` its help text shows | Set `timeoutMs` |
| An agent is "Budget paused" | It hit 100% of its monthly budget | Working as designed. Raise it only as the board, on purpose |
| You can't reach :3100 from the phone | The phone is on mobile data or a guest Wi-Fi | Join the home Wi-Fi. The site is not on the internet, by design |

### The old test install (from 2026-10-02)

An earlier hand-started copy lives in `~/.paperclip` on the VM. It is not used by the Docker
service and holds nothing worth keeping. To remove it (your call):

```bash
ssh cyberpulse-vm
rm -rf ~/.paperclip ~/paperclip-lab
sudo rm -rf /root/.paperclip
```
