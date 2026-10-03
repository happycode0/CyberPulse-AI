-- AU relevance from facts (PLAN.md section 9, Stage 2 item 7).
--
-- Until now the brief task wrote its AU reading straight into the published `au_*` columns. The
-- AU engine (worker/pipeline/au.py) now sets those from the record's facts at every rescore, and
-- the model's reading may only lift the number. So the model's reading moves here and stays,
-- as `source_summary` (006) keeps the feed's text apart from the summary. Not published.
alter table events
    add column au_model_relevance double precision
        check (au_model_relevance between 0 and 1),
    add column au_model_reasons text[] not null default '{}',
    add column au_model_sectors text[] not null default '{}';

-- Only the brief task ever set `au_relevance`, so a rated event's columns are the model's,
-- apart from the one reason the pipeline added when an Australian source reported it.
update events
set au_model_relevance = au_relevance,
    au_model_reasons = array_remove(au_reasons, 'reported by an Australian source'),
    au_model_sectors = au_sectors
where au_relevance is not null;
