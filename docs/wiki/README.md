# CyberPulse-AI wiki

Short, practical pages for the owner. `PLAN.md` is the design; `docs/vm200-runbook.md` is the
exact record of how VM 200 was built. This wiki is the "what do I do next" layer on top of both.

| Page | What it is for |
|---|---|
| [Paperclip lab](paperclip-lab.md) | Install Paperclip in a throwaway sandbox on VM 200 and learn it hands-on |
| [Paperclip prompts](paperclip-prompts.md) | Copy-paste prompts: the company, the CEO, the first desks, the first tickets |
| [VM 200 runbook](../vm200-runbook.md) | How the production box was built, command by command |
| [The plan](../../PLAN.md) | Architecture, the sixteen-agent crew, the stages |

---

## You are here

```text
Stage 0  Prerequisites ............................ ✅ done   (.env, keys, VM 200 built)
Stage 1  Foundation: pipeline + site .............. ✅ done   (collecting, scoring, site renders)
Stage 2  Ground truth + enrichment ................ 🟡 half   (KEV / EPSS / CVSS live on VM 200)
           ├─ ground-truth sync ................... ✅ running every 6 h
           ├─ publish token (fine-grained) ........ ✅ created, ⏳ not yet on the VM
           └─ OpenRouter client + cost ledger ..... ⏳ next build
Stage 3  Correlation depth + trends ............... ⬜
Stage 4  Proxmox + Paperclip + first agents ....... 🟡 VM built; ▶ YOU ARE HERE: learning Paperclip in the lab
Stage 5  Full crew + notifications ................ ⬜
Stage 6  Self-healing ............................. ⬜
Stage 7  Hardening + backups ...................... ⬜   (off-host backup target still needed)
```

**Where things live right now**

| Thing | Where | Notes |
|---|---|---|
| Repo (your checkout) | `C:\Users\d739962\HappyCode\CyberPulse-AI` | branch `stage-1-foundation`; `main` not merged yet |
| VM 200 | `ssh cyberpulse-vm` → `oxygen@192.168.128.39` | Debian 13, 4 vCPU / 12 GB / 60 GB |
| SSH key | WSL `~/.ssh/cyberpulse_vm_ed25519` | no passphrase, no backup |
| CyberPulse stack | VM `~/CyberPulse-AI` | `docker compose ps` → `db` + `worker` |
| CyberPulse secrets | VM `~/CyberPulse-AI/.env` | git-ignored; never committed |
| Paperclip lab | VM `~/paperclip-lab` (run folder), `~/.paperclip` (data) | loopback only, `127.0.0.1:3100` |

**What is yours to do (🔴), in order**

1. Copy the fine-grained publish token to the VM — or ask Claude to (it will not do it unasked).
2. Learn Paperclip in the lab → [Paperclip lab](paperclip-lab.md).
3. Provide an off-host backup target (NAS, USB disk or PBS). The host has a single disk.
4. When Stage 4 lands for real: NetBird, claim the production instance, the five hardening toggles
   (runbook Part 6).
