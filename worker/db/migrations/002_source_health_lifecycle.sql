-- Source health detail (staleness) and the source lifecycle state.

alter table source_health
    add column newest_item_age_days double precision,
    add column items_fetched        integer not null default 0,
    add column duration_ms          integer not null default 0;

-- Null means "not set yet"; the worker derives ACTIVE/DISCOVERED from `enabled`.
alter table source_registry
    add column lifecycle_state text check (lifecycle_state in (
        'discovered', 'candidate', 'testing', 'validated',
        'active', 'degraded', 'broken', 'retired'
    ));
