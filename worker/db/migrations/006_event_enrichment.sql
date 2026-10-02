-- AI enrichment bookkeeping (PLAN.md section 7, Stage 2 item 6).
--
-- Enrichment is three separate calls per event (worker/ai/tasks.py): triage on tier 0
-- (categories, entities, tags), a brief on tier 1 (summary, why it matters, AU relevance), and,
-- only where no official score exists, a severity judgment on tier 2. Each can succeed while
-- another fails or waits for budget, so each has its own row here. An event leaves
-- `pending_enrichment` only when every task that applies to it is done.
--
-- Like cve_cvss_checks (004), this is bookkeeping about our own activity, not a fact about the
-- event, which is why it is a table rather than columns on `events`, and why it is not published.

create table event_enrichment (
    event_id        text not null references events (event_id) on delete cascade,
    task            text not null check (task in ('triage', 'brief', 'severity')),
    -- the ENRICHMENT_VERSION of the code that ran the task (worker/version.py)
    version         text not null,
    -- 'done'   the answer passed the schema and our own checks, and was written
    -- 'failed' the last attempt gave nothing usable; retried from next_attempt_at
    status          text not null check (status in ('done', 'failed')),
    -- the model that answered; null while nothing has
    model           text,
    -- failed passes in a row; a success resets it. Sets the backoff (worker/db/enrichment.py)
    failures        integer not null default 0 check (failures >= 0),
    -- null on a failed row means the task has given up and will not be retried
    next_attempt_at timestamptz,
    -- why the last attempt failed, in our words: never a quote of model output
    last_error      text,
    done_at         timestamptz,
    updated_at      timestamptz not null default now(),
    primary key (event_id, task)
);

-- The feed's own text, kept apart from `summary`.
--
-- Until now `summary` held the cleaned feed description. Enrichment replaces it with an original
-- summary (PLAN.md section 5: "never a reproduction of source text"), and anything that enriches
-- the event again, after a version bump, must read what the source said rather than what a model
-- said about it. So the source text moves here and stays. Not published.
alter table events add column source_summary text;
update events set source_summary = summary where source_summary is null;
