-- What a pass found, beside whether it completed. The source gate and the discovery search
-- kept their summaries only as a log line, so the Sources page could not say what the last gate
-- pass did or how many ready finds wait for a slot (worker/scheduler.py `_gate_job`,
-- `_discovery_job`; worker/publish/build.py `_discovery_summary`). Counts and source ids only:
-- the column is published, so it never holds a URL, a finder's reason or an error text.
alter table job_runs add column detail jsonb;
