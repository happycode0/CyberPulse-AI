# Stage 5 — Full crew + follow-up + notifications

[← 4c — How the crew works](stage-4c-how-the-crew-works.md) · [Wiki home](README.md) ·
[Stage 6 — Self-healing →](stage-6-self-healing.md)

**Status: ▶ in progress.** Telegram notifications, THE CREW's published workload, event status,
DECKARD's follow-up queue, source discovery and RIPPERDOC's model scout are built. Plan:
[PLAN.md §9, Stage 5](../../PLAN.md#9-stages)

---

## What this stage gives you

The rest of the newsroom. Global and AI desks join the Australian one, an editor checks quality,
developing stories get followed up, new sources are found and tested without you, the model
ladder is reviewed with evidence, and a daily digest lands in Telegram.

## Switched on in this stage

The six agents you created **paused** in [4a step 6](stage-4a-paperclip-setup.md#6--create-the-16-agents),
and their routines from [4a step 7](stage-4a-paperclip-setup.md#7--create-the-routines):

| Agent | Does | Routine (Sydney time) |
|---|---|---|
| BLASTER | Global cyber desk | `[DIGEST] Global desk`, every 4 h at :10 |
| WINTERMUTE | AI intelligence desk | `[DIGEST] AI desk`, every 4 h at :20 |
| TACHIKOMA | Finds new sources | `[DISCOVERY] Nightly`, 03:00 |
| DECKARD | Follows developing events | `[FOLLOW-UP] Sweep`, every 6 h at :45 |
| VOIGHT | Editorial QA | `[QA] Daily sample`, 07:00 |
| RIPPERDOC | Model scout | `[MODEL] Daily scan` 04:00 · `[MODEL] Weekly gauntlet` Sunday 05:00 |

## What's left

| Who | Step |
|---|---|
| 🔴 | **A Telegram bot**: [the steps below](#telegram) |
| 🔴 | **Paste DECKARD's new instructions**: [the steps below](#follow-up-and-status) |
| 🔴 | **A Tavily key, and TACHIKOMA's new instructions**: [the steps below](#source-discovery) |
| 🔴 | **Paste RIPPERDOC's new instructions, and move its gauntlet routine to 05:00**: [the steps below](#model-scout) |
| 🔴 | **Approve each un-pause**, as the board: agent page → **Resume agent**, then resume its routine |
| ✅ | RIPPERDOC's daily model scan, weekly gauntlet and golden set: [below](#model-scout) |
| ✅ | Source discovery with Tavily, behind SERAPH's gate (the finder can never activate): [below](#source-discovery) |
| ✅ | Event status from the record, and DECKARD's follow-up queue: [below](#follow-up-and-status) |
| ✅ | Telegram notifications: the daily digest, critical alerts and developing updates for Australia |
| ✅ | The public **THE CREW** page shows the workload the worker can vouch for (`data/crew.json`) |

## Telegram

The worker sends three kinds of message, and nothing else, to one chat:

| Message | When | What |
|---|---|---|
| **Daily digest** | 07:10 Sydney time, after the 07:00 collection (08:10 and 09:10 try again only if 07:10 failed) | The last 24 hours: new, updated and merged events, the five most prominent, the events to watch, sources that changed status, collection runs, the month's AI spend |
| **Critical alert for Australia** | Within 3 minutes of the hourly collection (:03 UTC), and again at :18 and :48, 13 minutes after each enrichment pass, which can raise an event's Australian relevance | A new event that is critical or carries a CISA KEV-listed CVE, and that has Australian relevance 0.7 or more or was reported in Australia. At most 5 per pass; the rest follow at the next pass, 15 to 30 minutes later |
| **Developing update for Australia** | On the same passes | A material change on an event that already alerted, or could have: it is critical or KEV-listed and Australian. A new Australian exposure on a high or critical event counts too. Only changes recorded at least an hour after the event was first seen, so its alert goes first. At most 5 per pass |

Each message is sent once: the `notifications` table records it before it goes. A message is
plain text, passes the same secret scan as a publish, and is withheld if anything is found. The
bot token is never written to a log: it is part of every Telegram URL, so the worker rewrites
it out of the HTTP client's log lines.

🔴 **To switch it on:**

1. In Telegram, message **@BotFather** → `/newbot` → pick a name → copy the token it gives you.
2. Send your new bot any message (say "hi").
3. On the VM, put the token in `~/CyberPulse-AI/.env` as `TELEGRAM_BOT_TOKEN=` (edit with
   `nano .env`; don't paste it into a chat or a terminal history).
4. Find your chat id. This reads the token from `.env` without printing it:

   ```bash
   cd ~/CyberPulse-AI
   T=$(grep '^TELEGRAM_BOT_TOKEN=' .env | cut -d= -f2-)
   curl -s "https://api.telegram.org/bot${T}/getUpdates" \
     | python3 -c 'import json,sys; print({u["message"]["chat"]["id"] for u in json.load(sys.stdin)["result"] if "message" in u})'
   unset T
   ```

5. Put the number it prints in `.env` as `TELEGRAM_CHAT_ID=`.
6. `docker compose up -d --force-recreate worker` (a plain restart keeps the old, empty values),
   then `docker compose logs worker | grep -i telegram` should say
   **Telegram notifications are on**.

The next 07:10 brings the first digest. To see what was sent:
`docker compose logs worker | grep notified`.

## Follow-up and status

The worker sets every event's status itself at the end of each collection run, from the
event's record (`config/followup.yaml` holds the numbers):

| Status | When |
|---|---|
| `new` | One report and nothing since |
| `active` | Confirmed by an independent source |
| `developing` | A material change (exploitation, a proof-of-concept, a new target, actor or Australian exposure, ...) in the last 3 days |
| `monitoring` | Quiet for 3 days after that |
| `contained` | A patch or mitigation, and nothing material since it (or quiet again for 3 days) |
| `resolved` | Patched or mitigated, then quiet for 14 days |
| `archived` | Gone quiet for good, or merged into another event (as before) |

A status is worked out afresh each time, not stepped, so a missed run puts nothing out of place.

**DECKARD's queue.** Right after the statuses, the worker opens a follow-up task for each
developing or monitoring event that is medium or above. The more severe it is, the sooner the
task is due: a developing critical every 6 hours, a monitoring medium every 4 days. A resolved
critical or high event gets one task for its closing summary. DECKARD reads up to 5 due tasks
from `GET /ops/followup` and posts one report per task to `POST /ops/followup/<task>`.

The worker treats each report as hostile. It refuses a change unless:

- it rests on one `https` page on a named host;
- its summary is short, with no link and no CVE the event doesn't already name;
- the event doesn't already say the same thing.

What passes goes on the event's timeline with DECKARD and the link as its source. That is what
moves the status. A task refused three times is given up, and the next check comes round on
its cadence. A resolved event's closing summary appears on the site under **RESOLUTION**.

🔴 **To switch DECKARD on:**

1. In Paperclip, open **DECKARD** → **Instructions**. Replace everything with the house rules
   and then DECKARD's text, both from [4b §12](stage-4b-the-crew.md#12-deckard--follow-up--developing-events-paused-until-stage-5).
2. Open the **Follow-up** routine. Change the issue text to
   `Work the follow-up queue: report on each task due.`
3. Resume DECKARD, then the routine. To see the queue it will get, run this on the VM:
   `docker compose logs worker | grep follow-up`.

## Source discovery

New sites reach the collection in four steps. Only the worker fetches a found site, and nothing
a site says can move it through the gate faster:

| Step | When | What |
|---|---|---|
| **Search** | 03:10 Sydney time | The worker runs the queries in `config/discovery.yaml` through Tavily, up to 30 credits a day (the free tier is 1,000 a month). Each search is pinned to one credit. Only the links, titles and dates are kept. Each site the results name that isn't registered and isn't a platform (social media, video, a blog host) becomes a *find* |
| **Proposals** | TACHIKOMA's nightly routine | TACHIKOMA reads the finds from `GET /ops/candidates` and proposes feeds and sites with `POST /ops/candidates`, at most 20 a day. The worker checks each one: `https` on port 443, a public host, a reason with no links, examples on the same site |
| **The gate** | Every 4 hours, 10 minutes before each normal collection | For a find with no feed, the worker reads its home page for an advertised feed and then tries the usual places (`/feed/`, `/rss`, ...): three tries a week apart, then it is rejected. A candidate's feed is fetched once a pass and judged. It must have at least 3 items dated in the last 14 days, 2 of them on the beat, and at most 60% already collected from another source. 6 healthy probes in a row activate it; 3 failures in a row reject it. A probe stores nothing |
| **Collection** | Each normal run | An activated site is collected as source class `discovered`. Its items are *community* evidence, which never confirms an event on its own. At most 25 at once. One with no healthy fetch in 21 days is retired. `rejected` and `retired` are final, so the site is never proposed again |

Every request to a found site goes through the worker's URL guard. It allows only `https` on
port 443, with no user name or password in the URL. The host must resolve to public addresses
only, checked before every request and every redirect (at most 5). A reply is read up to a size
cap, counted after decompression. The Tavily key stays in the worker's `.env` and never goes to
Paperclip. Names and titles from the open web reach the agents as evidence, never as
instructions. Each activation sends one Telegram message, if Telegram is on.

🔴 **To switch it on:**

1. Sign in at [app.tavily.com](https://app.tavily.com) (the free plan) and copy your API key.
2. On the VM, put it in `~/CyberPulse-AI/.env` as `TAVILY_API_KEY=` (with `nano .env`; don't
   paste it into a chat or a terminal history). Then
   `docker compose up -d --force-recreate worker`.
3. In Paperclip, open **TACHIKOMA** → **Instructions**. Replace everything with the house rules
   and then TACHIKOMA's text, both from [4b §11](stage-4b-the-crew.md#11-tachikoma--source-discovery-paused-until-stage-5).
4. Open the **Source discovery** routine. Change the issue text to
   `Read the worker's discovery finds and propose sources, plus any open [GAP] topics.`
5. Resume TACHIKOMA, then the routine.

After the next 03:10, `docker compose logs worker | grep discovery` shows the night's searches,
and `grep "source gate"` shows each gate pass. Without a key the search is skipped and says so;
the gate still tests TACHIKOMA's proposals.

## Model scout

The worker keeps the model ladder (`config/models.yaml`) honest and RIPPERDOC reports on it.
The worker holds the OpenRouter key and makes every call; RIPPERDOC only reads `GET /ops/models`.

| Step | When | What |
|---|---|---|
| **Scan** | 03:20 Sydney time, daily | The worker reads OpenRouter's model list (no key needed) and records what changed since yesterday: new models, withdrawn ones, price changes and withdrawal dates. New `:free` models are logged loudly. Then every ladder model goes through the price guard again. One that no longer passes leaves its chain from the next enrichment pass, so a price rise goes unnoticed for a day at most. If what is left is not a usable ladder, the AI layer stays off until it is |
| **Gauntlet** | 03:40 Sydney time, Sundays | Each tier's default and up to two challengers answer the golden set. Tier 0 is judged on triage, tier 1 on the brief and tier 2 on the severity judgment, each through the price guard and the tier's own request rules. Per model: schema compliance, refusals, agreement with the golden labels, production's checks, p95 latency and the cost OpenRouter billed. A challenger is proposed only when it clears every gate and is free, agrees clearly more, or costs at least 20% less per event while agreeing as well |
| **Proposal** | RIPPERDOC's Sunday 05:00 routine | Each proposal is an `agent_proposals` row with a side-by-side table. RIPPERDOC opens it as a `[MODEL] Proposal` issue for ROGUE and MORPHEUS. Approving changes nothing by itself: promoting a model is a reviewed edit to `config/models.yaml` |

**The golden set** is 30 events, pinned the first time the gauntlet runs, spread across
severities: 9 critical, 9 high, 9 medium and 3 low. Each has a severity from a register (the CNA,
CISA's ADP, NVD or the vendor), names a CVE, and has enough of the feed's own text to work from.
The record a model is shown is frozen when the event is pinned, and every result names the set's
digest. To pin it again by hand, run `docker compose exec -T worker python -m worker --pin-golden-set`.

**What it costs.** The gauntlet spends at most US$0.25 a month and US$0.08 a run, counted from
the cost ledger. A model is tried only if its estimate fits what is left. The incumbent's result
is reused for 28 days on the same set, so most Sundays only the challengers are paid for. Free
models are tried only while at least 100 of the day's free requests would be left for
enrichment. An HTTP 402 from OpenRouter stops the run. The scan costs nothing.

**Not checked yet**, and every proposal says so:

- **The adoption veto** (PLAN.md §7.6): a model whose use fell 40% or more week on week. It needs
  OpenRouter's authenticated datasets, so check [the rankings](https://openrouter.ai/rankings)
  before approving.
- **The labels are not reviewed.** They come from the registers and the source registry, not
  from a person.
- **Few Australian events.** Tier 1's brief is scored on AU relevance only, and the only events
  labelled Australian are ones an Australian advisory carried: 2 of the 538 that qualified
  when the set was first pinned. Up to 2 per severity are taken first, and every tier 1
  proposal says how many the score rests on.
- **The `code` and `audit` tiers** (WHEELJACK and TRON) do no enrichment, so the gauntlet has
  nothing to judge them on. Their models are still checked by the daily scan.

🔴 **To switch it on:**

1. In Paperclip, open **RIPPERDOC** → **Instructions**. Replace everything with the house rules
   and then RIPPERDOC's text, both from [4b §14](stage-4b-the-crew.md#14-ripperdoc--model-scout-paused-until-stage-5).
2. Open the **Model scan** routine. Change the issue text to
   `Do the daily model scan: read /ops/models and report what changed.`
3. Open the **Model gauntlet** routine. Change the cron expression to `0 5 * * 0`, so it runs
   after the worker's 03:40 gauntlet, and the issue text to
   `Do the weekly gauntlet: report the results and raise each new proposal.`
4. Resume RIPPERDOC, then both routines.

After the next 03:20, `docker compose logs worker | grep "model scan"` shows the night's scan, and
after a Sunday, `grep gauntlet` shows the run or why it was skipped. The gauntlet follows the
worker's budget mode (PLAN.md §7.4): it tries tier 2 only in `full`, tier 1 down to `conserve`,
and only free models below that. With the budget off, or the OpenRouter key's limit spent, it
is skipped and says so; the scan runs anyway.

## THE CREW page

`data/crew.json` lists only what the worker itself does and can count from its own rows:
LIBRARIAN (ground-truth passes), PROWL (collection runs and items), SERAPH (source checks), ROGUE
(model calls on the ledger) and LINK (publishing), plus the pipeline's own AI calls and spend
this month. The AI agents work in Paperclip, which the worker never reads, so their cards say
**NOT PUBLISHED** rather than showing numbers nobody measured.

## Done when

- [ ] A source is discovered, validated and activated without human action
- [ ] A developing event builds up real timeline entries (DECKARD's, with their links)
- [ ] RIPPERDOC proposes a model-ladder change with gauntlet evidence attached
- [ ] The daily digest arrives in Telegram

---

[← 4c](stage-4c-how-the-crew-works.md) · [Wiki home](README.md) ·
**Next:** [Stage 6 — Self-healing →](stage-6-self-healing.md)
