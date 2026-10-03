# Tokens and keys

[Runbooks](README.md) · [Threat model, credentials](../threat-model.md#credentials-names-only) ·
[.env.example](../../.env.example)

The credentials live in `.env` on the VM (mode 600, owner `oxygen`). Paperclip also keeps its
secrets folder, and its own copy of the OpenRouter key. There are copies in every backup, and in the
worker image from its last build
([threat model, B8](../threat-model.md#b8-backups-on-a-single-disk-host)). This page names them and
never shows a value.

| Name | What it is | Expires | Read by |
|---|---|---|---|
| `CYBERPULSE_PUBLISH_TOKEN` | GitHub fine-grained token: this repository, **Contents** read and write | **90 days** | worker |
| `CYBERPULSE_ENGINEER_TOKEN` | GitHub fine-grained token: this repository, **Contents** and **Pull requests** read and write | **90 days** | WHEELJACK, once Stage 6 runs |
| `CYBERPULSE_OPS_TOKEN` | Made on the VM, 32 characters or more | Never | worker and server, so every AI agent |
| `OPENROUTER_API_KEY` | OpenRouter key, US$20 monthly limit | Never | worker, and Paperclip's OpenRouter connection |
| `TAVILY_API_KEY`, `NVD_API_KEY` | Free-tier API keys | Never | worker |
| `TELEGRAM_BOT_TOKEN` | The bot's token from @BotFather | Never | worker |
| `PAPERCLIP_INCIDENT_WEBHOOK_SECRET` | The Incident routine's webhook secret | Never | worker |
| `POSTGRES_PASSWORD` | The database superuser's password | Never | db, worker, server |
| `BETTER_AUTH_SECRET`, `PAPERCLIP_AGENT_JWT_SECRET`, `PAPERCLIP_TOOL_ACTION_SIGNING_SECRET`, and Paperclip's secrets folder | Paperclip's signing and encryption keys | Never | server |

The two GitHub tokens are the only ones that run out. GitHub e-mails before they do. Put a
reminder in your calendar for day 80 as well.

---

## The same six steps every time

1. **Make the new one** at the provider. Keep the old one working for now.
2. **Edit `.env` with an editor,** as `oxygen` and without `sudo`: `nano .env`, then replace the
   value after the `=`. Never paste a token into a command line, a chat or a ticket.
3. **Check it parses:** `docker compose config -q` is silent when the file is fine.
4. **Recreate what reads it,** at a safe minute (`date -u +%M`, about :10 or :40). A restart keeps
   the old environment:

   ```bash
   docker compose up -d --force-recreate worker
   ```

5. **Check the new one works,** as each entry below says.
6. **Revoke the old one** at the provider. Only now, once the new one works.

The worker image still holds the old value from its last build. A revoked token there is dead. For
a value that cannot be revoked, such as the Postgres password, rebuild the image and remove the
old one, at a safe minute:

```bash
docker compose build worker
docker compose up -d --force-recreate worker
docker image prune                 # it asks first
docker builder prune               # the build cache keeps the layers too; it asks first
```

Old backups keep the old value too. That is fine once it is revoked.

---

## Publish token

**Symptom.** A GitHub e-mail that the token expires soon. Or `push-failure`, with
`Authentication failed` or `403` in the dry run below.

**Check.**

```bash
docker compose exec -T worker python -m worker.publish.push --dry-run </dev/null
```

On GitHub: **Settings → Developer settings → Personal access tokens → Fine-grained tokens** lists
each token with its expiry date and when it was last used.

**Fix.**

1. On that page, **Generate new token**:

   | Field | Value |
   |---|---|
   | Token name | `cyberpulse-publish`, with the month, so you can tell old from new |
   | Expiration | 90 days |
   | Repository access | **Only select repositories** → `happycode0/CyberPulse-AI` |
   | Permissions | **Contents: Read and write.** Metadata: Read is added by itself. Nothing else |

2. `nano .env`, the line `CYBERPULSE_PUBLISH_TOKEN=`.
3. `docker compose config -q`
4. `docker compose up -d --force-recreate worker` at a safe minute.
5. Run the dry run again. `dry run: would push … to data` or `data unchanged; nothing pushed` means
   GitHub took the token. A dry run only reads, so wait for the next collection run to prove it
   can write:

   ```bash
   docker compose logs worker --since 20m | grep -E "pushed|push after"
   ```

   `pushed <sha> to the data branch` or `data branch already current; nothing pushed` is right.
6. Delete the old token on GitHub.

**Stop and decide yourself:**

- **Never give it Workflows, Administration or any other permission, and never "All
  repositories".** With Workflows it could change what Pages builds and what every job runs
  ([threat model, B4](../threat-model.md#b4-worker-to-the-github-data-branch-publish-token)). If a
  push fails with the right permissions, the cause is somewhere else.
- **"Last used" shows a time when the worker did not push,** or the token is used from somewhere
  else: treat it as leaked. Delete it now, make a new one, then look for where it got out.

## Engineer token

Not in use yet. WHEELJACK is paused, and the token is still to be made
([Stage 6, What's left](../wiki/stage-6-self-healing.md#whats-left)).

**Fix.** The same as the publish token, with these permissions: this repository only, **Contents**
and **Pull requests** read and write, nothing else, 90 days. Put the new one wherever WHEELJACK
gets it from, then delete the old one. Compose does not pass it to the server
(docker-compose.yml:51-73). The worker never uses it.

**Check** after a rotation: WHEELJACK's next pull request opens, and the token's "Last used" on
GitHub moves.

**Stop and decide yourself** before WHEELJACK runs at all. A fine-grained token acts as your own
account, so it can merge past your own rules
([threat model, B6](../threat-model.md#b6-wheeljack-to-github-engineer-token-and-branch-protection)).
A separate identity for WHEELJACK comes first. After that, this entry rotates that identity's
token.

## Ops token

It never expires. **Rotate it** when an agent may have leaked it, after a prompt-injection scare,
or when you remove an agent. Every AI agent gets it from the server's environment
([threat model, B5](../threat-model.md#b5-paperclip-its-agents-and-the-ops-api-ops-token)), so a new
one keeps nothing out of the server container: it only ends the old one's use anywhere else.

**Fix.**

1. Pick a safe minute when no agent is mid-run. With the AI agents paused, as the threat model
   advises, any safe minute will do.
2. Write a new token straight into `.env`. It never appears on screen:

   ```bash
   sed -i "s/^CYBERPULSE_OPS_TOKEN=.*/CYBERPULSE_OPS_TOKEN=$(openssl rand -hex 32)/" .env
   grep -c '^CYBERPULSE_OPS_TOKEN=[0-9a-f]\{64\}$' .env      # prints 1
   ls -l .env                                                # still -rw------- oxygen
   ```

   If `grep` prints 0, the line was missing. Add `CYBERPULSE_OPS_TOKEN=` with `nano`, then run the
   `sed` line again.
3. `docker compose config -q`
4. Recreate both, together, so they never hold different tokens for long:

   ```bash
   docker compose up -d --force-recreate worker server
   ```

5. Check both sides:

   ```bash
   docker compose logs worker --since 5m | grep "ops API listening"     # "read endpoints need the token"
   ops /ops/incidents                                                   # the worker's side
   docker compose exec -T server sh -c 'curl -sS -o /dev/null -w "%{http_code}\n" -H "Authorization: Bearer $CYBERPULSE_OPS_TOKEN" http://worker:8700/ops' </dev/null
   ```

   The last line asks from inside the server, as an agent would. `200` is right. `401` means the
   two sides differ: one of them was not recreated. `503` means the worker has no token, or one
   under 32 characters.
6. Nothing to revoke. The old token stopped working when both were recreated.

## OpenRouter key

It never expires. **Rotate it at once** on spend that neither the worker's ledger nor Paperclip
explains ([AI budget](ai-budget.md)), or if it may have leaked.

**Fix.**

1. **If it leaked,** delete the old key first: **OpenRouter → Keys**. The worker's enrichment
   fails until the new key is in. Collection and publishing go on.
2. **OpenRouter → Keys → Create key**, as in [.env.example](../../.env.example): name
   `cyberpulse-worker`, **Credit limit** 20, **Limit reset** monthly. Never a management key.
   A new key starts the month with nothing spent. To keep this month's cap, give it what the old
   key had left, and set it back to 20 after the reset.
3. `nano .env`, the line `OPENROUTER_API_KEY=`. Then `docker compose config -q`, and
   `docker compose up -d --force-recreate worker` at a safe minute.
4. **Paperclip has its own copy.** The agents use the key through Paperclip's OpenRouter
   connection (**Connectors → My OpenRouter API**). Put the new key there too, or the agents stop
   when the old key goes.
5. Check:

   ```bash
   docker compose exec -T worker python -m worker --check-budget </dev/null
   ```

   It should show the new key's limit, `key limit $20.00 (monthly)` or what you set, and how much
   is left. **OpenRouter → Activity** shows the agents' calls on the new key after their next run.
6. If you have not yet, delete the old key.

**Stop and decide yourself:** never raise the limit to get through a rotation, and never leave the
new key without one.

## Other keys

Each takes [the same six steps](#the-same-six-steps-every-time), and a recreate of the worker:

| Key | Make the new one | Check it works | Revoke the old one |
|---|---|---|---|
| `TAVILY_API_KEY` | [app.tavily.com](https://app.tavily.com), a new API key | The next discovery pass (03:00 Sydney) completes: `q "select finished_at, completed, note from job_runs where job = 'discovery' order by finished_at desc limit 1"` | Delete it on Tavily |
| `NVD_API_KEY` | [Request a new one](https://nvd.nist.gov/developers/request-an-api-key). Blank is fine too | The next ground-truth pass (:25, every 6 hours) completes, as above with `job = 'groundtruth'` | NVD's site has no revoke. The key only raises a free rate limit |
| `TELEGRAM_BOT_TOKEN` | **@BotFather** → `/revoke` → your bot. The old one stops at once, so do the rest straight away | `docker compose logs worker --since 5m \| grep -i telegram` says `Telegram notifications are on`. The chat id does not change | Done by `/revoke` |
| `PAPERCLIP_INCIDENT_WEBHOOK_SECRET` | Paperclip → **Routines → Incident**, the webhook trigger card → **Rotate secret**. It is shown once | The start-up log says `incidents go to the Incident routine in Paperclip`. On the next sent incident, no `paperclip:incident:<n>: failed` line; if there is one, see [Stage 6's table](../wiki/stage-6-self-healing.md#the-incident-routine) | Done by the rotation |

`ops/check-keys.sh` checks most of these in one go from the VM, without showing a value. It needs
`jq` (`sudo apt-get install -y jq`), and it sends a test message to the Telegram chat.

## Postgres password

**Rotate it** only if it may have leaked. Every AI agent can read it today
([threat model, B5](../threat-model.md#b5-paperclip-its-agents-and-the-ops-api-ops-token)), so a new
one does not keep them out. It is in every backup's `env` too.

**Fix.** Every step at a safe minute, in this order. Postgres reads `POSTGRES_PASSWORD` only when
its volume is first set up, so changing `.env` alone changes nothing in the database.

1. **Back up first:** `./ops/backup.sh /mnt/backup` ([Backup and restore](backup-and-restore.md)).
2. Write a new password straight into `.env`. Hex needs no escaping in the database URLs:

   ```bash
   sed -i "s/^POSTGRES_PASSWORD=.*/POSTGRES_PASSWORD=$(openssl rand -hex 24)/" .env
   grep -c '^POSTGRES_PASSWORD=[0-9a-f]\{48\}$' .env         # prints 1
   grep -cF '${POSTGRES_PASSWORD}@db' .env                   # prints 1
   docker compose config -q
   ```

   The second `grep` checks that the worker's `DATABASE_URL` takes the password from
   `POSTGRES_PASSWORD`, as in .env.example, and so follows it. If it prints 0, fix that line with
   `nano` to match .env.example first.

3. Recreate `db`, so its environment has the new value. The worker and server reconnect with the
   old password, which still works:

   ```bash
   docker compose up -d --force-recreate db
   ```

4. Set the database to the new password with the command under "Making the database match a new
   `POSTGRES_PASSWORD`" in [4a troubleshooting](../wiki/stage-4a-paperclip-setup.md#troubleshooting).
   It reads the value from the db container's environment, so it is never typed or shown. Run its
   `docker compose exec` command only. Its last lines restart the worker, and a restart keeps the
   old password; step 5 recreates instead.
5. Recreate the two that connect:

   ```bash
   docker compose up -d --force-recreate worker server
   ```

6. Check:

   ```bash
   docker compose ps                  # db and server "(healthy)", worker "Up"
   docker compose logs worker server --since 10m | grep -i "password authentication"     # nothing
   docker compose logs -f worker      # the next run logs "done: ok="
   ```

7. Rebuild the worker image and remove the old one, as under
   [the six steps](#the-same-six-steps-every-time).

If step 4 was missed, the worker and server log `password authentication failed`. Do step 4 then,
and recreate them again.

**Stop and decide yourself:** a backup must come first. If the server will not come back after
step 5, the way out is to put the old password back in `.env` and recreate all three, not to touch
the volume.

## Paperclip's own secrets

`BETTER_AUTH_SECRET`, `PAPERCLIP_AGENT_JWT_SECRET`, `PAPERCLIP_TOOL_ACTION_SIGNING_SECRET` and the
secrets folder in the `paperclip-data` volume sign and encrypt Paperclip's own state: sessions,
agent tokens, approvals and the secrets it stores.

**Stop and decide yourself** before changing any of them. Rotate one only if it has leaked, after a
backup, with Paperclip's own documentation open. Never delete or replace a file in the secrets
folder: a backup of the `paperclip` database is no use without it
([Stage 7](../wiki/stage-7-hardening.md#backups--do-this-one-early)). Every AI agent can read these
today ([threat model, B5](../threat-model.md#b5-paperclip-its-agents-and-the-ops-api-ops-token)).
Keeping the agents paused is what protects them.

---

[Runbooks](README.md) · [AI budget](ai-budget.md) · [Backup and restore](backup-and-restore.md)
