"""Version literals for CyberPulse-AI worker."""

SCHEMA_VERSION = "1"
PIPELINE_VERSION = "1.0.0"
SCORING_VERSION = "2"
# What an event's `enrichment_version` says: UNENRICHED until worker/ai/enrich.py has enriched it,
# then the version of the enrichment that did. Bump ENRICHMENT_VERSION when a task's prompt,
# schema or checks change what it writes.
UNENRICHED = "0"
ENRICHMENT_VERSION = "1"
