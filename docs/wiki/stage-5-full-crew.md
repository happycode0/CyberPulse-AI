# Stage 5 — Full crew + follow-up + notifications

[← 4c — How the crew works](stage-4c-how-the-crew-works.md) · [Wiki home](README.md) ·
[Stage 6 — Self-healing →](stage-6-self-healing.md)

**Status: ⬜ not started.** Plan: [PLAN.md §9, Stage 5](../../PLAN.md#9-stages)

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
| 🔴 | **A Telegram bot.** Create it with @BotFather, and put `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` in the VM's `.env` |
| 🔴 | **Approve each un-pause**, as the board: agent page → **Resume agent**, then resume its routine |
| 🟢 | Source discovery with Tavily, behind SERAPH's test gate (the finder can never activate) |
| 🟢 | RIPPERDOC's gauntlet and golden set (PLAN.md §7.6) |
| 🟢 | The follow-up graph and event status changes; the daily intelligence report |
| 🟢 | Telegram notifications; the public **THE CREW** page |

## Done when

- [ ] A source is discovered, validated and activated without human action
- [ ] A developing event builds up real timeline entries
- [ ] RIPPERDOC proposes a model-ladder change with gauntlet evidence attached
- [ ] The daily digest arrives in Telegram

---

[← 4c](stage-4c-how-the-crew-works.md) · [Wiki home](README.md) ·
**Next:** [Stage 6 — Self-healing →](stage-6-self-healing.md)
