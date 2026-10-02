# 4a — Paperclip setup: open it, claim it, create the crew

[← Stage 4 — overview](stage-4-paperclip.md) · [Wiki home](README.md) ·
[4b — The crew →](stage-4b-the-crew.md)

The production Paperclip on the VM: one control panel for all 16 agents. Do the steps in order;
each one says who does it (🔴 you · 🟢 Claude).

| Step | | State |
|---|---|---|
| 0 | `.env` settings | ✅ done 2026-10-02 |
| 1 | Start Paperclip | ✅ done 2026-10-02, healthy |
| 2–3 | Open it, claim it | ✅ done 2026-10-02 20:21 Sydney — your account is instance admin, sign-up is off |
| 4–5 | Board approval toggle; company mission and budget | 🔴 **next** — the company `CyberPulse` exists, the rest is not set yet |
| 6–7 | 16 agents, routines | 🔴 straight after |
| 8 | First test ticket | 🔴 once Claude has wired OpenCode ([Stage 4](stage-4-paperclip.md#whats-left-in-order)) |

You need [4b — the crew](stage-4b-the-crew.md) open at step 6.

---

## 0. 🔴 Add the Paperclip settings to `.env` (once)

Paperclip reads its settings from the same `~/CyberPulse-AI/.env` as the rest of the stack. On
2026-10-02 that file had every other key (OpenRouter, Tavily, the publish token) but **no
Paperclip section yet**. This adds it.

The three secrets are generated **on the VM itself**, so they never appear on screen, in a chat
or in git. `.env` is backed up first:

```bash
ssh cyberpulse-vm
cd ~/CyberPulse-AI
cp -p .env .env.bak-$(date +%Y%m%d%H%M%S)
{
  echo
  echo "# ═══ STAGE 4 — Paperclip control plane ═══"
  echo "BETTER_AUTH_SECRET=$(openssl rand -base64 32)"
  echo "PAPERCLIP_AGENT_JWT_SECRET=$(openssl rand -base64 32)"
  echo "PAPERCLIP_TOOL_ACTION_SIGNING_SECRET=$(openssl rand -base64 32)"
  echo "PAPERCLIP_PUBLIC_URL=http://10.0.0.0:3100"
  echo "PAPERCLIP_DEPLOYMENT_MODE=authenticated"
  echo "PAPERCLIP_DEPLOYMENT_EXPOSURE=private"
  echo "PAPERCLIP_AUTH_BASE_URL_MODE=explicit"
  echo "PAPERCLIP_AUTH_DISABLE_SIGN_UP=false"
  echo "PAPERCLIP_TELEMETRY_DISABLED=1"
  echo "PAPERCLIP_ANNOUNCEMENTS_ENABLED=false"
  echo "PAPERCLIP_SECRETS_STRICT_MODE=true"
} >> .env
chmod 600 .env
grep -c '^PAPERCLIP_\|^BETTER_AUTH' .env      # expect 11
```

**Run it once.** Running it again adds duplicate lines, and new secrets would sign everyone out.
To check what is there without showing any value:

```bash
grep -E '^(BETTER_AUTH|PAPERCLIP_)' .env | cut -d= -f1
```

Afterwards the section looks like this (your three secrets are your own random values):

```bash
# ═══ STAGE 4 — Paperclip control plane ═══
BETTER_AUTH_SECRET=<random, generated on the VM>
PAPERCLIP_AGENT_JWT_SECRET=<random, generated on the VM>
PAPERCLIP_TOOL_ACTION_SIGNING_SECRET=<random, generated on the VM>
PAPERCLIP_PUBLIC_URL=http://10.0.0.0:3100
PAPERCLIP_DEPLOYMENT_MODE=authenticated
PAPERCLIP_DEPLOYMENT_EXPOSURE=private
PAPERCLIP_AUTH_BASE_URL_MODE=explicit
PAPERCLIP_AUTH_DISABLE_SIGN_UP=false     # becomes true right after you claim it (step 3)
PAPERCLIP_TELEMETRY_DISABLED=1
PAPERCLIP_ANNOUNCEMENTS_ENABLED=false
PAPERCLIP_SECRETS_STRICT_MODE=true
```

| Setting | What it does |
|---|---|
| `BETTER_AUTH_SECRET` | Signs your login sessions |
| `PAPERCLIP_AGENT_JWT_SECRET` | Signs each agent's identity when it calls Paperclip |
| `PAPERCLIP_TOOL_ACTION_SIGNING_SECRET` | Signs approved agent actions. It has **no fallback**: approvals fail without it |
| `PAPERCLIP_PUBLIC_URL` | The address you open, on the home network. Replaces the NetBird address the template expects |
| `PAPERCLIP_DEPLOYMENT_MODE=authenticated` | Login required. The other mode, `local_trusted`, has no login at all |
| `PAPERCLIP_DEPLOYMENT_EXPOSURE=private` | Treated as a private network, not the internet |
| `PAPERCLIP_AUTH_BASE_URL_MODE=explicit` | Use `PAPERCLIP_PUBLIC_URL` exactly, rather than guessing it from requests |
| `PAPERCLIP_AUTH_DISABLE_SIGN_UP` | `false` until you have claimed it, then `true` so nobody else can sign up |
| `PAPERCLIP_TELEMETRY_DISABLED=1` | Telemetry defaults to **on** upstream |
| `PAPERCLIP_ANNOUNCEMENTS_ENABLED=false` | Announcements phone home by default |
| `PAPERCLIP_SECRETS_STRICT_MODE=true` | Paperclip's stricter handling of secrets in agent configuration. Off by default upstream |

**Back up the secrets with the rest of `.env`.** Lose them and every login and agent identity
has to be redone. Keep `.env` off the repo — it is git-ignored, and the repo is public.

## 1. 🟢 Start Paperclip

**✅ Done 2026-10-02.** Healthy since 20:15 Sydney. This is kept as the record, and as the
commands for a rebuild.

The `server` service in `docker-compose.yml` runs the official image, **pinned to release
2026.1001.0 by digest**, so it only changes when someone bumps it on purpose. The first download
is 1.8 GB (7.3 GB unpacked); on this connection that took about 15 minutes.

**Before it can start, the worker must be able to log in to Postgres.** Paperclip uses the same
`POSTGRES_USER` / `POSTGRES_PASSWORD`. If the worker logs `password authentication failed`, fix
that first ([troubleshooting](#troubleshooting)).

```bash
ssh cyberpulse-vm
cd ~/CyberPulse-AI
git pull

# Once only: Paperclip's own database, next to cyber_intel in the shared Postgres
docker compose exec -T db sh -c 'psql -q -U "$POSTGRES_USER" -d "$POSTGRES_DB"' <<'SQL'
CREATE DATABASE paperclip ENCODING 'UTF8' LC_COLLATE 'C' LC_CTYPE 'C' TEMPLATE template0;
SQL

docker compose up -d server        # the first start downloads the image (a few GB)
docker compose ps server           # wait for "(healthy)" — the first boot sets up its tables
docker compose logs -f server      # Ctrl-c stops watching, not the server
```

It keeps its data in the shared Postgres (the `paperclip` database) and its files in the
`paperclip-data` volume, and it comes back up after a reboot like the rest of the stack. It
listens on **`10.0.0.0:3100` only**: the home network, never `0.0.0.0`, never a router port
forward. It gets only its own settings from `.env`, so it never sees the publish token or the API
keys.

## 2. 🔴 Open it

From any browser **on your home network**:

> **http://10.0.0.0:3100**

Use exactly that address. Login only works at the address in `PAPERCLIP_PUBLIC_URL`, so
`localhost`, an SSH tunnel or the VM's hostname will load the page but fail to sign you in.

From outside the house: not possible, on purpose. NetBird (runbook Part 6) is the way to add that
later, without opening anything to the internet.

## 3. 🔴 Create your account and claim the instance — straight away

**✅ Done 2026-10-02.** Your account was created at 20:21 Sydney and holds `instance_admin`; it is
the only account. Sign-up was then turned off (`PAPERCLIP_AUTH_DISABLE_SIGN_UP=true`); a test
sign-up is now refused with HTTP 400. Kept as the record:

1. **Create account** with your email and a strong password. Use **Create account**, not
   **Sign in**: there are no accounts yet.
2. On the setup screen ("Instance setup required"), click **Claim this instance**. The first
   signed-in account to claim it becomes the instance admin, so do this before anything else.
3. Tell Claude it is claimed. Claude sets `PAPERCLIP_AUTH_DISABLE_SIGN_UP=true` in `.env` and
   restarts the service, so nobody else can create an account.

**If there is no Claim button**, or it fails: the release has a fallback that prints a one-time
admin invite link. Open the link while signed in:

```bash
cd ~/CyberPulse-AI
docker compose exec server pnpm paperclipai auth bootstrap-ceo
```

Check that it worked: `curl -s http://10.0.0.0:3100/api/health` no longer says
`"bootstrapStatus":"bootstrap_pending"`.

## 4. 🔴 Harden it — the five toggles (runbook Part 6)

| Check | Where | Set it to |
|---|---|---|
| **Require board approval for new hires** | Company settings (after step 5) | **on** — it defaults to off, and agents can hire agents. 🔴 **Still off** on 2026-10-02 |
| Sign-up | `.env` `PAPERCLIP_AUTH_DISABLE_SIGN_UP` | `true` (step 3) ✅ |
| Secrets strict mode | `.env` `PAPERCLIP_SECRETS_STRICT_MODE` | `true` (step 0) ✅ |
| Telemetry | `.env` `PAPERCLIP_TELEMETRY_DISABLED` | `1` (step 0) ✅ |
| Announcements | `.env` `PAPERCLIP_ANNOUNCEMENTS_ENABLED` | `false` (step 0) ✅ |

## 5. 🔴 Create the company

**Already started:** on 2026-10-02 the setup screen created a company named `CyberPulse` (issue
prefix `CYB`), but its mission is empty and its monthly budget is 0. Open that company's
settings and fill in the two missing fields below rather than creating a second one.

**New Company** (or the existing company's settings):

| Field | Type this |
|---|---|
| Name | `CyberPulse` |
| Mission | *(below)* |
| Monthly budget | US$12 |

Every agent sees the company mission, so this is the one text that sets the direction for the
whole crew. Paste it whole:

```text
MISSION
Produce the most accurate, evidence-based picture of cyber security, AI security and
AI-related developments, with an Australia-first perspective and global coverage. Monitor and
update continuously, covering Australian and international cyber security news, threat
intelligence, vulnerabilities, incidents, ransomware activity, data breaches, regulatory
changes, emerging threats, AI security research, adversarial AI risks, model vulnerabilities,
AI governance, AI safety, and significant AI industry developments that affect security.

ACCURACY
Accuracy always beats speed. Every claim must be traceable to a primary source. Every
vulnerability, threat or incident severity must be backed by a published score or
authoritative framework: CVSS, CISA Known Exploited Vulnerabilities (KEV), EPSS, MITRE ATT&CK,
vendor advisories, government alerts, or an equivalent recognised standard. Never invent,
estimate or assume missing facts. Any unknown value is written as "Unknown" and is never shown
as zero or as a guessed value. Analytical judgment (for example a suggested ATT&CK technique or
an Australian-relevance assessment) is allowed only when it is clearly labelled as AI
assessment, with its confidence and the evidence it rests on - never presented as fact.

AUSTRALIA FIRST
Prioritise Australian organisations, government agencies, critical infrastructure sectors,
regulatory updates, vendor advisories and threat activity affecting Australia, while also
tracking significant global developments that may affect Australian businesses, government,
cyber defenders and AI practitioners.

IN SCOPE
- Australian cyber security news and incident reporting
- Global cyber security news and threat intelligence
- Vulnerability disclosures and exploit activity
- CISA KEV additions and actively exploited vulnerabilities
- Nation-state and cybercrime activity
- Ransomware, extortion and data breach reporting
- AI security news, research and vulnerabilities
- AI model attacks, jailbreaks, prompt injection techniques and supply-chain risks
- AI governance, regulation, compliance and safety developments
- Major AI company announcements where there is a security, governance or risk implication
- Emerging technologies that materially affect cyber security or AI security

WRITING
Public-facing content is written in clear, concise Australian English, avoiding jargon where
possible while keeping technical accuracy. Distinguish clearly between verified facts, vendor
statements, analyst assessments and community reports. Where sources disagree, present the
disagreement openly and cite every relevant source.

COST
Cost efficiency is a core requirement. Use the lowest-cost approach that achieves the required
quality, accuracy and coverage. Minimise unnecessary computation, API calls and storage. Any
proposed new agent, paid subscription, new vendor, external service or material budget
increase must be approved by the board before it is committed.

SUCCESS IS MEASURED BY
- Accuracy over speed
- Source transparency and traceability
- Australian relevance with global awareness
- Comprehensive cyber security and AI security coverage
- Zero hallucinations and zero fabricated data
- Cost efficiency and operational sustainability
- Actionable intelligence for security leaders, analysts and decision-makers

GOLDEN RULES
1. Never guess.
2. Never hide uncertainty.
3. Cite primary sources whenever available.
4. Mark missing information as "Unknown".
5. Australia first, global by necessity.
6. Accuracy beats speed.
7. Cyber security, AI security and AI developments are all in scope.
8. Spend as little as the mission allows.
9. Board approval is required before hiring agents or people, or increasing recurring costs.
10. Every published insight must be explainable, traceable and defensible.
```

If the **Mission** box has a length limit and cuts this off, put the first paragraph in
**Mission** and paste the whole text into a **New Goal** called `CyberPulse charter`.

Then open **Company settings** and turn on **Require board approval for new hires**.

## 6. 🔴 Create the 16 agents

Go to the agents list → **New Agent** (or **Add Agent**). Do them **in the order on the
[crew page](stage-4b-the-crew.md#the-crew-at-a-glance)**, because **Reports to** needs the manager
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
| Worker or `server` logs `password authentication failed for user "cyberpulse"` | `POSTGRES_PASSWORD` in `.env` was changed after the database was created. Postgres only reads it when the data volume is first set up, so the database still expects the old one | Put the old password back in `.env`, or set the database to the new one (see below). **Never** fix it with `docker compose down -v`: that deletes the database |
| Server log `WARN [Better Auth]: User not found` | Someone used **Sign in** with an email that has no account yet | Use **Create account** first (step 3) |
| `docker compose up -d` sits at `Pulling` for minutes | The first download of the Paperclip image is 1.8 GB | Wait. It is slow, not stuck. Check with `docker compose ps -a`: if `db` and `worker` are gone too, start them on their own with `docker compose up -d --no-deps db worker` |
| `server` stays `(unhealthy)` or keeps restarting | Usually the database: the login above, or the `paperclip` database was never created | `docker compose logs --tail 50 server` shows which |
| `database "paperclip" does not exist` | Step 1's one-off `CREATE DATABASE` was skipped | Run it, then `docker compose restart server` |
| `/api/health` warns `database_backup_missing` | Paperclip backs up its own database every 60 minutes (kept 7 days) into its `paperclip-data` volume; a fresh instance has none yet | Clears after the first hourly backup. Those copies sit on the same single disk, so they are not the real backup — [Stage 7](stage-7-hardening.md#backups--do-this-one-early) is |

**Making the database match a new `POSTGRES_PASSWORD`.** This runs inside the db container and
reads the password from its environment, so the value is never typed or shown:

```bash
cd ~/CyberPulse-AI
docker compose exec -T db sh -c 'psql -q -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v pw="$POSTGRES_PASSWORD" -v u="$POSTGRES_USER"' <<'SQL'
ALTER ROLE :"u" PASSWORD :'pw';
SQL
docker compose restart worker
docker compose logs -f worker      # the next run should log "done: ok=" and "published"
```

### The old test install (from 2026-10-02)

An earlier hand-started copy lives in `~/.paperclip` on the VM. It is not used by the Docker
service and holds nothing worth keeping. To remove it (your call):

```bash
ssh cyberpulse-vm
rm -rf ~/.paperclip ~/paperclip-lab
sudo rm -rf /root/.paperclip
```

---

[← Stage 4 — overview](stage-4-paperclip.md) · [Wiki home](README.md) ·
**Next:** [4b — The crew →](stage-4b-the-crew.md)
