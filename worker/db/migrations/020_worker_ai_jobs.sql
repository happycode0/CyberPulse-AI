-- The worker's own AI jobs, which replace the Paperclip crew (owner, 2026-10-31): the daily
-- intelligence report (MORPHEUS's), the follow-up writer (DECKARD's) and the incident diagnosis
-- (TELETRAAN's). Each is one structured call (worker/ai/report.py, followup_writer.py,
-- diagnosis.py), charged to the cost ledger under its own stage.

-- One report per Sydney date. `items` are the events it names, each with why it matters, and
-- `gaps` the quiet topics it picked, each with how long it has been quiet; both are checked
-- against what the call was given before they are stored.
create table daily_reports (
    id            bigint generated always as identity primary key,
    report_date   date not null unique,
    generated_at  timestamptz not null,
    window_hours  integer not null check (window_hours > 0),
    summary       text not null,
    items         jsonb not null default '[]',
    gaps          jsonb not null default '[]',
    model         text
);

-- The diagnosis a high or critical incident got when it opened or reopened: the likely cause, a
-- suggested fix and whose it is ('board' or 'code'), with the model and when. Null until one is
-- made, or when none could be.
alter table incidents add column diagnosis jsonb;

alter table job_runs drop constraint job_runs_job_check;
alter table job_runs add constraint job_runs_job_check
    check (job in ('groundtruth', 'enrichment', 'discovery', 'source-gate', 'model-scan',
                   'model-gauntlet', 'watchdog', 'publish', 'push', 'daily-report',
                   'followup-writer'));

alter table notifications drop constraint notifications_kind_check;
alter table notifications add constraint notifications_kind_check
    check (kind in (
        'critical_au_alert', 'daily_digest', 'daily_report', 'developing_update', 'incident',
        'ladder_change', 'model_proposal', 'monthly_cost', 'routine_wake', 'source_discovery',
        'system_failure', 'weekly_trends'
    ));
