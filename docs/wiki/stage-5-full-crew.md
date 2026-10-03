# Stage 5 — Full crew + follow-up + notifications

[← 4c — How the crew works](stage-4c-how-the-crew-works.md) · [Wiki home](README.md) ·
[Stage 6 — Self-healing →](stage-6-self-healing.md)

**Status: ▶ in progress.** Telegram notifications, THE CREW's published workload, event status,
DECKARD's follow-up queue and source discovery are built. Plan:
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
| RIPPERDOC | Model scout | `[MODEL] Daily scan` 04:00 · `[MODEL] Weekly gauntlet` Sunday 04:00 |

## What's left

| Who | Step |
|---|---|
| 🔴 | **A Telegram bot**: [the steps below](#telegram) |
| 🔴 | **Paste DECKARD's new instructions**: [the steps below](#follow-up-and-status) |
| 🔴 | **A Tavily key, and TACHIKOMA's new instructions**: [the steps below](#source-discovery) |
| 🔴 | **Approve each un-pause**, as the board: agent page → **Resume agent**, then resume its routine |
| 🟢 | RIPPERDOC's gauntlet and golden set (PLAN.md §7.6) |
| ✅ | Source discovery with Tavily, behind SERAPH's gate (the finder can never activate): [below](#source-discovery) |
| ✅ | Event status from the record, and DECKARD's follow-up queue: [below](#follow-up-and-status) |
| ✅ | Telegram notifications: the daily digest, critical alerts and developing updates for Australia |
| ✅ | The public **THE CREW** page shows the workload the worker can vouch for (`data/crew.json`) |

## Telegram

The worker sends three kinds of message, and nothing else, to one chat:

| Message | When | What |
|---|---|---|
| **Daily digest** | 07:00 Sydney time (08:00 and 09:00 try again only if 07:00 failed) | The last 24 hours: new, updated and merged events, the five most prominent, the events to watch, sources that changed status, collection runs, the month's AI spend |
| **Critical alert for Australia** | Within 3 minutes of collection (every 15 minutes) | A new event that is critical or carries a CISA KEV-listed CVE, and that has Australian relevance 0.7 or more or was reported in Australia. At most 5 per pass; the rest follow 15 minutes later |
| **Developing update for Australia** | On the same 15-minute pass | A material change on an event that already alerted, or could have: it is critical or KEV-listed and Australian. A new Australian exposure on a high or critical event counts too. Only changes recorded at least an hour after the event was first seen, so its alert goes first. At most 5 per pass |

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

The next 07:00 brings the first digest. To see what was sent:
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
| **Search** | 03:00 Sydney time | The worker runs the queries in `config/discovery.yaml` through Tavily, up to 30 credits a day (the free tier is 1,000 a month). Each search is pinned to one credit. Only the links, titles and dates are kept. Each site the results name that isn't registered and isn't a platform (social media, video, a blog host) becomes a *find* |
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

After the next 03:00, `docker compose logs worker | grep discovery` shows the night's searches,
and `grep "source gate"` shows each gate pass. Without a key the search is skipped and says so;
the gate still tests TACHIKOMA's proposals.

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
