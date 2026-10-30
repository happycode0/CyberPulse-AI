-- The worker fires the crew's Follow-up and Source discovery routines itself, and only when there
-- is work for them (worker/notify/routines.py, 2026-10-30). Each fire is claimed here, as an
-- incident's notice is, so two passes never both fire the same wake: `routine_wake`, on the
-- `paperclip` channel (migration 015).
alter table notifications drop constraint notifications_kind_check;
alter table notifications add constraint notifications_kind_check
    check (kind in (
        'critical_au_alert', 'daily_digest', 'developing_update', 'incident', 'ladder_change',
        'model_proposal', 'monthly_cost', 'routine_wake', 'source_discovery', 'system_failure',
        'weekly_trends'
    ));
