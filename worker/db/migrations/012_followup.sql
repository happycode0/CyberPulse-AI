-- Event status, DECKARD's follow-up queue and what it reports (Stage 5, worker/db/followup.py).

-- When an entry was written, as against `ts`, when the change happened (a KEV listing is dated
-- the day CISA added it, whenever the sync sees it). Notifications go by this. Entries from
-- before this migration have none, so nothing is ever sent about them.
alter table event_timeline add column created_at timestamptz;
alter table event_timeline alter column created_at set default now();
create index ix_event_timeline_created_at on event_timeline (created_at)
    where created_at is not null;

-- A resolved event's closing summary of the whole case: written once, by DECKARD through the
-- ops API, after the worker's checks (worker/pipeline/followup.py).
alter table events add column resolution text;

-- followup_tasks (migration 001) gets its kinds, and at most one open task per event and kind,
-- so the sweep that opens them can run every collection without doubling any.
alter table followup_tasks add constraint followup_tasks_kind_check
    check (kind in ('check', 'final_summary'));
create unique index ux_followup_tasks_open on followup_tasks (event_id, kind)
    where status in ('pending', 'in_progress');
