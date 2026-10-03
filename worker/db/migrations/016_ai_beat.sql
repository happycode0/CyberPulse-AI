-- The AI news beat (docs/wiki/ai-news-beat.md; AUCyberPulse_EFFORT.md, change (b)).
--
-- Every event is on a beat: cyber, ai, both or other. The beat is not stored. It is read off
-- `domains` (worker/models.py `beat_of`): "cybersecurity" is the cyber desk, "ai" the AI desk,
-- both is both, and neither is other. A new event's domains are seeded from its source's beat
-- (config/sources.yaml `beat`, cyber when unset) and triage then sets them, so other only
-- appears once triage has found neither.
--
-- Triage also judges how much an AI story matters. An AI-only story is ranked on that in place
-- of severity (worker/pipeline/score.py `ranking`), and gets no cyber severity rating.

-- major, notable or minor. Null unless the story is on the AI desk.
alter table events
    add column ai_significance text check (ai_significance in ('major', 'notable', 'minor')),
    add constraint events_ai_significance_needs_ai
        check (ai_significance is null or 'ai' = any(domains));

-- Events assembled before this were not seeded. Those triage has not read yet get the seed
-- their sources give them now. A source on the ai beat seeds "ai"; on both, both; any other
-- source seeds "cybersecurity". The ids are the beat ai and beat both sources in
-- config/sources.yaml on 2026-10-04, plus abc_ai, which is due to join on the ai beat.
-- An event with no sources is cyber, the default.
with beats (source_id, beat) as (
    values
        ('simonwillison', 'ai'), ('openai_news', 'ai'), ('deepmind', 'ai'),
        ('huggingface', 'ai'), ('anthropic_news', 'ai'), ('verge_ai', 'ai'),
        ('techcrunch_ai', 'ai'), ('ars_ai', 'ai'), ('mit_tr_ai', 'ai'), ('import_ai', 'ai'),
        ('cais_aisn', 'ai'), ('eu_digital_strategy', 'ai'), ('abc_ai', 'ai'),
        ('embracethered', 'both'), ('ms_ai_security', 'both'), ('owasp_genai', 'both')
),
seeds as (
    select e.event_id,
           bool_or(coalesce(b.beat, 'cyber') in ('cyber', 'both')) as cyber,
           bool_or(coalesce(b.beat, 'cyber') in ('ai', 'both')) as ai
    from events e
    left join event_sources s on s.event_id = e.event_id
    left join beats b on b.source_id = s.source_id
    where e.domains = '{}'
      and not exists (
          select 1 from event_enrichment x
          where x.event_id = e.event_id and x.task = 'triage' and x.status = 'done')
    group by e.event_id
)
update events e
set domains = array_remove(
        array[case when seeds.cyber then 'cybersecurity' end, case when seeds.ai then 'ai' end],
        null)
from seeds
where seeds.event_id = e.event_id;

-- An AI story with a security angle is on the cyber desk as well. Triage now says so itself
-- (worker/ai/tasks.py `parse_triage`); this applies the same rule to the stories it has read.
update events
set domains = array_prepend('cybersecurity', domains)
where ai_subdomain in ('AI_SECURITY', 'AI_THREAT_ACTIVITY', 'AI_CYBER_CONVERGENCE')
  and not 'cybersecurity' = any(domains);

-- A model's severity estimate is a cyber rating, so an AI-only story drops it. An official
-- score stays. The judgment itself stays in `evidence`.
update events
set severity = 'unknown', severity_source = 'unknown'
where severity_source = 'ai_estimate'
  and 'ai' = any(domains)
  and not 'cybersecurity' = any(domains);
