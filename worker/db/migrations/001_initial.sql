-- 001_initial: the Stage 1 schema (PLAN.md section 5).
-- All timestamps are timestamptz (UTC). Enum-like columns are text + CHECK so
-- the value sets can change by migration without ALTER TYPE.
-- pgvector is enabled but deliberately unused in Stage 1: no vector columns.

create extension if not exists pg_trgm;
create extension if not exists vector;

create table if not exists schema_migrations (
    filename   text primary key,
    applied_at timestamptz not null default now()
);

-- ---------------------------------------------------------------------------
-- Reference data
-- ---------------------------------------------------------------------------

create table organisations (
    id         bigint generated always as identity primary key,
    name       text not null unique,
    country    text,
    created_at timestamptz not null default now()
);

create table products (
    id         bigint generated always as identity primary key,
    vendor_id  bigint references organisations (id),
    name       text not null,
    cpe        text,
    created_at timestamptz not null default now(),
    unique (vendor_id, name)
);

create table threat_actors (
    id             bigint generated always as identity primary key,
    name           text not null unique,
    mitre_group_id text,
    description    text,
    created_at     timestamptz not null default now()
);

create table threat_actor_aliases (
    id       bigint generated always as identity primary key,
    actor_id bigint not null references threat_actors (id) on delete cascade,
    alias    text not null,
    unique (alias)
);
create index ix_threat_actor_aliases_actor_id on threat_actor_aliases (actor_id);

create table countries (
    code text primary key check (code ~ '^[A-Z]{2}$'),
    name text not null
);

create table sectors (
    slug             text primary key,
    name             text not null,
    soci_asset_class text
);

create table mitre_dataset_versions (
    version     text primary key,
    matrix      text not null default 'enterprise',
    released_at timestamptz,
    loaded_at   timestamptz not null default now()
);

-- ---------------------------------------------------------------------------
-- Sources
-- ---------------------------------------------------------------------------

create table source_registry (
    id                 text primary key,
    name               text not null,
    type               text not null,
    region             text not null,
    category           text not null,
    source_class       text not null,
    priority           integer not null,
    lane               text not null check (lane in ('fast', 'normal', 'deep')),
    enabled            boolean not null,
    url                text not null,
    parser             text not null,
    expected_frequency text not null,
    notes              text,
    updated_at         timestamptz not null default now()
);

create table source_health (
    id         bigint generated always as identity primary key,
    source_id  text not null references source_registry (id),
    checked_at timestamptz not null,
    status     text not null,
    error      text
);
create index ix_source_health_source_checked on source_health (source_id, checked_at desc);

-- A lineage groups reports that trace back to the same original publication,
-- so syndicated copies do not count as independent confirmations.
create table source_lineage (
    lineage_id       text primary key,
    origin_source_id text references source_registry (id),
    description      text,
    created_at       timestamptz not null default now()
);

-- ---------------------------------------------------------------------------
-- Runs and operations
-- ---------------------------------------------------------------------------

create table runs (
    run_id          text primary key,
    lane            text not null check (lane in ('fast', 'normal', 'deep')),
    started_at      timestamptz not null,
    finished_at     timestamptz,
    sources_ok      integer not null default 0,
    sources_failed  integer not null default 0,
    sources_stale   integer not null default 0,
    items_fetched   integer not null default 0,
    new_events      integer not null default 0,
    updated_events  integer not null default 0,
    duplicates      integer not null default 0,
    archived_events integer not null default 0,
    errors          text[] not null default '{}'
);
create index ix_runs_started_at on runs (started_at desc);

-- ---------------------------------------------------------------------------
-- Events
-- ---------------------------------------------------------------------------

