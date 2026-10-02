-- The cost ledger, reshaped for its first writer.
--
-- 001 created cost_ledger before anything wrote to it, so its columns were a guess. The OpenRouter
-- client (worker/ai/client.py) is the first writer, and ROGUE's brief (PLAN.md section 4) is to
-- record every AI call from OpenRouter's usage.cost (the billed cost, not an estimate) attributed
-- to agent, event and pipeline stage, then report cost per event and per source. A row is written
-- for every call OpenRouter billed, including one whose answer was unusable: that money is spent
-- whether or not the output was any good, and a ledger that drops it under-reports exactly the
-- calls worth looking at.
--
-- `provider` keeps its meaning from 001: whose API billed us ('openrouter', and later 'tavily').
-- The inference company OpenRouter routed the call to is `upstream`, a new column, because
-- "which API" and "which company ran the model" are different questions with different owners.
--
-- `model` is the model that answered, which is the one billed; `requested_model` is the head of
-- the chain that was asked. They differ whenever a fallback served the request, and on tier 0
-- that difference is how a free model's daily cap shows up as spend (PLAN.md section 7.2).
--
-- `cost_usd` becomes nullable with no default: null means OpenRouter did not report a cost, which
-- is not the same as a free call, so it is never stored as zero (the rule 004 applies to absent
-- CVSS). It also gains decimal places. A tier 0 classification on a paid fallback costs around
-- $0.00003, so six places would round thousands of real calls to nothing.
--
-- The table is empty everywhere when this runs (nothing wrote to it before 005), which is why
-- columns can change meaning and nullability here without a backfill.

alter table cost_ledger rename column purpose to stage;

alter table cost_ledger
    alter column cost_usd drop default,
    alter column cost_usd drop not null,
    alter column cost_usd type numeric(18, 10);

alter table cost_ledger
    add column agent           text,
    add column event_id        text references events (event_id) on delete set null,
    -- the config/models.yaml tier the call was made on; null for spend that is not a model call
    add column tier            text check (
        tier in ('tier0_free', 'tier1_cheap', 'tier2_strong', 'code', 'audit')
    ),
    add column requested_model text,
    add column upstream        text,
    -- OpenRouter's generation id: /api/v1/generation looks the call up again from it
    add column generation_id   text,
    -- 'ok'             the answer parsed and passed our own schema check
    -- 'invalid_output' billed, but unusable: not JSON, wrong shape, cut off, or a refusal
    add column outcome         text not null default 'ok'
        check (outcome in ('ok', 'invalid_output')),
    add column duration_ms     integer check (duration_ms >= 0);

-- The default only satisfied `not null` for rows that already existed (there are none). Every
-- writer from here on states the outcome, so a forgotten one fails rather than reads as 'ok'.
alter table cost_ledger alter column outcome drop default;

-- Cost per event is the Stage 2 exit report, and an event's calls are a tiny slice of the table.
create index ix_cost_ledger_event_id on cost_ledger (event_id) where event_id is not null;
