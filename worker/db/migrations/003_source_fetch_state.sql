-- Conditional-request validators (ETag / Last-Modified) per source, so an unchanged feed
-- answers 304 instead of being downloaded and re-parsed every run.
-- Kept apart from source_registry, whose static columns are re-synced from the YAML.

create table source_fetch_state (
    source_id     text primary key references source_registry (id) on delete cascade,
    etag          text,
    last_modified text,
    updated_at    timestamptz not null default now()
);