create table events (
    event_id                       text primary key check (event_id ~ '^evt-[0-9]{4}-[0-9]{6}$'),
    schema_version                 text not null,
    pipeline_version               text not null,
    scoring_version                text not null,
    enrichment_version             text not null,

    first_seen                     timestamptz not null,
    last_seen                      timestamptz not null,
    last_material_update           timestamptz,
    last_independent_confirmation  timestamptz,
    status                         text not null default 'new' check (status in (
        'new', 'active', 'developing', 'monitoring', 'contained', 'resolved', 'archived')),

    title                          text not null,
    normalised_title               text not null default '',
    summary                        text not null,
    why_it_matters                 text,

    domains                        text[] not null default '{}',
    categories                     text[] not null default '{}',
    ai_subdomain                   text check (ai_subdomain in (
        'AI_INDUSTRY', 'AI_SECURITY', 'AI_THREAT_ACTIVITY', 'AI_CYBER_CONVERGENCE')),

    severity                       text not null default 'unknown' check (severity in (
        'critical', 'high', 'medium', 'low', 'unknown')),
    severity_source                text not null default 'unknown' check (severity_source in (
        'cna', 'cisa_adp', 'nvd', 'vendor', 'ai_estimate', 'unknown')),

    -- risk: NULL means unscored, never zero.
    urgency                        double precision check (urgency between 0 and 1),
    confidence                     double precision check (confidence between 0 and 1),
    novelty                        double precision check (novelty between 0 and 1),
    prominence                     double precision check (prominence between 0 and 1),

    au_relevance                   double precision check (au_relevance between 0 and 1),
    au_directly_reported           boolean not null default false,
    au_reasons                     text[] not null default '{}',
    au_sectors                     text[] not null default '{}',
    au_soci_asset_classes          text[] not null default '{}',

    -- entities: names as extracted; canonical rows live in the reference tables.
    entity_actors                  text[] not null default '{}',
    entity_organisations           text[] not null default '{}',
    entity_products                text[] not null default '{}',
    entity_countries               text[] not null default '{}',
    entity_industries              text[] not null default '{}',

    tags                           text[] not null default '{}',
    pending_enrichment             boolean not null default false,

    created_at                     timestamptz not null default now(),
    updated_at                     timestamptz not null default now()
);
create index ix_events_normalised_title_trgm on events using gin (normalised_title gin_trgm_ops);
create index ix_events_status on events (status);
create index ix_events_prominence on events (prominence);
create index ix_events_last_material_update on events (last_material_update);

create table event_sources (
    id                bigint generated always as identity primary key,
    event_id          text not null references events (event_id) on delete cascade,
    source_id         text not null references source_registry (id),
    url               text not null,
    canonical_url     text,
    guid              text,
    title             text,
    published         timestamptz,
    fetched_at        timestamptz,
    evidence_class    text not null check (evidence_class in (
        'PRIMARY', 'AUTHORITATIVE', 'VENDOR', 'SPECIALIST',
        'NEWS', 'COMMUNITY', 'SOCIAL', 'AI_INFERENCE')),
    lineage_id        text references source_lineage (lineage_id),
    independent       boolean not null default false,
    url_hash          text not null,
    title_hash        text,
    payload_hash      text,
    created_at        timestamptz not null default now(),
    unique (event_id, url_hash)
);
create index ix_event_sources_event_id on event_sources (event_id);
create index ix_event_sources_url_hash on event_sources (url_hash);
create index ix_event_sources_guid on event_sources (guid);
create index ix_event_sources_lineage_id on event_sources (lineage_id);

create table event_timeline (
    id        bigint generated always as identity primary key,
    event_id  text not null references events (event_id) on delete cascade,
    ts        timestamptz not null,
    type      text not null check (type in (
        'NEW_FACT', 'NEW_CVE', 'NEW_EXPLOIT', 'EXPLOIT_CONFIRMED', 'NEW_ACTOR',
        'NEW_TARGET', 'NEW_GEOGRAPHY', 'NEW_AU_EXPOSURE', 'NEW_IMPACT', 'NEW_PATCH',
        'NEW_MITIGATION', 'NEW_EVIDENCE', 'CORRECTION', 'NO_MATERIAL_CHANGE')),
    summary   text not null,
    sources   text[] not null default '{}'
);
create index ix_event_timeline_event_ts on event_timeline (event_id, ts);

create table event_relationships (
    id               bigint generated always as identity primary key,
    event_id         text not null references events (event_id) on delete cascade,
    related_event_id text not null references events (event_id) on delete cascade,
    type             text not null check (type in (
        'related_event', 'follow_up_to', 'caused_by', 'exploits', 'affects',
        'targets', 'uses', 'attributed_to', 'mitigated_by', 'resolves')),
    created_at       timestamptz not null default now(),
    check (event_id <> related_event_id),
    unique (event_id, related_event_id, type)
);
create index ix_event_relationships_related on event_relationships (related_event_id);

create table claims (
    id         bigint generated always as identity primary key,
    event_id   text not null references events (event_id) on delete cascade,
    text       text not null,
    confidence double precision not null check (confidence between 0 and 1),
    evidence   text[] not null default '{}',
    created_at timestamptz not null default now()
);
create index ix_claims_event_id on claims (event_id);

-- Structured evidence backing an event or one of its claims. Kept apart from
-- AI inference: `evidence_class` records how strong the underlying source is.
create table evidence (
    id              bigint generated always as identity primary key,
    event_id        text not null references events (event_id) on delete cascade,
    claim_id        bigint references claims (id) on delete cascade,
    event_source_id bigint references event_sources (id) on delete set null,
    kind            text not null,
    evidence_class  text not null check (evidence_class in (
        'PRIMARY', 'AUTHORITATIVE', 'VENDOR', 'SPECIALIST',
        'NEWS', 'COMMUNITY', 'SOCIAL', 'AI_INFERENCE')),
    detail          jsonb not null default '{}'::jsonb,
    observed_at     timestamptz not null default now()
);
create index ix_evidence_event_id on evidence (event_id);
create index ix_evidence_claim_id on evidence (claim_id);

