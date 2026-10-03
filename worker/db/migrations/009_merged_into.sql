-- Consolidation (PLAN.md section 9, Stage 3).
--
-- An event found to be part of another one is archived and points at the event it joined.
-- Its sources, CVEs, timeline, claims and techniques move to that event (worker/db/merge.py).
-- The row stays: events are never deleted, and a published link to it must still lead
-- somewhere (index.json's `merged` map).
alter table events
    add column merged_into text references events (event_id),
    add constraint events_merged_into_archived check (merged_into is null or status = 'archived'),
    add constraint events_merged_into_other check (merged_into <> event_id);
create index ix_events_merged_into on events (merged_into) where merged_into is not null;

-- Headlines a feed sent as markup: DTA's are whole <a href="..."> elements. New items are
-- cleaned at normalisation (`clean_title` in worker/pipeline/normalise.py); this repairs the
-- ones already stored, the same way: tags become spaces, the common entities are decoded
-- (`&amp;` last, as `html.unescape` would leave `&amp;lt;`), whitespace collapses. The
-- normalised title and its hash follow `normalise_title`: lower case, ASCII punctuation
-- removed, whitespace collapsed. The helpers are session-only (pg_temp); nothing stays.
create or replace function pg_temp.clean_title(t text) returns text language sql immutable as $$
    select btrim(regexp_replace(
        replace(replace(replace(replace(replace(replace(replace(replace(
            regexp_replace(t, '<[^>]*>', ' ', 'g'),
            '&lt;', '<'), '&gt;', '>'), '&quot;', '"'), '&#39;', ''''), '&#039;', ''''),
            '&apos;', ''''), '&nbsp;', ' '), '&amp;', '&'),
        '\s+', ' ', 'g'))
$$;
create or replace function pg_temp.normalise_title(t text) returns text language sql immutable as $$
    select btrim(regexp_replace(
        translate(lower(t), '!"#$%&''()*+,-./:;<=>?@[\]^_`{|}~', ''), '\s+', ' ', 'g'))
$$;

update event_sources s
set title = c.title,
    title_hash = encode(sha256(convert_to(pg_temp.normalise_title(c.title), 'UTF8')), 'hex')
from (
    select id, pg_temp.clean_title(title) as title from event_sources
    where title ~ '<[A-Za-z/][^>]*>'
) c
where s.id = c.id and c.title <> '';

update events e
set title = c.title,
    normalised_title = pg_temp.normalise_title(c.title)
from (
    select event_id, pg_temp.clean_title(title) as title from events
    where title ~ '<[A-Za-z/][^>]*>'
) c
where e.event_id = c.event_id and c.title <> '';
