# Stage 6 — Self-healing

[← Stage 5 — Full crew](stage-5-full-crew.md) · [Wiki home](README.md) ·
[Stage 7 — Hardening →](stage-7-hardening.md)

**Status: ▶ built, waiting for you.** The watchdog, its incidents, TRON's verdicts, the circuit
breaker and rollback are built and running on the VM. The crew's part waits for your tokens,
branch protection and the Incident routine. Plan: [PLAN.md §9, Stage 6](../../PLAN.md#9-stages) ·
How the loop runs: [4c, the repair loop](stage-4c-how-the-crew-works.md#6-repair-loop--stage-6)

---

## What this stage gives you

When a source breaks (a changed feed, a parser that stops matching), the crew notices, finds the
cause, writes a fix with a test, and has a second agent check it. **Then you approve the merge.**
No code reaches production without you.

## Switched on in this stage

| Agent | Does | Model |
|---|---|---|
| TELETRAAN | Reads each incident the watchdog opens, finds the cause, hands it to its owner | CHEAP tier |
| WHEELJACK | Writes the fix on a branch, with a regression test, and opens a PR | CODE tier |
| TRON | Independently verifies it: PASS or FAIL, recorded against the incident | AUDIT tier, **a different vendor** from WHEELJACK's |

## What's left

| Who | Step |
|---|---|
| 🔴 | **A second fine-grained token**, `CYBERPULSE_ENGINEER_TOKEN`: this repo only, 90-day expiry, **Contents** and **Pull requests** read and write, nothing else. It is the only credential WHEELJACK ever gets |
| 🔴 | **Branch protection on `main`:** require a pull request and your review before merging |
| 🔴 | **The Incident routine and its webhook**: [the steps below](#the-incident-routine) |
| 🔴 | **Paste the new instructions** for TELETRAAN, WHEELJACK and TRON from [4b](stage-4b-the-crew.md#2-teletraan--watchdog--sre): each agent's page → **Instructions** → replace the text below the house rules |
| 🔴 | **Approve the un-pause** of TELETRAAN, WHEELJACK and TRON, as the board |
| ✅ | The watchdog: 14 signatures, checked every 5 minutes for nothing: [below](#the-watchdog) |
| ✅ | Incidents that open, update, resolve and reopen on their own, told to Telegram and to the crew: [below](#incidents) |
| ✅ | TRON's verdicts and the circuit breaker: 3 failed fixes stop the crew and wait for you: [below](#the-circuit-breaker) |
| ✅ | Rollback, `ops/rollback.sh`: [below](#rollback) |

## The watchdog

Every five minutes (at 2, 7, 12 … minutes past the hour, UTC) the worker checks the system
against fixed signatures. The checks are queries and two HTTP requests: no model, no cost. A
signature seen opens an **incident**.

| Signature | Seen when | Severity |
|---|---|---|
| `no-collection` | A lane has finished no run for 35 minutes (fast) or 4 h 35 min (normal) | critical (fast), high (normal) |
| `zero-volume` | A lane's last 3 fast or 2 normal runs, over at least 30 minutes, fetched nothing | high |
| `feed-failing` | A feed's last 5 checks failed. The evidence lists its same-host siblings, so a publisher that is down shows as one | high for a priority-1 source, else medium |
| `parser-drift` | A feed answers, but its last 3 checks found no items | high |
| `stale-feed` | A feed's newest item has been older than expected for 3 checks | low |
| `volume-collapse` | Fresh events in 24 hours are under a quarter of a usual day (the median of up to 7 days) | high |
| `duplicate-explosion` | Fresh events in 6 hours are over 5 times a usual quarter-day, and over 100 | high |
| `job-failing` | A scheduled job has not completed a pass within its limit (13 h for ground truth, 75 min for enrichment …) | high for ground truth and enrichment, else medium |
| `publish-failure` | Building the site's data has failed for 30 minutes | high |
| `schema-drift` | The site's data fails its schemas or the secret scan, so nothing publishes | high |
| `push-failure` | Pushing the data branch has failed for 45 minutes | high |
| `site-stale` | The public site's data is over 3 hours old, or could not be read 3 times in a row | high |
| `cost-anomaly` | AI spend in 24 hours is over 3 times a usual day and over US$1, or the month has reached 90% of the budget | high |
| `paperclip-down` | Paperclip's health endpoint has not answered 3 checks in a row | high |

"Fresh" events are those first published less than a week before the worker saw them, so a new
source's back catalogue is not read as a flood. The site check runs only on the host that
pushes. A part the watchdog cannot read (a query that fails, a probe that blips) leaves its
incidents as they are: nothing is cleared because nothing was seen.

## Incidents

- **One per fault.** An incident is a signature and its subject: a lane, a source or a job.
  Seen again, the same incident is updated. Its evidence is figures and short statuses, never
  feed content.
- **Resolved by the watchdog.** Once its fault has been gone for 15 minutes, it resolves.
  Nobody closes one by hand, and no agent can.
- **Reopened, not reborn.** A fault back within 6 hours reopens its old incident, with its
  count of failed fixes and its breaker as they were.
- **Told once.** A high or critical incident goes to Telegram when it opens or reopens, and
  again when it resolves. It also fires the Incident routine, so TELETRAAN files it. Each
  notice is recorded in `notifications` before it goes. An incident that keeps reopening is
  announced 3 times, then waits in the list. Medium and low ones only wait in the list.
- **When the database is down**, nothing can be judged. After 3 passes like that, Telegram is
  told once, and again when it is back.

The crew reads them from the ops API: `GET /ops/incidents` (unresolved), `?status=all` (and
those resolved in the last 14 days) and `GET /ops/incidents/<id>`. Each comes with a guide to
its kind: what it means and who fixes it.

```bash
# On the VM: what the watchdog has open, and its last passes
docker compose logs worker --since 1h | grep -E "incident|watchdog"
```

## The Incident routine

The watchdog fires this routine through a webhook trigger. Each fire opens a TELETRAAN issue,
or joins the one already open. The payload holds only the incident's number, kind, subject,
severity and title; TELETRAAN reads the rest from the ops API, so no evidence passes through
Paperclip.

1. **Routines → New routine**:

   | Field | Value |
   |---|---|
   | Name | `Incident` |
   | Assignee | TELETRAAN |
   | Project | `cyberpulse-ai` |
   | Issue title | `[INCIDENT] Watchdog` |
   | Description | `An incident is open. GET /ops/incidents and follow your instructions.` |
   | Concurrency | `coalesce_if_active`: a second incident while the issue is open joins it |
   | Catch-up | `skip_missed` |

2. **Add trigger → Webhook**, signing mode **bearer**. Paperclip shows the webhook URL and its
   secret **once**. Leave the banner open.
3. On the VM, add both to `~/CyberPulse-AI/.env`:

   ```bash
   PAPERCLIP_INCIDENT_WEBHOOK_URL=http://server:3100/api/routine-triggers/public/<the id in the URL>/fire
   PAPERCLIP_INCIDENT_WEBHOOK_SECRET=<the secret>
   ```

   Keep the `/api/routine-triggers/public/…/fire` part of the URL Paperclip showed, and put
   `http://server:3100` in front of it: that is how the worker reaches Paperclip on the compose
   network.
4. `docker compose up -d --force-recreate worker` (a restart keeps the old values).
5. The worker's log says `incidents go to the Incident routine in Paperclip` when it starts.
6. **Keep the routine active.** Paperclip refuses a fire for a paused routine. With TELETRAAN
   paused, the issue simply waits for it.

If a fire fails, the worker logs `paperclip:incident:<n>: failed on attempt <k>: <reason>` and
tries twice more on later passes:

| Reason | Meaning | Fix |
|---|---|---|
| `HTTP 401` | The secret is wrong | **Rotate secret** on the trigger card, and put the new one in `.env` |
| `HTTP 403` | The routine or trigger is paused, or Paperclip refused the hostname `server` | Turn the routine on. If it is on, add `server` to Paperclip's allowed hostnames (`PAPERCLIP_ALLOWED_HOSTNAMES`, comma-separated) in the server's environment, then recreate the server |
| `HTTP 404` | The trigger id is wrong | Copy the URL again |
| `ConnectError` | The worker cannot reach that address | Use `http://server:3100` as in step 3 |

The watchdog probes Paperclip's health endpoint (`WATCHDOG_PAPERCLIP_URL`, by default
`http://server:3100/api/health`) with no credentials. Any answer below 500, a 403 included,
counts as up. Set it empty in `.env` to stop the probe.

## The circuit breaker

TRON records each verdict against the incident: `POST /ops/incidents/<id>/verdict` with
`{"verdict": "pass" | "fail", "pr": <number>, "reasons": "<its own words>"}`. The reasons pass
the same secret scan as a publish.

- Each **fail** counts. At the **third**, the incident needs a human: Telegram is told, and
  `GET /ops/incidents` shows `needs_human`. TELETRAAN and WHEELJACK stop on it, and the API
  takes no more verdicts (409).
- A pass changes nothing by itself: **you** still review and merge the pull request, and the
  incident resolves when the watchdog no longer sees the fault.
- The count survives a reopen, so a fix that only hides the fault for an hour cannot reset it.
- After the breaker, you decide: fix it yourself, or close the `[ENGINEERING]` issue.

## Rollback

Every merge reaches production by `git pull` on the VM, so a merge can be undone the same way.
`ops/rollback.sh` does it:

```bash
cd ~/CyberPulse-AI
./ops/rollback.sh status              # what is running, against main's tip
./ops/rollback.sh pin <sha>           # run an earlier commit of main now, restart the worker
./ops/rollback.sh unpin               # back to main's tip: pull, migrate, restart
./ops/rollback.sh revert <merge-sha>  # where you push from: a PR that undoes one merge
```

`pin` is the fast way out; `revert` is the lasting fix, merged like any other. Migrations only
ever add, so an earlier commit runs on the newer schema and the database is never rolled back.
Each restart comes between collection runs: keep clear of :00, :15, :30 and :45 UTC.

## Done when

- [ ] **The drill.** A deliberately broken parser is detected, diagnosed, fixed on a branch,
      verified independently, and merged after your approval. On a test branch, change a
      feed's parser so it finds nothing. Within about 3 normal runs `parser-drift` opens,
      Telegram is told and TELETRAAN files an `[ENGINEERING] INC-<n>` issue. WHEELJACK opens
      `fix/inc-<n>-…`, TRON records PASS, you merge, and the incident resolves 15 minutes
      after the next good check.
- [ ] **The breaker.** Three FAIL verdicts on one incident trip the breaker: the third reply
      says `tripped`, a fourth is refused with 409, and the incident shows `needs_human`.

---

[← Stage 5](stage-5-full-crew.md) · [Wiki home](README.md) ·
**Next:** [Stage 7 — Hardening + operations →](stage-7-hardening.md)