-- ---------------------------------------------------------------------------
-- Vulnerabilities and ATT&CK
-- ---------------------------------------------------------------------------

create table cves (
    cve_id         text primary key check (cve_id ~ '^CVE-[0-9]{4}-[0-9]{4,}$'),
    description    text,
    published      timestamptz,
    kev_listed     boolean not null default false,
    kev_date_added date,
    kev_due_date   date,
    created_at     timestamptz not null default now(),
    updated_at     timestamptz not null default now()
);

-- Score history. A CVE without a known score has no row (or an epss row with
-- status 'unknown' and NULL score); an absent score is never stored as zero.
create table cve_scores (
    id          bigint generated always as identity primary key,
    cve_id      text not null references cves (cve_id) on delete cascade,
    kind        text not null check (kind in ('cvss', 'epss')),
    score       double precision,
    vector      text,
    source      text,
    status      text not null default 'known',
    observed_at timestamptz not null default now(),
    check (
        (kind = 'cvss' and score is not null and score between 0 and 10)
        or (kind = 'epss' and (score is null or score between 0 and 1))
    )
);
create index ix_cve_scores_cve_kind_observed on cve_scores (cve_id, kind, observed_at desc);

create table event_cves (
    event_id text not null references events (event_id) on delete cascade,
    cve_id   text not null references cves (cve_id),
    primary key (event_id, cve_id)
);
create index ix_event_cves_cve_id on event_cves (cve_id);

-- ATT&CK technique mappings per event (not the ATT&CK catalogue itself).
create table mitre_techniques (
    id                bigint generated always as identity primary key,
    event_id          text not null references events (event_id) on delete cascade,
    technique_id      text not null,
    name              text not null,
    confidence        double precision not null check (confidence between 0 and 1),
    confidence_type   text not null,
    dataset_version   text not null references mitre_dataset_versions (version),
    evidence          text[] not null default '{}',
    unique (event_id, technique_id, dataset_version)
);
create index ix_mitre_techniques_technique_id on mitre_techniques (technique_id);

-- ---------------------------------------------------------------------------
-- Analytics, cost and self-management
-- ---------------------------------------------------------------------------

create table trends (
    id           bigint generated always as identity primary key,
    kind         text not null,
    key          text not null,
    period_start timestamptz not null,
    period_end   timestamptz not null,
    value        double precision,
    payload      jsonb not null default '{}'::jsonb,
    run_id       text references runs (run_id) on delete set null,
    computed_at  timestamptz not null default now(),
    check (period_end > period_start),
    unique (kind, key, period_start, period_end)
);

create table cost_ledger (
    id            bigint generated always as identity primary key,
    ts            timestamptz not null default now(),
    run_id        text references runs (run_id) on delete set null,
    provider      text not null,
    model         text,
    purpose       text,
    tokens_in     integer not null default 0,
    tokens_out    integer not null default 0,
    cost_usd      numeric(12, 6) not null default 0
);
create index ix_cost_ledger_ts on cost_ledger (ts);

create table followup_tasks (
    id          bigint generated always as identity primary key,
    event_id    text not null references events (event_id) on delete cascade,
    kind        text not null,
    status      text not null default 'pending' check (status in (
        'pending', 'in_progress', 'done', 'failed', 'cancelled')),
    due_at      timestamptz not null,
    attempts    integer not null default 0,
    last_error  text,
    payload     jsonb not null default '{}'::jsonb,
    created_at  timestamptz not null default now(),
    updated_at  timestamptz not null default now()
);
create index ix_followup_tasks_due on followup_tasks (status, due_at);
create index ix_followup_tasks_event_id on followup_tasks (event_id);

create table incidents (
    id           bigint generated always as identity primary key,
    kind         text not null,
    severity     text not null default 'medium',
    status       text not null default 'open' check (status in (
        'open', 'mitigated', 'resolved')),
    source_id    text references source_registry (id) on delete set null,
    title        text not null,
    detail       text,
    opened_at    timestamptz not null default now(),
    resolved_at  timestamptz
);
create index ix_incidents_status on incidents (status, opened_at desc);

create table agent_proposals (
    id          bigint generated always as identity primary key,
    agent       text not null,
    kind        text not null,
    title       text not null,
    body        text,
    payload     jsonb not null default '{}'::jsonb,
    status      text not null default 'proposed' check (status in (
        'proposed', 'approved', 'rejected', 'applied')),
    created_at  timestamptz not null default now(),
    decided_at  timestamptz,
    decided_by  text
);
create index ix_agent_proposals_status on agent_proposals (status, created_at desc);
