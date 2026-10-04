# Runbooks

[Threat model](../threat-model.md) · [Wiki home](../wiki/README.md) ·
[the VM runbook](../vm200-runbook.md)

What to do when something breaks. Every entry has the same four parts:

- **Symptom:** what you see, usually a Telegram message.
- **Check:** commands that only read. Run them all before you change anything.
- **Fix:** the usual repair.
- **Stop and decide yourself:** where these steps end and your judgement starts.

---

## Before you start

Everything runs on the VM, in the checkout:

```bash
ssh cyberpulse-vm
cd ~/CyberPulse-AI
```

Two helpers make the checks shorter. Paste them into the shell once per session. Both only read.

```bash
# A query on cyber_intel, in a read-only session
q() { docker compose exec -T -e PGOPTIONS='-c default_transaction_read_only=on' db \
        psql -X -U cyberpulse -d cyber_intel -c "$1" </dev/null; }

# A GET on the worker's ops API, from inside the worker, with the token it already has
ops() { docker compose exec -T worker python -c '
import json, os, sys, urllib.error as e, urllib.request as u
r = u.Request("http://127.0.0.1:8700" + sys.argv[1],
              headers={"Authorization": "Bearer " + os.environ.get("CYBERPULSE_OPS_TOKEN", "")})
try:
    print(json.dumps(json.loads(u.urlopen(r, timeout=30).read()), indent=2))
except e.HTTPError as x:
    print(x.code, x.read().decode())
' "$1" </dev/null; }
```

Try them: `q "select now()"` and `ops /ops/incidents`. The token never appears on screen: the
helper reads it inside the container, and every ops API answer passes the secret scan.

## Ground rules

- **Never delete a volume.** No `docker compose down` with the volumes flag, no
  `docker volume rm`. The database lives in a volume.
- **Never widen a port.** Paperclip stays on `10.0.0.0:3100`. Never bind anything to every
  interface, and never forward a router port. Docker-published ports bypass `ufw`.
- **No one-off SQL that changes production rows.** These pages only read the database. If a repair
  needs a change, take a backup first ([backup and restore](backup-and-restore.md)) and decide it
  yourself.
- **Restart between :10 and :45 past the hour.** The fast lane runs every hour at :00 UTC. Its
  alerts and enrichment follow at :03 and :05, and the gate runs at :50. `date -u +%M` prints
  the minute.
- **A restart keeps the old environment.** After changing `.env`, use
  `docker compose up -d --force-recreate <service>`.
- **Never print `.env`.** To check it parses: `docker compose config -q` (silent when fine). To
  see which names it sets, without values: `grep -oE '^[A-Z0-9_]+' .env`.
- **`docker compose exec` always gets `-T`**, and `</dev/null` when it needs no input.
- **Write down what you did,** with the time, in the incident's `[INCIDENT]` issue or your notes.

## The runbooks

| Page | Covers |
|---|---|
| [Your checklist](owner-checklist.md) | What is left for you, in order: the 8-agent import, the agents' environment, turning the crew on |
| [Watchdog incidents](watchdog-incidents.md) | One entry for each of the 14 incident kinds |
| [Services](services.md) | The database down; Paperclip down |
| [AI budget](ai-budget.md) | A budget exhausted, the OpenRouter limit hit, unexpected spend |
| [Tokens](tokens.md) | Rotating the publish, engineer and ops tokens, and the other keys |
| [Deploy and rollback](deploy-and-rollback.md) | A normal deploy; a bad one: `ops/rollback.sh` |
| [Backup and restore](backup-and-restore.md) | Taking, checking and rehearsing backups; a restore onto a rebuilt VM |

## Where to look first

| You see | Go to |
|---|---|
| `CyberPulse-AI · incident · HIGH` or `· CRITICAL`, then `INC-<n> · <kind> · <subject>` | [Watchdog incidents](watchdog-incidents.md), the entry for that kind |
| `CyberPulse-AI · incident · needs a human` | [The circuit breaker](watchdog-incidents.md#the-circuit-breaker-tripped) |
| `CyberPulse-AI · incident resolved` | Nothing. The watchdog saw the fault gone for 15 minutes |
| `CyberPulse-AI · system failure` ("The worker has not reached its database since …") | [Database down](services.md#database-down) |
| `CyberPulse-AI · system recovered` | Nothing. Read the worker's log for the gap if you want to know why |
| An agent shows "Budget paused" in Paperclip | [AI budget](ai-budget.md) |
| A GitHub e-mail that a token expires soon | [Tokens](tokens.md) |
| The worker crashes straight after a merge | [Deploy and rollback](deploy-and-rollback.md) |
| Nothing at all from Telegram for a day | [Database down](services.md#database-down) first, then `docker compose ps` |

Medium and low incidents are not sent anywhere. They wait in `ops /ops/incidents`.

---

[Threat model](../threat-model.md) · [Wiki home](../wiki/README.md)
