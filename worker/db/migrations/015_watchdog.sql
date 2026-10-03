-- The watchdog (Stage 6, PLAN.md §11, worker/watchdog/): incidents it opens and resolves, TRON's
-- verdicts on the fixes for them, and the circuit breaker.
--
-- Every five minutes the watchdog checks the system against fixed signatures (a lane that has
-- stopped, a feed that keeps failing, a parser that finds nothing, a publish that failed...). A
-- signature seen opens one incident per (kind, subject); seen again, it updates that incident;
-- gone for long enough, it resolves it. Seen again soon after, the same incident reopens, so its
-- count of failed fixes survives a flapping fault.
--
-- An incident only describes the fault. The fix is a pull request a human merges: TELETRAAN
-- files it, WHEELJACK writes it, TRON tests it and posts a verdict here. Three failed verdicts
-- trip the breaker (`needs_human`): no more verdicts are taken, and a person decides.

-- What the watchdog checked, as its own words and figures. `subject` names what is wrong: a lane,
-- a source id, a job, or '' when the system as a whole is.
alter table incidents add column subject text not null default '';
alter table incidents add column last_seen timestamptz;
-- How many passes in a row have seen it.
alter table incidents add column checks integer not null default 1 check (checks >= 1);
-- Figures and short statuses, never feed content or model output.
alter table incidents add column evidence jsonb not null default '{}'::jsonb;
-- The first pass that no longer saw it; it resolves once this is old enough.
alter table incidents add column clear_since timestamptz;
alter table incidents add column fix_failures integer not null default 0
    check (fix_failures >= 0);
alter table incidents add column needs_human boolean not null default false;
alter table incidents add column reopened integer not null default 0 check (reopened >= 0);
alter table incidents add constraint incidents_severity_check
    check (severity in ('low', 'medium', 'high', 'critical'));
-- One unresolved incident per fault.
create unique index ux_incidents_open on incidents (kind, subject) where status <> 'resolved';

-- TRON's verdict on a fix for an incident: the pull request it tested and whether it passed.
-- `reasons` is TRON's own text; the API checks it, and nothing sends it anywhere.
create table incident_verdicts (
    id          bigint generated always as identity primary key,
    incident_id bigint not null references incidents (id) on delete cascade,
    ts          timestamptz not null,
    verdict     text not null check (verdict in ('pass', 'fail')),
    pr          integer check (pr > 0),
    reasons     text
);
create index ix_incident_verdicts_incident on incident_verdicts (incident_id, ts desc);

-- The watchdog's passes, and the publish and push after each run, so it can tell when they stop
-- working. `note` is an exception's type name when a pass raised, never its message.
alter table job_runs drop constraint job_runs_job_check;
alter table job_runs add constraint job_runs_job_check
    check (job in ('groundtruth', 'enrichment', 'discovery', 'source-gate', 'model-scan',
                   'model-gauntlet', 'watchdog', 'publish', 'push'));
alter table job_runs add column note text;

-- An incident goes to Paperclip as well as to Telegram: the Incident routine's webhook.
alter table notifications drop constraint notifications_channel_check;
alter table notifications add constraint notifications_channel_check
    check (channel in ('telegram', 'paperclip'));
