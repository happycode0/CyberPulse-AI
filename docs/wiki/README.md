# CyberPulse-AI wiki

Short, practical pages for the owner. `PLAN.md` is the design; `docs/vm200-runbook.md` is the
exact record of how VM 200 was built. This wiki is the "what do I do next" layer on top of both.

| Page | What it is for |
|---|---|
| [Paperclip setup](paperclip-setup.md) | Open Paperclip, claim it, harden it, create the company, the 16 agents and their routines |
| [The crew](paperclip-crew.md) | All 16 agents: the settings for each one and the prompt to paste |
| [How the crew works together](how-the-crew-works.md) | Who wakes whom, how work is handed off, where money and code are stopped |
| [VM 200 runbook](../vm200-runbook.md) | How the production box was built, command by command |
| [The plan](../../PLAN.md) | Architecture, the crew, the stages |

---

## You are here

```text
Stage 0  Prerequisites ............................ ✅ done   (.env, keys, VM 200 built)
Stage 1  Foundation: pipeline + site .............. ✅ done   (runbook Part 5: collecting every 15 min)
           └─ publish token on the VM (Part 5d) ... ✅ in .env since 2026-10-02 (first live push pending)
Stage 2  Ground truth + enrichment ................ 🟡 half   (KEV / EPSS / CVSS live on VM 200)
           └─ OpenRouter client + cost ledger ..... ⏳ built with Stage 4, before any agent spends
Stage 3  Correlation depth + trends ............... ⬜
Stage 4  Paperclip + first agents ................. ▶ YOU ARE HERE
           ├─ Paperclip Docker service ............ 🟢 Claude builds next
           ├─ open, claim, harden ................. 🔴 you — setup steps 2–4
           ├─ company + 16 agents + routines ...... 🔴 you — setup steps 5–7 (9 agents start paused)
           └─ ops API + http agents ............... 🟢 Claude
Stage 5  Full crew + notifications ................ ⬜   (un-pauses BLASTER, WINTERMUTE, TACHIKOMA,
                                                          DECKARD, VOIGHT, RIPPERDOC)
Stage 6  Self-healing ............................. ⬜   (un-pauses WHEELJACK, TRON)
Stage 7  Hardening + backups ...................... ⬜   (off-host backup target still needed)
```

**Where things live right now**

| Thing | Where | Notes |
|---|---|---|
| Repo (your checkout) | `C:\Users\d739962\HappyCode\CyberPulse-AI` | branch `stage-1-foundation`; `main` not merged yet |
| VM 200 | `ssh cyberpulse-vm` → `oxygen@192.168.128.39` | Debian 13, 4 vCPU / 12 GB / 60 GB |
| SSH key | WSL `~/.ssh/cyberpulse_vm_ed25519` | no passphrase, no backup |
| CyberPulse stack | VM `~/CyberPulse-AI` | `docker compose ps` → `db` + `worker` (+ `server` once Stage 4 lands) |
| CyberPulse secrets | VM `~/CyberPulse-AI/.env` | git-ignored; never committed. What goes in it: [setup step 0](paperclip-setup.md#0--add-the-paperclip-settings-to-env-once) |
| Paperclip dashboard | http://192.168.128.39:3100 | home network only, login required — once Stage 4 lands |

**What is yours to do (🔴), in order**

1. **Add the Paperclip section to `.env`** — [setup step 0](paperclip-setup.md#0--add-the-paperclip-settings-to-env-once).
   One command; it generates the secrets on the VM.
2. **When the Paperclip service lands:** [setup](paperclip-setup.md) steps 2–8 — open it, claim
   it, harden it, create the company, the 16 agents and the routines.
3. Provide an off-host backup target (NAS, USB disk or PBS). The host has a single disk.
4. Later: NetBird, if you want the dashboard from outside the house.
