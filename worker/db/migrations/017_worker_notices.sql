-- Three notices the worker sends the owner itself, since RIPPERDOC was retired on 2026-10-20
-- (docs/wiki/stage-4b-the-crew.md, card 7; worker/notify/jobs.py). RIPPERDOC only passed on
-- what the worker had measured, so the worker now says it directly:
--
-- - `ladder_change`: the nightly model scan took a model out of the ladder, or let one back in;
-- - `model_proposal`: the weekly gauntlet proposed a model, for the owner to decide;
-- - `monthly_cost`: last month's cost ledger, on the 1st.
alter table notifications drop constraint notifications_kind_check;
alter table notifications add constraint notifications_kind_check
    check (kind in (
        'critical_au_alert', 'daily_digest', 'developing_update', 'incident', 'ladder_change',
        'model_proposal', 'monthly_cost', 'source_discovery', 'system_failure', 'weekly_trends'
    ));
