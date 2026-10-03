-- RIPPERDOC, the model scout (PLAN.md §7.6): the daily catalogue scan, the golden set and the
-- weekly gauntlet (worker/ai/catalogue.py, worker/ai/golden.py, worker/ai/gauntlet.py).
--
-- Nothing here is the ladder. config/models.yaml stays the only place a tier's models are named,
-- and a promotion is a reviewed change to it. These tables are what the scan saw and what the
-- gauntlet measured, so RIPPERDOC can report it and MORPHEUS and ROGUE can judge a proposal.

-- OpenRouter's model list as the scans have seen it: one row per model ever listed. The first
-- scan writes every row and no changes, so an empty table is not read as 466 new models.
create table model_catalogue (
    slug                text primary key,
    name                text not null,
    first_seen          timestamptz not null,
    last_seen           timestamptz not null,
    -- When a scan first no longer listed it; cleared if it comes back.
    withdrawn_at        timestamptz,
    -- The listing's headline price, US$ per million tokens; null when it is not a fixed price.
    -- A note, not the guard: the guard reads each route (worker/ai/ladder.py).
    prompt_per_mtok     numeric,
    completion_per_mtok numeric,
    -- The listing names both tools and structured outputs among its parameters.
    capable             boolean not null,
    -- OpenRouter's announced withdrawal date, when it has announced one.
    expiration_date     date,
    -- Artificial Analysis' index, where the listing carries it.
    intelligence_index  numeric,
    updated_at          timestamptz not null default now()
);

-- What changed from one scan to the next, and what the price guard dropped from the ladder or
-- let back in. `detail` holds figures, never model output.
create table model_changes (
    id     bigint generated always as identity primary key,
    ts     timestamptz not null default now(),
    slug   text not null,
    kind   text not null check (kind in (
               'new', 'withdrawn', 'returned', 'price', 'capability', 'expiring',
               'dropped', 'restored')),
    detail jsonb not null default '{}'::jsonb
);
create index ix_model_changes_ts on model_changes (ts desc);
create index ix_model_changes_slug on model_changes (slug, ts desc);

-- One row per daily scan. `ladder` is, per tier, the configured chain, the chain in use and what
-- was dropped from it and why; null when the scan could not check it, and `error` says why.
create table model_scans (
    id            bigint generated always as identity primary key,
    ts            timestamptz not null default now(),
    models_listed integer check (models_listed >= 0),
    changes       integer not null default 0 check (changes >= 0),
    ladder        jsonb,
    error         text
);
create index ix_model_scans_ts on model_scans (ts desc);

-- The pinned golden set the gauntlet judges every model on. `record` is the JSON a model sees
-- (worker/ai/tasks.py `record`), frozen when the event was pinned, so a later change to the
-- event does not move the set. `labels` are the expected answers, from the registers' own
-- severity and the event's sources; `reviewed` turns true when a person has checked them.
-- No foreign key: the set is self-contained on purpose.
create table golden_events (
    event_id  text primary key,
    pinned_at timestamptz not null default now(),
    record    jsonb not null,
    labels    jsonb not null,
    reviewed  boolean not null default false
);

-- One row per gauntlet. `results` holds each model's figures per task; `golden_digest` names the
-- golden set they were measured on, so a result is only ever reused against the same set.
create table gauntlet_runs (
    id            bigint generated always as identity primary key,
    started_at    timestamptz not null,
    finished_at   timestamptz,
    golden_digest text,
    spent_usd     numeric not null default 0 check (spent_usd >= 0),
    results       jsonb not null default '[]'::jsonb,
    note          text
);
create index ix_gauntlet_runs_started on gauntlet_runs (started_at desc);

alter table job_runs drop constraint job_runs_job_check;
alter table job_runs add constraint job_runs_job_check
    check (job in ('groundtruth', 'enrichment', 'discovery', 'source-gate', 'model-scan',
                   'model-gauntlet'));
