# The AI news beat

[Wiki home](README.md) ·
Design: [PLAN.md §5, data model](../../PLAN.md#5-data-model) and [§7, AI layer](../../PLAN.md#7-ai-layer)

Every event is on a beat, AI stories are ranked on their
own scale, and trends count AI subjects. The site's half is the one Events feed with a beat
filter (change (b), B3).

---

## What it gives you

AI news is news in its own right, not a side-show of cyber news. It sits in the same Events feed
as everything else. A filter picks the cyber desk, the AI desk or both. An AI story is never
scored as if it were an unrated cyber threat, and it is never given a cyber severity it did not
earn.

## The beat

Every event in `live.json` and the day pages carries `beat`. It is one of four values, always
lowercase:

| `beat` | When | `domains` holds |
|---|---|---|
| `cyber` | Security news | `cybersecurity` |
| `ai` | AI news with no security angle | `ai` |
| `both` | AI news with a security angle, or security news about AI | `cybersecurity` and `ai` |
| `other` | Triage read it and found neither | neither |

The beat is not stored. It is read off `domains` every time (`beat_of` in `worker/models.py`),
so the two can never disagree.

**How a story gets its beat.**

1. **At assembly, from its source.** Each source in `config/sources.yaml` may name a `beat`:
   `cyber`, `ai` or `both`. A source with none is on the cyber beat. A new event's `domains` are
   seeded from it, so an AI post is on the AI desk before any model has read it.
2. **At triage, from the story itself.** Triage sets `domains` from what the record says. It
   can move a story from one desk to another, or add the second desk. An AI story with a
   security angle (`AI_SECURITY`, `AI_THREAT_ACTIVITY` or `AI_CYBER_CONVERGENCE`) is always
   on both desks.
3. **At a merge.** When two stories turn out to be one, the result keeps the desks of both.
   What triage found beats a source's seed. Triage's AI subdomain stays with it.

A source can never start a story on `other`. So `other` only means "triage found neither". It
never means "not read yet".

**Sources on the AI beat** (`beat: ai`): Simon Willison, OpenAI, Google DeepMind, Hugging Face,
Anthropic, The Verge AI, TechCrunch AI, Ars Technica AI, MIT Technology Review AI, Import AI,
the CAIS AI Safety Newsletter and the European Commission's digital strategy news. **On both
beats** (`beat: both`): Embrace The Red, Microsoft's AI and security blog and OWASP GenAI.
ABC's AI feed joins on the AI beat with the ABC change.

## How much an AI story matters

Triage also judges `ai_significance` for any story on the AI desk. It is null on every other
story, and the database refuses one that is not.

| `ai_significance` | Means |
|---|---|
| `major` | A new frontier model, an AI law or ruling taking effect, an AI incident that caused real harm, or a deal or move that reshapes the industry |
| `notable` | A significant product or model update, funding round, policy step or research result |
| `minor` | A routine announcement, a small update, opinion, or a how-to |

Triage sorts AI stories into these categories as well: `ai-industry`, `model-release`,
`ai-governance`, `ai-incident` and `ai-research`. `research` now means security research only.

## How AI stories are ranked

A story's prominence decays from its last material update. The weight it starts from and the
half-life it decays over come from `config/scoring.yaml` (version 4).

| Cyber story | Weight | Half-life | AI story | Weight | Half-life |
|---|---|---|---|---|---|
| critical | 4 | 168 h | major | 4 | 168 h |
| high | 3 | 72 h | notable | 3 | 72 h |
| medium | 2 | 24 h | | | |
| low | 1 | 24 h | minor | 1 | 24 h |
| unknown | 1.5 | 24 h | not judged yet | 1.5 | 24 h |

- **A cyber story, or one on neither desk**, is ranked on its severity, as before.
- **An AI-only story** is ranked on its significance. A major AI story stays live at least as
  long as a critical cyber one.
- **A story on both desks** takes the stronger weight and the longer half-life of the two.
- **An official severity still counts.** If an AI-only story names a CVE with a CNA score, the
  stronger of the two scales stands.

The budget treats a major AI story like a critical or high one. When money is short and only
the stories that matter most are enriched, a major AI story is still among them.

## No cyber severity for AI-only stories

Severity is a cyber rating. An AI model launch is not "critical".

- The worker never asks a model to rate the severity of an AI-only story. That saves the
  strongest tier's spend as well.
- If triage moves a story to the AI desk only, any severity a model estimated for it goes back
  to `unknown`. The model's judgment stays in the evidence.
- An official score (CNA, CISA's ADP, NVD or the vendor) always stays.
- A story on both desks keeps its severity task, because it is security news too.

## Australian AI bodies

A story that names eSafety, DISR (the Department of Industry, Science and Resources), the
National AI Centre, CSIRO or Data61 goes on the Australian desk, under the government sector.
The list is the `au:` organisations in `config/scoring.yaml`. Names match as whole words, case
and all.

## Trends

`trends.json` gains an `ai` topic kind, alongside vendors, actors, malware and kinds of threat.
The AI topics are MCP, AI agents, Gemini, Llama, Mistral, DeepSeek, model poisoning, the EU AI
Act and AI regulation (`config/trends.yaml`, version 2). OpenAI, Anthropic and Google stay vendor
topics, and prompt injection stays a kind of threat.

Each day in `activity` gains `ai_stories`: the stories first seen that day on the AI desk
(beat `ai` or `both`).

## The Events filter

The site builds one Events feed from these fields. It reads, for each event:

- `beat`: always present, one of `cyber`, `ai`, `both` or `other`.
- `ai_significance`: `major`, `notable` or `minor` on an `ai` or `both` story, otherwise null.
- `ai_subdomain`: the AI tag (`AI_INDUSTRY`, `AI_SECURITY`, `AI_THREAT_ACTIVITY` or
  `AI_CYBER_CONVERGENCE`), or null.

The filter has a scope row (AUSTRALIA, preselected, then GLOBAL and ALL) and a beat dimension
(CYBER, AI). CYBER matches `cyber` and `both`. AI matches `ai` and `both`. A story on `other`
is left out by either beat tag. The schema in `schemas/event.schema.json` holds the contract,
and every file is checked against it before it is written.

## Labelling the AI test stories

🔴 This part is yours. The model gauntlet checks models against a golden set. Its cyber events are labelled from the
registers. No register labels AI news, so you label about 10 AI stories yourself. Until you do,
they change nothing: the gauntlet runs on the cyber events alone.

The stories are in `worker/ai/golden_ai.yaml`. For each one:

1. Open its link and read the story.
2. Fill in its labels:
   - `beat`: `cyber`, `ai`, `both` or `other`, as defined [above](#the-beat).
   - `ai_significance`: `major`, `notable` or `minor`, as defined
     [above](#how-much-an-ai-story-matters). Only for `ai` or `both`. Leave it blank otherwise.
   - `au_desk`: `true` if it belongs on the Australian desk, `false` if not. Leave it blank if
     unsure.
3. Set `reviewed: true` on each story you labelled. Never fill in a label you have not checked.
4. Check the story is in the Events feed. A feed may drop a story before the worker collects
   it. If one never arrived, swap in another AI story from the feed, with blank labels.
5. Commit, deploy, and pin the set again on the VM:
   `docker compose exec -T worker python -m worker --pin-golden-set`.

Only the link, source and headline are kept in the file. The record a model is shown comes from
the database when the set is pinned. A labelled story is judged on its beat and significance
(triage) and on the Australian desk (the brief). It is never judged on severity.

## What changed in the database

Migration 016 (`worker/db/migrations/016_ai_beat.sql`) runs on its own when the worker starts:

- It adds the `ai_significance` column. A check keeps it null off the AI desk.
- Stories triage has not read yet get the seed their sources give them now.
- AI stories triage tagged with a security angle are put on both desks.
- A model's severity estimate on an AI-only story goes back to `unknown`.

The scoring version went up to 4, so every live story is re-scored on the next run.
