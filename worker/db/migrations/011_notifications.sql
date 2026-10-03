-- Notifications the worker has sent, or tried to send (Stage 5, worker/notify/).
--
-- One row per message, with a key that says which message it is: a daily digest by its Sydney
-- date, a critical alert by its event. The key is unique, so a message is sent at most once.
-- The row is claimed ('sending') before the send and settled after it. A worker that dies
-- mid-send leaves 'sending', and that message is never tried again: a duplicate alert is worse
-- than a missing one, which the next daily digest covers anyway. A send that failed ('failed')
-- is tried again by a later pass, up to a few attempts. A message the secret scan stopped
-- ('withheld') is never sent.
create table notifications (
    id         bigint generated always as identity primary key,
    kind       text not null check (kind in (
                   'critical_au_alert', 'daily_digest', 'developing_update', 'incident',
                   'source_discovery', 'system_failure', 'weekly_trends'
               )),
    dedupe_key text not null unique,
    channel    text not null check (channel in ('telegram')),
    event_id   text,
    status     text not null check (status in ('sending', 'sent', 'failed', 'withheld')),
    attempts   integer not null default 1 check (attempts >= 1),
    claimed_at timestamptz not null,
    sent_at    timestamptz,
    -- What went wrong, as the notifier words it: a status code or an exception's type name,
    -- never a URL (Telegram's carries the bot token).
    error      text
);
create index ix_notifications_kind_claimed on notifications (kind, claimed_at desc);
