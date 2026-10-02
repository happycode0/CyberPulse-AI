# Paperclip lab — learn it on the VM without touching CyberPulse

A sandbox for learning. It is **not** the Stage 4 production install: it runs Paperclip's own
embedded database, listens on loopback only, and shares nothing with the CyberPulse stack. Break it
freely; step 7 deletes it.

> **Verified on 2026-10-02:** steps 0–3 were run on the VM. Paperclip `2026.824.1` came up healthy
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

### 3b. Or: open it straight from the home LAN (no tunnel)

The owner chose this on 2026-10-02 — the VM is on the home network, not the internet. Two things
change together, and the second is not optional:

1. **Bind to the VM's LAN address only** — `10.0.0.0`, not `0.0.0.0` (which would also listen
   on the Docker bridges).
2. **Switch from `local_trusted` to `authenticated`.** `local_trusted` has *no login*: on loopback
   that means only you, but on the LAN it means every phone, guest and smart TV on the Wi-Fi gets
   admin over agents that run commands on the VM.

`onboard --bind lan` does (2) but binds `0.0.0.0` and leaves telemetry on, so set it directly:

```bash
C=~/.paperclip/instances/default/config.json
cp -p $C $C.bak-$(date +%Y%m%d%H%M%S)
python3 - "$C" <<'PY'
import json, sys
p = sys.argv[1]; c = json.load(open(p))
c["server"].update({"deploymentMode": "authenticated", "exposure": "private", "bind": "custom",
                    "host": "10.0.0.0", "allowedHostnames": ["10.0.0.0"]})
c["telemetry"]["enabled"] = False
c["$meta"]["source"] = "configure"   # doctor only accepts onboard | configure | doctor here
json.dump(c, open(p, "w"), indent=2)
PY
~/paperclip-lab/start.sh
```

Then, **straight away**, from a browser on the home network:

1. Open **http://10.0.0.0:3100** and create your account — the first account claims the
   instance, so do it before anyone else can.
2. Turn sign-up off, then restart: in `config.json` set `"auth": {"disableSignUp": true}`.

Loopback (`127.0.0.1:3100`) stops answering in this mode; the SSH tunnel from step 3 still works if
you point it at the LAN address: `ssh -N -L 3100:10.0.0.0:3100 cyberpulse-vm`.

**Keep it running across reboots (optional, your call).** A system service, running as `deploy-user`:

```bash
sudo tee /etc/systemd/system/paperclip-lab.service >/dev/null <<'UNIT'
[Unit]
Description=Paperclip lab (LAN, authenticated)
After=network-online.target
Wants=network-online.target

[Service]
User=deploy-user
Group=deploy-user
Environment=HOME=/home/deploy-user
WorkingDirectory=/home/deploy-user/paperclip-lab
ExecStart=/home/deploy-user/paperclip-lab/start.sh
Restart=on-failure
RestartSec=10
NoNewPrivileges=true

[Install]
WantedBy=multi-user.target
UNIT
sudo systemctl daemon-reload && sudo systemctl enable --now paperclip-lab
journalctl -u paperclip-lab -f          # watch it start; Ctrl-c to stop watching
```

Undo: `sudo systemctl disable --now paperclip-lab`, and restore the `config.json.bak-*` file.

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
