-- Source discovery (worker/discovery/).
--
-- A host found by the nightly search, or proposed by TACHIKOMA, gets one source_candidates row.
-- It is `discovered` while only the host is known, a `candidate` once it has a feed, `testing`
-- once a probe of that feed has passed SERAPH's gate, and `active` when enough probes in a row
-- have. Activation writes the source_registry row the collection then reads; until then the
-- candidate is never collected and its items never reach an event. An active source that stops
-- answering is `retired` and its registry row disabled. `rejected` and `retired` are final: the
-- row stays, so the host is never proposed again.
create table source_candidates (
    id            bigint generated always as identity primary key,
    -- Lower case, no leading "www.": one row per host, so nothing is proposed twice.
    host          text not null unique,
    feed_url      text,
    name          text,
    found_by      text not null check (found_by in ('search', 'tachikoma')),
    reason        text,
    -- When TACHIKOMA proposed it, if it did: its proposals a day are capped.
    proposed_at   timestamptz,
    -- What found it: search hits, or TACHIKOMA's example items. Titles and links only.
    evidence      jsonb not null default '[]'::jsonb,
    state         text not null default 'discovered' check (state in (
                      'discovered', 'candidate', 'testing', 'active', 'retired', 'rejected')),
    passes        integer not null default 0 check (passes >= 0),    -- in a row
    failures      integer not null default 0 check (failures >= 0),  -- in a row
    last_probe_at timestamptz,
    -- The last probe's numbers, or why the home page gave no feed.
    last_result   jsonb,
    last_error    text,
    -- Registry rows are never deleted in service; a test that clears the registry takes these too.
    source_id     text references source_registry (id) on delete cascade,
    created_at    timestamptz not null default now(),
    updated_at    timestamptz not null default now(),
    check (state in ('discovered', 'rejected') or feed_url is not null),
    check ((state in ('active', 'retired')) = (source_id is not null))
);
create index ix_source_candidates_state on source_candidates (state, updated_at);

-- Each Tavily search, for the daily credit cap and ROGUE's count against the free month.
create table discovery_searches (
    id         bigint generated always as identity primary key,
    ts         timestamptz not null default now(),
    query      text not null,
    results    integer not null default 0 check (results >= 0),
    -- As Tavily reported it (include_usage); null if it did not say.
    credits    integer check (credits >= 0),
    error      text
);
create index ix_discovery_searches_ts on discovery_searches (ts);

alter table job_runs drop constraint job_runs_job_check;
alter table job_runs add constraint job_runs_job_check
    check (job in ('groundtruth', 'enrichment', 'discovery', 'source-gate'));
