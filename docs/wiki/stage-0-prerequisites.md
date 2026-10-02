# Stage 0 — Prerequisites

[← Wiki home](README.md) · [Stage 1 — Foundation →](stage-1-foundation.md)

**Status: ✅ done.** Plan: [PLAN.md §9, Stage 0](../../PLAN.md#9-stages) · Build record:
[runbook Parts 1–4](../vm200-runbook.md)

---

## What this stage gives you

The accounts, keys and machine that everything else runs on. No code yet: this is the part only
the owner can do.

## What is done

| Item | State | Where it lives |
|---|---|---|
| OpenRouter account and API key, US$20 hard limit on the key | ✅ | `.env` `OPENROUTER_API_KEY` |
| Tavily (free tier) for source discovery | ✅ | `.env` `TAVILY_API_KEY` |
| NVD API key, the last rung of the CVSS chain | ✅ | `.env` `NVD_API_KEY` |
| GitHub repo, public | ✅ | `happycode0/CyberPulse-AI` |
| Publish token: `contents: write` on this repo only, **no `workflow` scope** | ✅ | `.env` `CYBERPULSE_PUBLISH_TOKEN` (first used in Stage 1) |
| the VM on Proxmox: Debian 13, 4 vCPU / 12 GB / 60 GB disk, Docker | ✅ | [runbook Parts 1–3](../vm200-runbook.md) |
| `.env` on the VM: 24 keys, none empty, no duplicates (checked 2026-10-02) | ✅ | VM `~/CyberPulse-AI/.env` |
| Telegram bot and chat id | ⬜ not needed until [Stage 5](stage-5-full-crew.md) | `.env` `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` |

## How to check it

On the VM. Neither command prints a secret value:

```bash
cd ~/CyberPulse-AI
docker compose config -q && echo "compose config OK"
grep -E '^[A-Z0-9_]+=.+' .env | cut -d= -f1 | sort     # names of the keys that have a value
```

## Rules that started here and still apply

- **`.env` never goes into git.** It is git-ignored, and the repo is public.
- **Secrets are only read from the environment** (`worker/settings.py`). They are never hard-coded,
  logged or written into `data/`.
- **The publish token never gets `workflow` scope.** A worker that could rewrite
  `.github/workflows/**` could get round Stage 6's approval gates.
- **Back up `.env` somewhere off the VM.** Lose it and every key has to be reissued.

## Done when

- [x] `.env` populated
- [x] `docker compose config` validates

---

[← Wiki home](README.md) · **Next:** [Stage 1 — Foundation: pipeline + site →](stage-1-foundation.md)
