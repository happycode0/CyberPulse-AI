# Watchdog incidents

[Runbooks](README.md) · [Stage 6 — the watchdog](../wiki/stage-6-self-healing.md#the-watchdog) ·
[Threat model](../threat-model.md)

The watchdog checks the system every five minutes, at 2, 7, 12 … minutes past the hour (UTC). A
fault it sees opens an **incident**: a kind, and a subject such as a lane, a source or a job. High
and critical incidents go to Telegram, and to the crew's Incident routine in Paperclip when that
is set up:

```text
CyberPulse-AI · incident · HIGH

INC-42 · parser-drift · <source id>
<title>
Seen 14:07 Sydney time, …

<what this kind means and who fixes it>
```

Medium and low ones only wait in the list. Every entry below assumes the `q` and `ops` helpers from
[Before you start](README.md#before-you-start).

## For every incident

**Check** first, whatever the kind:

```bash
ops /ops/incidents                 # what is open, each with a guide to its kind
ops /ops/incidents/42              # one incident: its evidence, and TELETRAAN's verdicts
docker compose logs worker --since 1h | grep -E "incident|watchdog"
```

- **Look in Paperclip before you fix anything.** If TELETRAAN or WHEELJACK is running, the
  `[INCIDENT]` or `[ENGINEERING]` issue shows whether someone is already on it.
- **You never close an incident.** It resolves itself 15 minutes after its fault is last seen. A
  fault back within 6 hours reopens the same incident, with its count of failed fixes.
- **To see at once whether a fix took,** run one pass by hand. Avoid minutes ending in 2 or 7,
  when the scheduled pass runs:

  ```bash
  docker compose exec -T worker python -m worker --watchdog </dev/null
  ```

---

## no-collection

**Symptom.** `no-collection · fast` (critical) or `no-collection · normal` (high): "The fast lane
has not finished a run for 47 minutes". A lane has finished no run for 35 minutes (fast) or
4 h 35 min (normal).

**Check.**

```bash
docker compose ps
docker compose logs worker --since 1h --tail 200
docker compose logs worker --since 2h | grep -E "done: ok=|Traceback|Error" | tail -20
q "select lane, max(finished_at) as last_finished,
          count(*) filter (where finished_at is null) as unfinished
     from runs where started_at > now() - interval '1 day' group by lane"
df -h /
free -h
```

**Fix.**

- **The database is down** (the log says it cannot connect): [Database down](services.md#database-down).
- **The worker crashes straight after a merge:** pin the last good commit,
  [Deploy and rollback](deploy-and-rollback.md).
- **The disk is full:** [Database down, disk full](services.md#disk-full).
- **Otherwise the worker is stuck.** Restart it between runs, then watch for the next
  `done: ok=`:

  ```bash
  date -u +%M                        # restart at about :10 or :40
  docker compose restart worker
  docker compose logs -f worker      # Ctrl-c stops watching, not the worker
  ```

**Stop and decide yourself** if it crashes again with the same error and nothing was merged, or
if runs start and never finish more than once. That is a code fault: a pull request, not more
restarts.

## zero-volume

**Symptom.** `zero-volume · fast` or `· normal` (high): "The fast lane's last 3 runs fetched
nothing". Runs finish, but no source returns anything.

**Check.** The network and DNS, from the VM and from inside the worker:

```bash
getent hosts www.cyber.gov.au
curl -sS -o /dev/null -w '%{http_code}\n' https://www.cyber.gov.au/rss/alerts
docker compose exec -T worker python -c 'import socket; print(socket.gethostbyname("www.cyber.gov.au"))' </dev/null
docker compose exec -T worker python -c 'import urllib.request as u; print(u.urlopen(u.Request("https://www.cyber.gov.au/rss/alerts", headers={"User-Agent": "CyberPulse-AI/1.0"}), timeout=30).status)' </dev/null
q "select source_id, status, error, checked_at from source_health
    where checked_at > now() - interval '1 hour' order by checked_at desc limit 30"
```

**Fix.**

- **The VM cannot reach the internet:** the home connection or the router. Wait, or restart the
  router. The worker carries on by itself once the network is back.
- **The VM can, the container cannot:** Docker's networking. Restart the worker at a safe minute.
  If that does not help, `sudo systemctl restart docker` restarts every container, so pick a safe
  minute for that too, and check `docker compose ps` afterwards (see
  [after a reboot](services.md#database-down)).
- **Every source answers with an error such as 403:** the publishers are refusing the worker. Do
  not change the User-Agent to look like a browser. Tell the lead.

**Stop and decide yourself** if the network is fine and the worker still fetches nothing. That is
a collector fault.

## feed-failing

**Symptom.** `feed-failing · <source id>`: "<name> has failed its last 5 checks". High for a
priority-1 source, otherwise medium (not sent).

**Check.**

```bash
ops /ops/incidents/42              # same_host and same_host_failing: are its siblings failing too?
q "select id, name, url, lane, priority, enabled from source_registry where id = '<source id>'"
q "select checked_at, status, error from source_health
    where source_id = '<source id>' order by checked_at desc limit 10"
curl -sS -o /dev/null -w '%{http_code} %{redirect_url}\n' '<the url from source_registry>'
```

**Fix.**

- **Its siblings on the same host fail too:** the publisher is down. Nothing to fix. It resolves
  when they are back.
- **Only this feed fails, and the URL now redirects or gives 404:** the feed has moved. Find the
  new address on the publisher's own page. The change is a pull request to `config/sources.yaml`,
  with that page as proof. WHEELJACK does this when it runs.

**Stop and decide yourself** before disabling a source. A priority-1 source that is gone for good
changes what the site covers.

## parser-drift

**Symptom.** `parser-drift · <source id>` (high): "<name> answers, but its last 3 checks found no
items". The feed or page changed shape.

**Check.** Look at what the worker last received. The raw cache keeps every response, under
`/var/cache/cyberpulse/<source id>/<UTC date>/`. Feed bytes are untrusted: `cat -v` shows control
characters instead of passing them to your terminal.

```bash
q "select checked_at, status, error from source_health
    where source_id = '<source id>' order by checked_at desc limit 10"
docker compose exec -T worker sh -c 'ls -t /var/cache/cyberpulse/<source id>/ | head -3' </dev/null
docker compose exec -T worker sh -c 'f=$(ls -t /var/cache/cyberpulse/<source id>/*/*.raw | head -1); head -c 3000 "$f" | cat -v' </dev/null
```

**Fix.** A pull request that fixes the parser, with a fixture of the new shape in
`tests/fixtures/feeds/` and a test that fails on the old code. WHEELJACK does this when it runs;
TELETRAAN checks it; you merge. Then [deploy](deploy-and-rollback.md#a-normal-deploy).

**Stop and decide yourself** if the page now needs a login, a cookie wall or JavaScript to show its
items. Getting around that is a choice about the source, not a parser fix.

## stale-feed

**Symptom.** `stale-feed · <source id>` (low, not sent): its newest item has been older than its
expected frequency for 3 checks.

**Check.**

```bash
q "select id, name, expected_frequency from source_registry where id = '<source id>'"
```

Then open the publisher's page in a browser and see when they last posted.

**Fix.** Usually nothing: the publisher is quiet. If they post more rarely than
`expected_frequency` says, change it in `config/sources.yaml` by pull request.

**Stop and decide yourself** if a source has stopped publishing for good. Retiring it is your call.

## volume-collapse

**Symptom.** `volume-collapse` (high): "12 fresh events in 24 hours, against a usual 61". Fresh
events in a day fell under a quarter of the usual day. It only fires with at least 3 usual days and
a usual day of at least 8.

**Check.** Sources failing quietly, or deduplication merging too much:

An event counts as fresh when it was first published less than a week before the worker stored
it, so a back catalogue does not count. The query below counts the same way.

```bash
ops /ops/sources
q "select date_trunc('day', created_at) as day, count(*) from events
    where created_at > now() - interval '8 days'
      and first_seen >= created_at - interval '7 days' group by 1 order by 1"
docker compose exec -T worker python -m worker --check-duplicates </dev/null
docker compose exec -T worker python -m worker --check-lineage </dev/null
```

Both `--check-…` commands only read.

**Fix.** If many sources show errors, work through their `feed-failing` or `parser-drift`
incidents. If the duplicate check shows real, different events merged together, that is a
deduplication fault: a pull request with a test of the case.

**Stop and decide yourself** if it is a quiet news week. The incident clears as the usual day
moves.

## duplicate-explosion

**Symptom.** `duplicate-explosion` (high): "430 fresh events in 6 hours, against a usual 60 a
day". Over 5 times a usual quarter-day, and over 100.

**Check.**

```bash
q "select date_trunc('hour', created_at) as hour, count(*) from events
    where created_at > now() - interval '12 hours'
      and first_seen >= created_at - interval '7 days' group by 1 order by 1"
docker compose exec -T worker python -m worker --check-duplicates </dev/null
docker compose logs worker --since 6h | grep -E "done: ok=" | tail -10
```

**Fix.** Usually deduplication that stopped matching, or a new source that re-dates old items as
new. The fix is a pull request with a test of the case. Do not merge or delete events in the database by
hand.

**Stop and decide yourself** if it is a real flood, such as a major incident everyone is writing
about. Then it is news, not a fault.

## job-failing

**Symptom.** `job-failing · <job>`: "The <job> job has not completed a pass for 14 hours". High
for `groundtruth` and `enrichment`, otherwise medium (not sent).

| Job | Limit | Runs | By hand |
|---|---|---|---|
| `groundtruth` | 13 h | every 6 h at :25 UTC | `python -m worker --groundtruth` |
| `enrichment` | 75 min | :05 and :35 | `python -m worker --enrich` (spends, within the budget mode) |
| `source-gate` | 9 h | 03:50 UTC and every 4 h after | runs with the scheduler only |
| `discovery` | 50 h | 03:00 Sydney | runs with the scheduler only |
| `model-scan` | 50 h | 03:20 Sydney | `python -m worker --scan-models` |
| `model-gauntlet` | 15 days | Sundays 03:40 Sydney | `python -m worker --gauntlet` (spends, within its monthly cap) |

**Check.**

```bash
q "select distinct on (job) job, finished_at, completed, errors, note
     from job_runs order by job, finished_at desc"
q "select job, max(finished_at) as last_completed from job_runs where completed group by job"
docker compose logs worker --since 24h | grep -iE "<job>" | tail -30
```

`note` is the type of the exception, when a pass raised.

**Fix.**

- **`enrichment`:** the log shows `scheduled enrichment failed` or `scheduled MITRE suggestions
  failed`, with a traceback. A pass that calls nothing for want of budget still completes, so the
  budget never opens this incident. Missing summaries with no incident are in
  [AI budget](ai-budget.md).
- **A register or service is down** (the log names it): wait for the next pass.
- **The same exception every pass:** a code fault. A pull request, or pin the last good commit if
  it started with a merge ([Deploy and rollback](deploy-and-rollback.md)).
- To test a fix with one pass by hand, at a safe minute and not while the scheduled one runs:

  ```bash
  docker compose exec -T worker python -m worker --groundtruth </dev/null
  ```

  A pass by hand leaves no `job_runs` row. The incident clears after the next scheduled pass
  completes.

**Stop and decide yourself** before running a job that spends (`--enrich`, `--gauntlet`) when the
budget is already tight.

## publish-failure

**Symptom.** `publish-failure` (high): "Building the site's data has failed for 30 minutes".

**Check.**

```bash
q "select finished_at, completed, errors, note from job_runs
    where job = 'publish' order by finished_at desc limit 5"
docker compose logs worker --since 1h | grep -A 15 "publish after" | tail -40
df -h /
```

**Fix.**

- **The disk is full:** [disk full](services.md#disk-full).
- **It started with a merge:** pin the last good commit ([Deploy and rollback](deploy-and-rollback.md)).
- Once the cause is fixed, build once by hand to test it. This writes `data/*.json` and pushes
  nothing:

  ```bash
  docker compose exec -T worker python -m worker --publish </dev/null
  ```

  The incident clears after the next collection run publishes.

**Stop and decide yourself** if the builder fails on one event's data. Changing that event in the
database is a repair: backup first, and your decision.

## schema-drift

**Symptom.** `schema-drift` (high): "The site's data fails its schemas or the secret scan, so
nothing publishes". The publisher fails closed. The site keeps its last good data.

**Check.** The log names the file and the field, and what is wrong with it. For the secret scan
it names the kind of secret, not the match. A value that looks like a secret is redacted.

```bash
docker compose logs worker --since 1h | grep -E "schema validation|secret scan|publish blocked" | tail -10
ls -l .env
```

**Fix.**

- **"secret scan cannot read /app/.env":** the worker cannot read `.env`, so it cannot know what
  to look for. `ls -l .env` should show `-rw-------` and owner `oxygen`. If someone edited it with
  `sudo` and it now belongs to root, `sudo chown oxygen:oxygen .env`, then
  `docker compose up -d --force-recreate worker` at a safe minute.
- **A schema failure after a merge:** pin the last good commit, then fix the builder or the schema
  by pull request.
- **The scan found a secret pattern in the data:** find which event carries it. If it is **one of
  this system's own secrets**, treat it as leaked: rotate it now ([Tokens](tokens.md)), then
  find how it got into the data.

**Never loosen the scan** to get a publish through. Not a pattern, not the entropy check.

**Stop and decide yourself** about the event that carries the match. If it is a third party's key
quoted in a feed, the builder should drop or redact it, by pull request. Changing the event in the
database is a repair: backup first, and your decision.

## push-failure

**Symptom.** `push-failure` (high): "Pushing the site's data has failed for 45 minutes".

**Check.** A dry run does every check, reaches GitHub, and pushes nothing. Its errors are
redacted:

```bash
docker compose exec -T worker python -m worker.publish.push --dry-run </dev/null
q "select finished_at, completed, note from job_runs where job = 'push' order by finished_at desc limit 5"
docker compose logs worker --since 1h | grep -A 5 "push after" | tail -20
```

The dry run ends with `dry run: would push …` when all is well, or `push failed: …`.

Also look at [githubstatus.com](https://www.githubstatus.com).

**Fix.**

- **Authentication failed, or 403:** the publish token has expired (90 days) or was revoked.
  [Rotate it](tokens.md#publish-token).
- **GitHub is down:** wait. The next run pushes everything at once.

A worker with no publish token at all does not push, so it raises no `push-failure`. Its start-up
log says `no publish token: data/ is written here and not pushed`. To check the name is in `.env`
without showing its value: `grep -c '^CYBERPULSE_PUBLISH_TOKEN=.' .env` prints 1.

**Stop and decide yourself** if the dry run passes and the real push still fails. Look at the
token's permissions on GitHub before anything else, and never add permissions to make it work.

## site-stale

**Symptom.** `site-stale` (high): "The public site's data is 4 hours old", or it "could not be read
3 times in a row". The worker publishes, but the site does not change. Only the host that pushes
runs this check.

**Check.**

```bash
curl -fsS "https://happycode0.github.io/CyberPulse-AI/data/system-status.json?x=$(date +%s)" \
  | python3 -c 'import json, sys; print(json.load(sys.stdin)["generated_at"])'
git ls-remote https://github.com/happycode0/CyberPulse-AI.git refs/heads/data
```

Then on GitHub: **Actions → pages**. Look for failed runs, and for a banner saying the scheduled
workflow was disabled.

**Fix.**

- **The `pages` runs fail:** open the latest one and read the failing step. A GitHub outage passes
  on its own.
- **The schedule was disabled:** GitHub does this to scheduled workflows in public repositories
  after a long quiet spell. Enable it from the banner.
- **To rebuild now:** **Actions → pages → Run workflow** on `main`.

Pushes to `data` do not start the workflow; only its 15-minute schedule does.

**Stop and decide yourself** if the deploy fails on a permissions error. Do not widen the
workflow's `permissions:` block without a review.

## cost-anomaly

**Symptom.** `cost-anomaly · rate` (high): "AI spend in 24 hours is $2.40, against a usual
$0.30". Or `cost-anomaly · budget` (high): "AI spend this month is $18.20 of the $20.00 budget".

**Check, fix, stop:** [AI budget](ai-budget.md).

## paperclip-down

**Symptom.** `paperclip-down` (high): "Paperclip has not answered 3 checks in a row". The crew is
not told, since there is no crew to tell. The worker keeps collecting and publishing.

**Check, fix, stop:** [Paperclip down](services.md#paperclip-down).

---

## The circuit breaker tripped

**Symptom.** `CyberPulse-AI · incident · needs a human`: three fixes failed TELETRAAN's tests. The crew
has stopped work on that incident, and the ops API takes no more verdicts on it (409).

**Check.**

```bash
ops /ops/incidents/42              # needs_human, fix_failures, and each verdict's reasons
```

Read the three pull requests and TELETRAAN's reasons on GitHub.

**Fix.** There is no reset, by design. The count survives a reopen. Either fix the fault yourself,
by pull request, or close the `[ENGINEERING]` issue in Paperclip and live with the fault. The
incident resolves on its own once the watchdog no longer sees the fault.

**Stop and decide yourself:** this whole entry is yours. Also ask why three fixes failed: a
wrong diagnosis, a fault the tests cannot show, or a prompt being steered.

---

[Runbooks](README.md) · [Services](services.md) · [AI budget](ai-budget.md)
