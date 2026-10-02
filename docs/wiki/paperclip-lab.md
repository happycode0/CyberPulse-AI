# Paperclip lab — learn it on VM 200 without touching CyberPulse

A sandbox for learning. It is **not** the Stage 4 production install: it runs Paperclip's own
embedded database, listens on loopback only, and shares nothing with the CyberPulse stack. Break it
freely; step 7 deletes it.

> **Verified on 2026-10-02:** steps 0–3 were run on VM 200. Paperclip `2026.824.1` came up healthy
> on `127.0.0.1:3100` in 8 seconds, on Node 20.20.2.

---

## Why the first attempt failed

```
Paperclip server failed to start.
getaddrinfo ENOTFOUND db
```

Paperclip **auto-loads any `.env` in the folder you start it from** (PLAN.md §2.7). It was started
from `~/CyberPulse-AI`, whose `.env` has `DATABASE_URL=postgresql://…@db:5432/…`. That overrode
Paperclip's embedded database and pointed it at `db` — a hostname that only exists inside the
CyberPulse Docker network. `doctor` passed because it checks Paperclip's config file, not the
environment it inherits.

**Rule: never start Paperclip from inside `~/CyberPulse-AI`.** Always from `~/paperclip-lab`.

Two side notes from the same attempt:

- **Don't use `sudo`.** `sudo npx paperclipai onboard` created a second instance owned by root in
  `/root/.paperclip`. Paperclip runs agents that execute commands; as root, they would own the box.
- **Ignore the npm 12 notice.** npm 12 needs Node 22+, and npm 10 is fine for this.

---

## 0. Clean up the failed attempts (once)

```bash
ssh cyberpulse-vm
sudo rm -rf /root/.paperclip            # the root-owned instance from `sudo npx`; holds no data
```

Keep `~/.paperclip` — that is your instance, already migrated and ready.

## 1. Make the lab folder and its start script

```bash
mkdir -p ~/paperclip-lab && cd ~/paperclip-lab
cat > start.sh <<'EOF'
#!/usr/bin/env bash
# Start the Paperclip lab. Run from anywhere; it always starts from this folder.
set -euo pipefail
cd "$(dirname "$0")"
unset DATABASE_URL                       # belt and braces: never inherit CyberPulse's database
export PAPERCLIP_TELEMETRY_DISABLED=1    # defaults to ON
export PAPERCLIP_ANNOUNCEMENTS_ENABLED=false
exec npx -y paperclipai run
EOF
chmod +x start.sh
```

## 2. Start it inside `tmux`, so it survives you closing the terminal

```bash
sudo apt-get install -y tmux             # once
tmux new -s paperclip                    # opens a session
~/paperclip-lab/start.sh                 # wait for "Server listening on 127.0.0.1:3100"
# detach: Ctrl-b then d      reattach later: tmux attach -t paperclip      stop: Ctrl-c inside it
```

Check from a second shell:

```bash
curl -s http://127.0.0.1:3100/api/health | head -c 200; echo   # expect "status":"ok"
```

## 3. Open the dashboard from your laptop (SSH tunnel — nothing exposed)

In a **WSL** terminal on the laptop, leave this running:

```bash
ssh -N -L 3100:127.0.0.1:3100 cyberpulse-vm
```

Then browse to **http://localhost:3100** in Windows. The tunnel is why no firewall rule, port
forward or NetBird is needed for the lab — Paperclip still only listens on the VM's loopback.

## 4. Learn it — the exercises, in order

Each one teaches one idea. Prompts to paste are in [Paperclip prompts](paperclip-prompts.md).

| # | Do this | What it teaches |
|---|---|---|
| 1 | Create a company **CyberPulse Lab** with the mission prompt | Everything in Paperclip hangs off a company goal |
| 2 | **Settings → turn ON "require board approval for new agents"** before hiring anyone | It defaults to **off**, and agents can hire agents |
| 3 | Hire **MORPHEUS** as CEO with a **US$1 monthly budget** | Roles, instructions, budgets as hard caps |
| 4 | Turn the CEO's **heartbeat off**; wake it by assigning a ticket instead | "Each wakeup costs tokens" — CyberPulse agents wake on assignment, not on a timer |
| 5 | Assign the first ticket and watch the run and activity log | How an agent reads a ticket, works, and comments |
| 6 | Ask MORPHEUS to hire ZION; **approve or reject** it as the board | The approval gate in action |
| 7 | Let a budget run out (set one to US$0.10) | What a hard stop looks like — no surprise bill |
| 8 | Create a **routine** (scheduled task) for a daily briefing | How the DEEP lane will be scheduled in Stage 4 |

**Choosing an adapter (the "brain" of an agent).** Paperclip runs agents through a local CLI or an
HTTP endpoint. Pick one you actually have:

- `opencode_local` with an OpenRouter key — what PLAN.md uses for the crew. Pick a `:free` or cheap
  model for the lab.
- `claude_local` / `codex_local` — only if that CLI is installed and logged in on the VM.
  The Claude adapter asks for **Node 22+** (see the `EBADENGINE` warnings); the server itself is fine
  on Node 20.
- `process` / `http` — no LLM at all; a script or URL. This is how the zero-token agents (LIBRARIAN,
  SERAPH, LINK) will work.

## 5. Lab safety rules

- Loopback only. Never set the bind host to `0.0.0.0`; never forward a router port.
- Every agent gets a budget, and a small one.
- **Never paste a CyberPulse key** (publish token, OpenRouter production key, Telegram) into the lab.
  Make a separate OpenRouter key with a US$2 limit for it.
- Coding adapters (`codex_local`) default to **bypassing their sandbox** (PLAN.md §2.2). Don't give a
  coding agent a real repo in the lab.

## 6. Useful commands

```bash
npx paperclipai doctor          # run from ~/paperclip-lab, not ~/CyberPulse-AI
npx paperclipai configure       # change settings interactively
ls ~/.paperclip/instances/default/logs/
```

## 7. Reset or remove the lab

```bash
tmux kill-session -t paperclip
rm -rf ~/.paperclip                      # reset: data, embedded DB, secrets key — keeps start.sh
cd ~/paperclip-lab && npx -y paperclipai onboard --yes   # fresh instance; then start.sh again

rm -rf ~/paperclip-lab                   # remove the lab entirely (after the reset above)
```

Nothing here affects `~/CyberPulse-AI`.

## Lab vs. production (Stage 4)

| | Lab (this page) | Production (runbook Part 6) |
|---|---|---|
| Mode | `local_trusted` — no login | `authenticated` + `private` — claim the instance |
| Database | embedded, port 54329 | the shared `pgvector` Postgres, `paperclip` database |
| Reach | SSH tunnel | NetBird interface only |
| Version | whatever `npx` pulls | image pinned by digest |
| Hardening | telemetry + announcements off | all five toggles, sign-up off after claim |
