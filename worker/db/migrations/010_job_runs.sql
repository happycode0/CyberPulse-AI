-- The worker's own passes that are not collection runs (Stage 4, the ops API).
--
-- A collection leaves a row in `runs`. The ground-truth sync and AI enrichment left only log
-- lines, so nothing outside the container could tell whether they were still working. One row
-- per pass, written when it ends (worker/db/jobs.py). LIBRARIAN's wake reads the newest
-- ground-truth row: a pass that completed recently means the job is working.
create table job_runs (
    id          bigint generated always as identity primary key,
    job         text not null check (job in ('groundtruth', 'enrichment')),
    started_at  timestamptz not null,
    finished_at timestamptz not null,
    -- false: the pass raised and did not finish.
    completed   boolean not null,
    -- Failures the pass absorbed and carried on from (a register that could not be read).
    errors      integer not null default 0 check (errors >= 0),
    -- Whether anything the site publishes moved, and so whether it republished.
    changed     boolean not null default false,
    check (finished_at >= started_at)
);
create index ix_job_runs_job_finished on job_runs (job, finished_at desc);
