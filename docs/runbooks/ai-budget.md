# AI budget

[Runbooks](README.md) · [Watchdog incidents](watchdog-incidents.md#cost-anomaly) ·
[4c, the budget](../wiki/stage-4c-how-the-crew-works.md) · [Threat model, B3](../threat-model.md#b3-worker-to-openrouter-and-tavily)

One OpenRouter key pays for everything: the worker's enrichment and every AI agent in Paperclip.
Spend stops at four layers, from the inside out:

| Layer | Set in | At the limit |
|---|---|---|
| The worker's budget modes | `AI_MONTHLY_BUDGET_USD` in `.env` (US$20) | The worker steps down to cheaper models, then free ones (below) |
| Each agent's monthly budget | Paperclip, per agent. They add up to US$11.50 | The agent shows "Budget paused" |
| The company budget | Paperclip, US$12 | Paperclip's second stop for the agents |
| The key's hard limit | OpenRouter → **Keys**, US$20 a month | OpenRouter refuses paid calls. **This is the last stop**, for the worker and the agents alike |

The worker sets its mode from OpenRouter's own account of the key (`GET /api/v1/key`), before
each spend. That is the key's total, so **the agents' spend counts against the worker too**. Busy
agents can push enrichment down to free models.

| Mode | When | What still gets enriched |
|---|---|---|
| `full` | Over 50% of the month's budget left | Everything, on the whole ladder |
| `conserve` | 20 to 50% left | Everything; the strong tier only for critical and KEV-linked events |
| `minimal` | Under 20% left | Only critical, high, KEV-linked and developing events, on the cheapest tier |
| `free_only` | Nothing left; no reading in the last 30 minutes; or OpenRouter said the key's limit is spent | The same events as `minimal`, on `:free` models only |
| `off` | `AI_MONTHLY_BUDGET_USD=0`, or OpenRouter said the **account** is out of credits | Nothing. Events wait as `pending_enrichment` |

Collection, ground truth and publishing never stop for the budget. Only summaries and AI
estimates wait.

---

## Spend is high, or the budget is nearly gone

**Symptom.** Any of:

- `cost-anomaly · rate` (high): "AI spend in 24 hours is $2.40, against a usual $0.30".
- `cost-anomaly · budget` (high): "AI spend this month is $18.20 of the $20.00 budget".
- An agent shows **Budget paused** in Paperclip.
- New events on the site have no summary, and `pending_enrichment` keeps growing.

`cost-anomaly` counts **only the worker's own calls**, from its `cost_ledger`. Spend by the
agents never opens it. Look at OpenRouter for the whole picture.

**Check.**

```bash
docker compose exec -T worker python -m worker --check-budget </dev/null    # spends nothing
ops /ops/cost
q "select count(*) from events where pending_enrichment"
q "select date_trunc('day', ts) as day, model, round(sum(cost_usd), 4) as usd
     from cost_ledger where ts > now() - interval '3 days'
     group by 1, 2 order by 1, 3 desc nulls last"
docker compose logs worker --since 6h | grep -E "enrichment: mode|AI stopped|budget" | tail -20
```

`--check-budget` reads the key fresh and logs one line: `budget: mode …, <why>; spent $… today
and $… this month; key limit $20.00 (…), $… left`. That spend is the **key's**, agents included.
It does not know what the running worker has been told since its last reading. The worker's own
mode is in its log, once per enrichment pass: `enrichment: mode conserve (50% or less of the budget
left); …`.

Then, in a browser:

- **OpenRouter → Keys:** this key's usage this month, and its limit.
- **OpenRouter → Activity:** which models, and when.
- **Paperclip → Budgets:** which agent spent what.

Compare the key's spend this month with the worker's ledger for the month. **The difference is the
agents.** If the agents' figures in Paperclip do not account for it either, something else is using
the key.

**Fix.**

- **The worker spends more than usual:** the ledger shows the model and stage. A burst after many
  new events, such as a big incident in the news, passes on its own. A loop, or one model costing
  far more than the others, is a code or ladder fault: a pull request. The model scout checks the
  ladder's prices each night.
- **An agent spends more than usual:** pause that agent in Paperclip, then read its recent runs.
- **To stop all AI spend now,** do both of these. Pausing the company in Paperclip stops only the
  agents. The worker's enrichment goes on until its own budget is set to zero:
  1. In Paperclip, pause the company.
  2. On the VM, set the worker's budget to `0` with an editor (`nano .env`, the line
     `AI_MONTHLY_BUDGET_USD=0`). Then, at a safe minute:

     ```bash
     docker compose config -q
     docker compose up -d --force-recreate worker
     docker compose exec -T worker python -m worker --check-budget </dev/null    # shows off
     ```

  To undo, put the old value back and recreate the worker the same way.

**Stop and decide yourself:**

- **Never raise the key's hard limit, or an agent's budget, to clear a symptom.** Find what is
  spending first. Raising a budget is a board decision, made on purpose.
- **Spend that neither the ledger nor Paperclip explains** means the key may have leaked. Rotate
  it now: [Tokens](tokens.md#openrouter-key).

## The key's limit is spent, or the account is out of credits

**Symptom.** `--check-budget` shows `mode free_only, budget exhausted`. Or the worker's log has
one of these:

```text
enrichment: mode free_only (OpenRouter says the key's limit is spent); …
AI stopped: the OpenRouter account is out of credits: a person has to top it up
```

**Check.** As above, plus **OpenRouter → Credits** for the account balance.

**Fix.**

- **The key's monthly limit is spent:** wait. The limit resets with the month, and the next reading
  lifts the mode by itself. Collection and publishing go on in the meantime.
- **The account is out of credits:** top it up on OpenRouter. The worker does not recover from
  this by itself: once it has seen that answer, it stays `off` until it restarts. After the top-up,
  at a safe minute:

  ```bash
  docker compose restart worker
  docker compose exec -T worker python -m worker --check-budget </dev/null
  ```

Events left as `pending_enrichment` are taken up again by the next enrichment passes, at :05 and
:35, within the mode.

**Stop and decide yourself** how much to top up, and whether the month's limit was right. Ask why
the money ran out before the month did.

---

[Runbooks](README.md) · [Tokens](tokens.md) · [Watchdog incidents](watchdog-incidents.md)
