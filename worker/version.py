"""Version literals for CyberPulse-AI worker."""

SCHEMA_VERSION = "1"
PIPELINE_VERSION = "1.0.0"
SCORING_VERSION = "4"
# What an event's `enrichment_version` says: UNENRICHED until worker/ai/enrich.py has enriched it,
# then the version of the enrichment that did. Bump ENRICHMENT_VERSION when a task's prompt,
# schema or checks change what it writes.
# Triage gained `ai_significance` (docs/wiki/ai-news-beat.md) without a bump: a bump also makes
# every live attack event due for MITRE again (worker/db/mitre.py), and an event already
# enriched is not triaged again either way. So events enriched before it have none.
UNENRICHED = "0"
ENRICHMENT_VERSION = "1"
