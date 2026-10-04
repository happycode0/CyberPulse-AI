# Your checklist

[Runbooks](README.md) · [Wiki home](../wiki/README.md) · [Threat model](../threat-model.md)

What is left for you to do, in order, as of 2026-10-04. The code side is finished and deployed;
everything here is a decision or a click that is yours. Each step says where its detail lives.

**When can every agent and routine be on?** About a week after you finish steps 2 and 4: the
crew is turned on in stages (step 5). SERAPH can go on as soon as step 2 is done. Nothing in the
code is waiting; the gates are the import, the agents' environment and the money.

---

## 1. Quick ones (15 minutes)

1. **The OpenRouter key's limit.** The worker reads US$4.53 left of US$20, so it is in `conserve`
   mode: fewer AI briefs, and only for critical and KEV-linked events. On **OpenRouter → Keys**,
   look at the key's usage. If the US$20 is a one-off limit rather than a monthly one, set it to
   reset monthly and keep it at US$20, which is what the worker's budget assumes
   ([AI budget](ai-budget.md)). Don't raise it above US$20 to clear the symptom (threat model,
   risk 8).
2. **The Pages schedule.** Collection is hourly now, finishing a minute or two past the hour. In
   `.github/workflows/pages.yml`, `cron: "*/15 * * * *"` can become `cron: "12 * * * *"`, and the
   comment above it (lines 23–25) should say the FAST lane runs hourly. Optional: every 15
   minutes still works, it just rebuilds unchanged content three times an hour.
3. **Leftover screenshots.** Delete `/home/d739962/.aws/cp-trends.png`,
   `/home/d739962/.aws/.playwright-mcp/*.png`, and the folders `/home/d739962/.aws/cp-shell-shots/`
   and `/home/d739962/.aws/cp-pages-shots/` if they exist. Claude is not allowed to delete them.

## 2. Paperclip: 16 agents to 8 (about an hour)

Follow [4a, Moving from 16 agents to 8](../wiki/stage-4a-paperclip-setup.md#moving-from-16-agents-to-8).
The worker is already updated, so in its step 1 only try the wake. In short:

1. SERAPH's wake with `{"job":"pipeline"}` answers 200 or 503, and `link/wake` answers 404.
2. Build the package (`python3 ops/build-paperclip-package.py`) and import it: **Company Settings →
   Import → Replace**, with **Start imported agents and routines paused** ticked. Don't press
   **Activate selected**.
3. Check DECKARD's four desk skills, TELETRAAN on `qwen/qwen3.8-flash`, RIPPERDOC reporting to
   MORPHEUS, SERAPH's `{"job": "pipeline"}` payload, and 8 paused routines with the crons in
   [4a step 7](../wiki/stage-4a-paperclip-setup.md#7--create-the-routines).
4. Terminate ZION, BLASTER, WINTERMUTE, TRON, ROGUE, PROWL, LIBRARIAN and LINK (the console
   snippet archives their routines first), then untick them in the OpenRouter connector's
   permissions.
5. Set the budgets (in cents): MORPHEUS 200, TELETRAAN 100, DECKARD 200, VOIGHT 200, TACHIKOMA
   100, RIPPERDOC 50, WHEELJACK 100. The company stays at 1200.
6. Smoke-test DECKARD, TELETRAAN and RIPPERDOC, one at a time, pausing each straight after.
7. Leave everything paused.

**Then resume SERAPH.** It is an `http` agent: Paperclip calls the worker, no model runs and no
process starts in the server, so step 4's risk does not apply to it.

## 3. Label the AI golden set (20 minutes)

Fill in the 10 entries in `worker/ai/golden_ai.yaml`: `beat`, `ai_significance`, `au_desk` and
`reviewed: true` ([AI news beat](../wiki/ai-news-beat.md)). Commit, deploy, then on the VM:

```bash
docker compose exec -T worker python -m worker --pin-golden-set </dev/null
```

## 4. Before any AI agent runs: move the agents off the server

The 7 AI agents run as processes inside the Paperclip server's container and inherit its
environment: the database superuser password, the auth signing secret and the ops token
([threat model, risk 1](../threat-model.md#open-risks-ranked), **Critical** while any of them
runs). Paperclip's **Environments** feature (experimental) runs agents on another machine
instead, which closes most of it:

1. Make a small Proxmox LXC or VM for agent runs (1 vCPU, 2 GB). Put **no** CyberPulse secrets
   on it: no `.env`, no checkout with one. Install `opencode` and `git`, and make a user that logs
   in with an SSH key only.
2. In Paperclip: **Settings → Instance settings → Environments → Add environment → SSH** (host,
   user, key, remote path). Press **Test**, then mark it the instance **Default**.
3. Check an agent's configuration shows **Environment override: Default: <name> · ssh**.
4. Smoke-test MORPHEUS again, and on the new machine confirm the run happened there and that
   `DATABASE_URL` is not in its environment.

Also give Paperclip its own non-superuser database role (take a backup first). Tell Claude
when the machine exists, and it will write this up as a full runbook with your host's details.

## 5. Turn the crew on in stages (a week)

After steps 2 and 4. Each day, look at **OpenRouter → Activity** and each agent's **Budget** tab,
and pause any agent spending faster than its monthly budget ÷ 30 a day.

| When | Resume | Switch on |
|---|---|---|
| Day 1 | MORPHEUS, DECKARD, RIPPERDOC | Daily editorial, Desk digest, Monthly cost review |
| Day 3 | VOIGHT | Daily QA sample, Follow-up, Model scan |
| Day 5 | TACHIKOMA | Source discovery (it finds little until the worker has a Tavily key), Model gauntlet |
| Any time | TELETRAAN | Nothing: it wakes on incidents only |
| Last | WHEELJACK | Only after risks 3 and 4: its own GitHub identity, branch protection on `main`, CI on pull requests |

## 6. Data fixes

- **Three titles with `&amp;`** (evt-2026-002259, 002258 and 002043; new items are already
  unescaped). On the VM, `q` from the [runbooks](README.md#before-you-start) is read-only, so
  use plain psql:

  ```bash
  docker compose exec -T db psql -X -U cyberpulse -d cyber_intel <<'SQL'
  begin;
  select pg_advisory_xact_lock(726202603);
  update events set title = replace(title, '&amp;', '&'), updated_at = now()
   where event_id in ('evt-2026-002259', 'evt-2026-002258', 'evt-2026-002043')
     and title like '%&amp;%';
  commit;
  SQL
  ```

  It should say `UPDATE 3`. The next publish carries the fixed titles.
- **The evt-2026-001795 un-merge is no longer needed.** Both stories have been archived and are
  off the site.

## 7. The safety net

- An off-host backup: a nightly cron for `ops/backup.sh` behind a `mountpoint` check, plus
  `vzdump` of VM 200 (risk 2, [Backup and restore](backup-and-restore.md)).
- `restart: unless-stopped` on the `db` service in `docker-compose.yml` (risk 10), and the stale
  "reads only" comment on lines 70–71 (risk 15).
- Keys in the VM's `.env`, never in a Paperclip form: Telegram (alerts), Tavily (source
  discovery), NVD and YouTube ([Tokens](tokens.md)).
- The incident webhook and its hostname allowlist; the engineer token and branch protection.
