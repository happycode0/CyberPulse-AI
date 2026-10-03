# Threat model

[Runbooks](runbooks/README.md) · [Wiki home](wiki/README.md) ·
[Stage 7 — Hardening](wiki/stage-7-hardening.md)

**Reviewed 2026-10-03 against `main` at `13ecc2c9` (Stage 6 merged).** This page covers what the
code and the VM actually do, not what the plan meant them to do. Where the two differ, it says so.

Each boundary below has a table of the threats that are real here, sorted by STRIDE: **S**poofing,
**T**ampering, **R**epudiation, **I**nformation disclosure, **D**enial of service, **E**levation
of privilege. Then the risk that is left, and what you do about it. The page ends with the
[open risks, ranked](#open-risks-ranked).

Not covered: Paperclip's own code, the Proxmox host, the router, and GitHub, OpenRouter, Tavily
and Telegram as services.

---

## The system in one picture

```text
  feeds, registers, the open web (untrusted)
        │
        ▼
  ┌────────────── VM 200 (Debian, docker compose; one 94 GB disk on the host) ─────────────┐
  │                                                                                        │
  │  worker ── reads all of .env, mounts the checkout read-write                            │
  │    │  ├──► OpenRouter, Tavily, NVD (API keys)                                           │
  │    │  ├──► GitHub `data` branch (publish token) ──► Pages ──► the public site           │
  │    │  └──► Telegram (bot token)                                                         │
  │    │                                                                                    │
  │    │ :8700 ops API, compose network only (ops token; wakes need none)                   │
  │    ▼                                                                                    │
  │  server (Paperclip) ── published on 192.168.128.39:3100 only ◄── the home LAN           │
  │    └─ 11 opencode_local agents run inside this container, with its environment          │
  │                                                                                        │
  │  db (Postgres 17): cyber_intel, paperclip. One login, `cyberpulse`, a superuser,        │
  │                    used by both the worker and Paperclip                                │
  └────────────────────────────────────────────────────────────────────────────────────────┘
        WHEELJACK ──► GitHub pull requests (engineer token), when Stage 6 runs
        backups ──► none off-host yet
```

---

## 1. Assets

| Asset | Where it lives | Why it matters |
|---|---|---|
| The published intelligence | The `data` branch, served by GitHub Pages | People act on it. Wrong or planted content is the worst harm anyone outside sees |
| The code | `main` on GitHub, and the VM's checkout at `~/CyberPulse-AI` | The worker runs whatever the checkout holds: it is mounted at `/app` (docker-compose.yml:26) |
| `cyber_intel` | The `pgdata` volume | Events, ground truth, the cost ledger, incidents, TELETRAAN's verdicts and the circuit breaker |
| The raw cache | The `rawcache` volume | The bytes each event came from. Most can be fetched again; old ones cannot |
| Paperclip's state | The `paperclip` database, the `paperclip-data` volume and its secrets folder | Agents, issues, approvals, budgets, and the OpenRouter connection the agents use |
| Money | The OpenRouter key, US$20 a month hard limit | Shared by the worker and every AI agent |
| Credentials | `.env` on the VM (mode 600), every backup, Paperclip's stored secrets | [Below](#credentials-names-only) |
| The VM | Login `oxygen`: key-only, in the `docker` group, passwordless sudo (vm200-runbook.md:26) | `oxygen` is root in practice. Whatever runs as `oxygen` owns everything above |

### Credentials (names only)

| Name | Held by | What it opens |
|---|---|---|
| `POSTGRES_USER`, `POSTGRES_PASSWORD` | db; the worker (`DATABASE_URL`); the server (docker-compose.yml:53) | The bootstrap superuser, on both databases. A superuser can also run programs inside the db container (`COPY … TO PROGRAM`) |
| `CYBERPULSE_PUBLISH_TOKEN` | worker | Contents read and write on this repository: every branch that protection does not stop |
| `CYBERPULSE_ENGINEER_TOKEN` | Meant for WHEELJACK only. Kept in `.env`, so the worker holds it too | Contents and pull requests, read and write |
| `CYBERPULSE_OPS_TOKEN` | worker; server (docker-compose.yml:73), so every AI agent | Every ops API read, and all three writes |
| `OPENROUTER_API_KEY` | worker; the same key in Paperclip's OpenRouter connection (stage-4a-paperclip-setup.md:347) | Model spend, up to the key's limit |
| `TAVILY_API_KEY`, `NVD_API_KEY` | worker | Free-tier quotas |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | worker | Posting as the bot in your chat |
| `PAPERCLIP_INCIDENT_WEBHOOK_URL`, `_SECRET` | worker | Firing the Incident routine, which opens a TELETRAAN issue |
| `BETTER_AUTH_SECRET`, `PAPERCLIP_AGENT_JWT_SECRET`, `PAPERCLIP_TOOL_ACTION_SIGNING_SECRET` | server (docker-compose.yml:56-58); the worker through `env_file` | Signing Paperclip's sessions, agent tokens and tool actions |
| Paperclip's secrets folder | `/paperclip/instances/default/secrets`, inside `HOME=/paperclip` | The keys Paperclip signs and encrypts with |
| SSH key `cyberpulse_vm_ed25519` | The WSL box | `oxygen` on the VM |

The worker reads the whole `.env` (docker-compose.yml:21), so it holds every credential in this
table, Paperclip's included. The server gets an explicit list (docker-compose.yml:51-73).

Since 2026-10-03 a `.dockerignore` leaves `.env`, `.env.*` and `.git` out of the worker image
(`COPY . .`, Dockerfile.worker:16). Images built before that still hold a copy of `.env` as it was
at their build: run `docker image prune` after the next build, and after any rotation.

---

## 2. Trust boundaries

### B1. The public site on GitHub Pages

Anyone on the internet reads the HTML and scripts from `main` and the JSON from `data`.

| | Threat | Control (where) |
|---|---|---|
| T, E | Feed text runs as script on the page | A strict CSP: `default-src 'none'; script-src 'self'`, no third-party origins (site/index.html:6, and the same in event.html and history.html). Every string from the data is written with `textContent` or `setAttribute` (site/assets/hud.js:2, site/assets/map.js:8). Links pass `safeUrl`, http and https only (site/assets/hud.js:123), and open with `rel="noopener noreferrer nofollow"` (hud.js:492, 592, 671) |
| I | A secret in the published JSON | The publisher's secret scan fails closed (worker/publish/validate.py): credential patterns (validate.py:42-61), high-entropy tokens, and the literal value of every variable in `.env`. If `.env` exists but cannot be read, nothing publishes (validate.py:138-160) |
| T | The data is replaced | Only through the `data` branch: [B4](#b4-worker-to-the-github-data-branch-publish-token) |
| D | Pages is down, or GitHub stops the schedule | `site-stale` opens within 3 hours. Nothing to fix on this side |

**Residual risk: low.** **You:** nothing. In review, refuse any pull request that loosens the CSP,
adds `innerHTML`, or loads a script from another origin.

### B2. Untrusted feed content into the worker and into model prompts

Every feed, register and page the worker reads is someone else's text. Some of it reaches a model.

| | Threat | Control (where) |
|---|---|---|
| T | Prompt injection: feed text steers enrichment | The record goes in the user message as JSON, never in the system prompt, and no tools are offered (worker/ai/tasks.py:14-26). The answer must fit a strict JSON schema (worker/ai/client.py:207-208). Then it is checked again: no CVE the record does not name, no URL, no run of copied words, nothing shaped like a credential (tasks.py:17-22). A model's severity is labelled `ai_estimate`, and an official score replaces it. The worst case is one misleading summary or severity |
| T | Prompt injection: text an agent reads steers the agent | What an agent writes back is checked as hostile: follow-up reports (worker/pipeline/followup.py:1-20) and source proposals (worker/discovery/gate.py). **Nothing in this repo limits what an `opencode_local` agent can run.** It can run a shell inside the server container: [B5](#b5-paperclip-its-agents-and-the-ops-api-ops-token) |
| E | A URL from the open web reaches the private network (SSRF) | Discovered URLs pass a guard: https on port 443, a host name that resolves to public addresses only, each redirect checked, TLS verified (worker/discovery/guard.py:1-10) |
| D | A huge or endless response | Discovered sites: the body is capped as it arrives, after decompression (guard.py:9-10, 29). Curated sources: 30-second timeouts, but the whole body is read before the 32 MiB cap applies (worker/collectors/http.py:126-146), and a gzip body is unpacked in full first. The worker has no memory limit in compose |
| E | A parser bug gives code execution in the worker | Parsing runs in the worker, which holds every credential and mounts the checkout read-write (docker-compose.yml:21, 26). The container user is UID 1000 (Dockerfile.worker:4, 30). If `oxygen` is UID 1000 too, as on a default Debian install, the container can change the checkout's scripts and `.git/hooks`, and you run those later as `oxygen` |

**Residual risk: medium,** almost all of it from the agents in B5 and the read-write mount. Prompt
injection into enrichment is contained. **You:** check `id -u` on the VM. If it prints 1000, the
checkout mount is a path from the worker to `oxygen`. Do not run `git` hooks you did not write:
`ls .git/hooks` should show only the `*.sample` files. For the lead: mount the checkout read-only
except for `data/`, and stream the collector's body with a cap, as the discovery guard does.

### B3. Worker to OpenRouter and Tavily

| | Threat | Control (where) |
|---|---|---|
| I | A key leaks through logs, errors or the site | Keys are `SecretStr` and redacted in `Settings.__repr__` (worker/settings.py:66-99). Every publish and every ops API response passes the secret scan |
| D, E | Runaway spend: a loop, a price change, a stolen key | Stops, from inside out: the worker's own budget modes, read from OpenRouter's account of the key before each spend (worker/ai/budget.py); each agent's monthly budget; the company budget, US$12; and the key's hard limit, US$20 a month (.env.example:19-26). `cost-anomaly` opens at 3 times a usual day or 90% of the month, but it sums only the worker's own `cost_ledger` (worker/db/watchdog.py:162-180). Agent spend on the shared key never trips it. It does push the worker's budget mode down, because that is read from the key's total |
| T | A model answers with something other than it was asked | The strict schema and the checks in B2 |
| I | The provider sees what is sent | Only public feed content is sent |

The **OpenRouter key's hard limit is the last stop.** Everything inside it is this system's own
accounting. The same key sits in Paperclip's OpenRouter connection, so any agent that can read
Paperclip's stored secrets ([B5](#b5-paperclip-its-agents-and-the-ops-api-ops-token)) can spend it, with no
agent or company budget in the way. The worker reads the key's total spend, so the agents' spend
also pushes the worker towards `free_only`.

**Residual risk: medium.** The most it costs is US$20 a month. **You:** never raise the key's
limit to clear a symptom. No management key, ever (.env.example:24-25). If spend looks wrong,
rotate the key: [tokens](runbooks/tokens.md).

### B4. Worker to the GitHub data branch (publish token)

| | Threat | Control (where) |
|---|---|---|
| I | The token leaks into git config, logs or errors | It is passed per command as `http.extraheader`, never written to `.git/config` (worker/publish/push.py:153-161), and redacted from git's output (push.py:259). While a push runs it does appear in the container's process list (the `-c` argument). Only something already inside the worker can see that |
| T | Bad data is published | Schemas and the secret scan run before every push, and fail closed (validate.py). The push is an orphan commit, force-pushed (push.py:115-125) |
| T, E | The token writes somewhere other than `data` | The branch name is checked for shape only (push.py:40, 75). `python -m worker.publish.push --branch main` would force-push the data over `main`. Only branch protection stops that, and only if it binds the token: [B6](#b6-wheeljack-to-github-engineer-token-and-branch-protection) |
| T | The `data` branch carries more than JSON | Pages copies the whole branch into the site, `cp -R data/. site/data/` (.github/workflows/pages.yml:63-67). Whoever holds either token can put an HTML page on the site's own address |
| E | The token gains workflow rights | Today it has Contents and Metadata only (.env.example:39-45). With the **Workflows** permission it could change `.github/workflows/`, so it could change what Pages builds and what any job runs |

**Residual risk: medium.** **You:** the publish token must **never** get the Workflows permission,
or Administration. Keep a rule on `main` that blocks force pushes and deletion for everyone, admins
included. Check what the site serves: `https://happycode0.github.io/CyberPulse-AI/data/` should hold
only `.json` files, and `…/data/.git/config` should be a 404. For the lead: copy only `*.json` in
pages.yml, set `persist-credentials: false` on both checkouts, and let `push.py` refuse any branch
but `data`.

### B5. Paperclip, its agents and the ops API (ops token)

**This is the largest open risk.**

**Observed:** Paperclip's `opencode_local` agents run inside the `server` container and inherit its
environment. That environment holds `DATABASE_URL`, which logs in as `cyberpulse`, the Postgres
superuser (docker-compose.yml:53). It holds `BETTER_AUTH_SECRET` and the two other signing secrets
(docker-compose.yml:56-58), and the ops token (docker-compose.yml:72-73). The agents' home,
`/paperclip`, holds the secrets folder (PLAN.md §2.2).

So one agent run, whether prompt-injected or simply wrong, can:

- connect to `cyber_intel` as the superuser and change anything in it: events and ground truth
  (which the worker then publishes), the cost ledger the watchdog reads, and the incidents, verdicts
  and breaker. "No agent can close an incident" holds only through the ops API;
- change Paperclip's own state: approvals, budgets, which agents are paused;
- run a program inside the db container, as the superuser can;
- sign sessions and agent tokens with Paperclip's secrets, and act as the board inside Paperclip;
- read the secrets folder and Paperclip's database together, and so what Paperclip stores, such as
  the OpenRouter connection. PLAN.md §2.2 already says an unsandboxed agent there "can read every
  company secret";
- use the ops token for all three writes.

It cannot reach the publish token or the worker's other keys directly: the server's environment
does not hold them. It can still change what the site says, by changing `cyber_intel`.

Three pages said otherwise until 2026-10-03, and now point here: 4b's WHEELJACK card ("**Only**
`CYBERPULSE_ENGINEER_TOKEN`"), 4c's "Who may do what" table (the ops token's scope stops "a desk
writing ground truth") and PLAN.md §2.2, which chose `opencode_local`, a local adapter too.

| | Threat | Control (where) |
|---|---|---|
| E | An agent uses the server's environment | **None in this repo.** The agents that run models are paused, except those you have resumed. Since the move to 8 agents, 4b keeps every agent paused until you choose to resume it |
| S | Something other than an agent calls the ops API | Only the compose network can reach port 8700. No port is published (docker-compose.yml:28-29). The bearer token must be at least 32 characters and is compared in constant time (worker/ops_api.py:676-692) |
| T | A hostile write through the ops API | Three writes, each checked as hostile: follow-up reports (ops_api.py:1283), source proposals (ops_api.py:1355) and TELETRAAN's verdicts (ops_api.py:1204). Reads run in read-only transactions with a 15-second statement timeout (ops_api.py:695). One token opens all three writes, so any agent can write as DECKARD, TACHIKOMA or TELETRAAN (ops_api.py:641-655). A false FAIL only trips the breaker, which stops the crew and waits for you. A false PASS changes nothing, because you merge. Follow-up entries reach the public timeline, labelled as the agent's, at most 8 per event |
| I | A response leaks a secret | Every response passes the secret scan, or is withheld (ops_api.py:594-607) |
| D | The API is flooded | 4 requests at once, bodies up to 1 MiB, chunked bodies refused, 15-second socket and body deadlines (ops_api.py:127-133, 1431-1441) |
| S | A wake is forged | Wakes need no token, but run nothing and change nothing: they report whether a job is healthy (ops_api.py:1-12) |
| R | Who did what | Paperclip records each agent run. The ops API logs paths, never bodies or tokens (ops_api.py:23-25). Every agent shares one token, so the API cannot tell them apart |

One comment is out of date and understates the reach: docker-compose.yml:70-71 says "The token
opens reads only; nothing there writes". There are three writes: follow-up reports, source
proposals and TELETRAAN's verdicts (worker/ops_api.py, corrected 2026-10-03).

**Residual risk: critical while any `opencode_local` agent runs.**

**You, now:**

1. **Keep the AI agents paused** unless you are watching them. In Paperclip, pause every
   `opencode_local` agent. SERAPH, the one `http` agent, runs no model, so this risk does not
   need it paused.
2. **Give Paperclip its own database role, with least privilege.** This is a database change, so
   take a backup first ([backup and restore](runbooks/backup-and-restore.md)) and try it on a
   restored copy before production. In outline: a new login role that is not a superuser and
   cannot create roles or databases; the `paperclip` database and every object in it owned by that
   role; `CONNECT` on `cyber_intel` revoked from `PUBLIC`, so only `cyberpulse` connects there; a
   new password variable in `.env`; and docker-compose.yml:53 changed to use it. If a Paperclip
   upgrade needs an extension only a superuser can create, create it once by hand. This closes
   `cyber_intel` and running programs in the db container. It does not close the signing secrets or
   the secrets folder.
3. **Raise it upstream with Paperclip:** local adapters should get an allow-listed environment and
   a home that is not the instance directory, or run in their own container.

**For the lead:** an ops token per role (reads, and each write), so the server's environment holds
only what its agents need. Later, a least-privilege role for the worker too: it is the superuser
as well.

### B6. WHEELJACK to GitHub (engineer token and branch protection)

Not live yet: WHEELJACK is paused, TELETRAAN's pull request checks wait for Stage 6
(stage-4b-the-crew.md:40, 46), and the token is still to be made (stage-6-self-healing.md:31).

| | Threat | Control (where) |
|---|---|---|
| E | The token pushes to `main` without review | Branch protection on `main`, still to be set (stage-6-self-healing.md:32). **A fine-grained token acts as your own account.** GitHub does not let a pull request's author approve it, and WHEELJACK's pull requests would be yours. So a required review either blocks every one of them, or you merge by bypassing the rule, and a rule you can bypass the token can bypass too |
| T | The token rewrites `data` or other branches | Contents write covers every branch the rules do not protect, `data` included |
| T | A fix that hides a backdoor | TELETRAAN reviews it on a different model vendor, and you merge. No CI runs the tests on a pull request (.github/workflows/ holds only pages.yml), so there is no status check to require. TELETRAAN runs the tests itself, inside the server container: a pull request's code runs there, with the environment in B5, before you have seen it |
| T | A dependency changes under a merged fix | requirements.txt sets lower bounds only, with no lock file or hashes. A rebuild takes whatever PyPI has that day |
| I | The token leaks | Wherever it is stored, any agent in the server container can reach it (B5). Its scope is the real control |

TELETRAAN has no repository token, and needs none: the repository is public.

**Residual risk: high once WHEELJACK runs.** **You, before you resume WHEELJACK, or let TELETRAAN
check a pull request:** give WHEELJACK an identity of its own, a GitHub App or a machine account
with the Write role, never Admin (README §0.5 already suggests a GitHub App for Stage 7). Then
set a ruleset on `main` with no bypass for anyone: pull request required, one approval, no force
push, no deletion. Fix B5 first, because TELETRAAN runs pull request code inside that container.
**For the lead:** a CI workflow that runs the tests on pull requests with
`permissions: contents: read`, and a hashed lock file.

### B7. The LAN

| | Threat | Control (where) |
|---|---|---|
| S, I | Someone on the home network reads or replays a Paperclip login | Paperclip is published on `192.168.128.39:3100` only (docker-compose.yml:45-48), in `authenticated` and `private` mode, with sign-up off (vm200-runbook.md:590-594). It is plain HTTP, so passwords and session cookies cross the LAN unencrypted |
| E | Another device joins the company | **Connection requests → Human only** is still unset (vm200-runbook.md:611) |
| E | A port is opened wider than meant | Published Docker ports bypass `ufw`, so the bind address is the only limit. The worker and db publish none (docker-compose.yml:28-29). If the VM's DHCP address changes, the bind fails and `server` stops: it fails closed, it does not widen |
| E | SSH | Key-only as `oxygen`, who has passwordless sudo and the docker group (vm200-runbook.md:26). The key lives on the WSL box. The laptop's own `.env` on `/mnt/c` is world-readable, but it is not the VM's: the VM's credentials were made fresh (vm200-runbook.md:224-229) |

The bind in compose is the truth: Paperclip listens on the VM's LAN address only.

**Residual risk: medium,** for a home network with guests or smart devices on it. **You:** set
**Connection requests → Human only** (company settings). Give the VM a DHCP reservation on the
router. Keep smart devices and guests on a separate network. NetBird later, for access from
outside, with nothing forwarded on the router. Check the bind now and then:
`ss -ltn | grep 3100` should show only `192.168.128.39:3100`.

### B8. Backups on a single-disk host

| | Threat | Control (where) |
|---|---|---|
| D | The disk fails and takes everything | **Nothing yet.** The Proxmox host has one 94 GB disk (vm200-runbook.md:636). Paperclip's hourly dumps sit in the `paperclip-data` volume on that disk (stage-7-hardening.md:40-42) |
| I | A backup leaks | Each `ops/backup.sh` backup holds `env` and Paperclip's secrets folder, so it is every credential in one place. It is written with `umask 077`. `~/env-backup-20261002-signup` is another full copy of `.env`, still on the VM (vm200-runbook.md:592) |
| T | A backup is damaged or partial | `SHA256SUMS` and a `COMPLETE` marker, written last. `ops/restore.sh` checks both before it restores anything |
| D | A backup that has never been restored | `ops/restore.sh rehearse` restores into a throwaway container and compares row counts. Rehearsed on VM 200 on 2026-10-03: both databases, every row count matched (stage-7-hardening.md) |
| D | A scheduled backup fails, or lands on the VM's own disk, and nobody notices | **Nothing automatic.** The watchdog does not watch backups. `ops/backup.sh` refuses a target that does not exist, but an unmounted mount point exists |
| I | Secrets outlive a rotation in the worker image | `.dockerignore` (since 2026-10-03) keeps `.env` out of new builds. Images built before it hold `.env` as it was then. `vzdump` of the VM carries those layers. A `docker save` or a push to a registry would carry them off the host |

**Residual risk: high** until there is an off-host target and one rehearsed restore. **You:** set
up the off-host target and the `vzdump` job (stage-7-hardening.md:22-23). Schedule `ops/backup.sh`.
Rehearse once a month. Keep the target private, and encrypted if you can: it holds every key.
Delete `~/env-backup-20261002-signup` once a scheduled backup has run. Never `docker save` or push
the worker image. After a rotation, remove old worker images with `docker image prune`.
[Backup and restore](runbooks/backup-and-restore.md) has the steps.

### B9. The public repository

| | Threat | Control (where) |
|---|---|---|
| I | `.env` or a key is committed | .gitignore keeps out `.env`, `.env.*` (except `.env.example`), key files, `master.key`, `secrets/`, dumps and `backup/` (.gitignore:1-33). A check of the whole history on 2026-10-03 found no such file ever committed. Nothing scans commits on GitHub's side yet |
| E | A fork's pull request runs with this repo's rights | There is one workflow, pages.yml. It has no `pull_request_target` trigger, and its token is `contents: read`, `pages: write`, `id-token: write` (pages.yml:34-37) |
| T | A third-party action changes under its tag | Actions are pinned to major tags, not commit SHAs (pages.yml:6-8, 49, 57, 73, 76, 82). A moved tag could deploy a different site |
| D | GitHub turns the schedule off | GitHub can disable scheduled workflows in a public repository after 60 days without activity. The data pushes every hour should count; `site-stale` says if not |

**Residual risk: low likelihood, high impact** for a committed `.env`. **You:** turn on **Secret
scanning** and **Push protection** (Settings → Code security; free for public repositories). Never
`git add -f`, and never keep a backup inside the checkout. If a secret is ever pushed, rotate it
first; rewriting history comes second, because a public push is copied within minutes.

---

## Open risks, ranked

| # | Risk | Rating | What you do | Where |
|---|---|---|---|---|
| 1 | Paperclip's AI agents run in the server container and inherit its environment: the superuser `DATABASE_URL`, the auth and signing secrets, the ops token, and the secrets folder in their home | **Critical** while any `opencode_local` agent runs | Keep the AI agents paused. Give Paperclip a least-privilege database role (backup first). Raise it upstream | [B5](#b5-paperclip-its-agents-and-the-ops-api-ops-token); docker-compose.yml:53, 56-58, 72-73 |
| 2 | No off-host backup (a restore was rehearsed on 2026-10-03, from the VM's own disk). Nothing alerts on a failed backup | **High** | Off-host target, `vzdump`, scheduled `ops/backup.sh` behind a `mountpoint` check, a weekly look at its log, monthly rehearsal | [B8](#b8-backups-on-a-single-disk-host) |
| 3 | GitHub tokens act as your account, so branch protection and "a human merges" do not bind them. The publish token could force-push any unprotected branch; since 2026-10-03 worker/publish/push.py refuses any but `data` and `data-<name>` | **High** once WHEELJACK runs; medium now | A separate identity for WHEELJACK; a no-bypass ruleset on `main` | [B6](#b6-wheeljack-to-github-engineer-token-and-branch-protection), [B4](#b4-worker-to-the-github-data-branch-publish-token) |
| 4 | TELETRAAN runs pull request code in the server container, and no CI runs the tests | **High** once Stage 6 runs | Fix #1 first; CI on pull requests (lead) | [B6](#b6-wheeljack-to-github-engineer-token-and-branch-protection) |
| 5 | The worker holds every secret and mounts the checkout read-write, a path to `oxygen` and so to root | **Medium** | Check `id -u`; read-only mount except `data/` (lead) | [B2](#b2-untrusted-feed-content-into-the-worker-and-into-model-prompts); docker-compose.yml:21, 26 |
| 6 | The whole `data` branch becomes the site | **Medium** | Check what `/data/` serves; copy only `*.json` (lead) | [B4](#b4-worker-to-the-github-data-branch-publish-token); pages.yml:63-67 |
| 7 | One ops token opens all three writes, and every agent holds it | **Medium** | A token per role (lead) | [B5](#b5-paperclip-its-agents-and-the-ops-api-ops-token); ops_api.py:641-655 |
| 8 | The OpenRouter key's hard limit is the last spend stop, and the agents hold the same key. `cost-anomaly` counts only the worker's calls | **Medium** (at most US$20 a month) | Never raise the limit to clear a symptom; look at the key's usage on OpenRouter weekly; rotate on unexpected spend | [B3](#b3-worker-to-openrouter-and-tavily) |
| 9 | Paperclip on plain HTTP across the LAN; Connection requests unset | **Medium** | Human only; DHCP reservation; separate network for guests; NetBird later | [B7](#b7-the-lan) |
| 10 | `db` has no restart policy (docker-compose.yml:2-15). Compose's default is `no`, so after a reboot `worker` and `server` come back and the database does not | **Medium** (availability) | After a reboot, check `docker compose ps`; add `restart: unless-stopped` to `db` (lead) | [Services](runbooks/services.md#database-down) |
| 11 | `.env` committed to the public repository | **Low** likelihood, high impact | Secret scanning and push protection; never `git add -f` | [B9](#b9-the-public-repository) |
| 12 | The publish token gains the Workflows permission | **Low** today | Never add it, or Administration | [B4](#b4-worker-to-the-github-data-branch-publish-token) |
| 13 | Worker images built before 2026-10-03 hold a copy of `.env`; the `.dockerignore` keeps it out of new ones | **Low** while the image never leaves the VM | Never `docker save` or push it; `docker image prune` after the next build and after a rotation | [B8](#b8-backups-on-a-single-disk-host); Dockerfile.worker:16 |
| 14 | Curated feeds are read whole before the size cap | **Low** | Stream with a cap, as the discovery guard does (lead) | [B2](#b2-untrusted-feed-content-into-the-worker-and-into-model-prompts); collectors/http.py:126-146 |
| 15 | Out-of-date wording: docker-compose.yml:70-71 ("reads only"). The rest (ops_api.py, 4b, 4c, PLAN.md, 4a §9) was corrected on 2026-10-03 | **Low** | Correct the compose comment when you next edit the server service | This page |
| 16 | Actions pinned by tag; Python dependencies unpinned | **Low** | Pin by SHA; a hashed lock file (lead) | [B9](#b9-the-public-repository), [B6](#b6-wheeljack-to-github-engineer-token-and-branch-protection) |

---

[Runbooks](runbooks/README.md) · [Wiki home](wiki/README.md)
