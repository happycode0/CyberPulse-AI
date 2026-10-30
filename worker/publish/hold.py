"""The events the publisher leaves out of the public files, and why (CYB-62).

The worker keeps everything it collects. A held event stays in the database and the ops API, and
it is published again at the first publish where it no longer matches, say once a curated source
reports the same story. Only the public files leave it out: nothing here deletes anything.

- **No sources.** Every published event points at the pages it came from; one with none cannot.
- **A listing.** A directory feed's entry ("plex-mcp-server by vladimir-tutin — ★ 150", Agent
  Skills Hub) is a catalogue page, not news.
- **Off the beat.** Only discovered sources carry it (community evidence the gate activated,
  never a curated source), and either triage put it on neither desk, or its sources are mostly
  off the beat and so are its own title and summary: a whole-site news feed's sport and recipes,
  a paper index's NLP papers. "Mostly" is the gate's own bar, `min_on_beat_share`, over the
  events being published. A discovered source mostly on the beat keeps every item, so a security
  site's explainer that happens to name no beat word is never lost.

What is on the beat is the gate's word list (config/discovery.yaml `beat` and `beat_words`), or
a CVE. In a week of the site's events (2026-10-21..27) these held 1,032 of 3,531.
"""

import re
from collections import Counter
from collections.abc import Sequence
from enum import StrEnum

from worker.discovery.gate import SOURCE_ID_PREFIX
from worker.models import Beat, Event


class Hold(StrEnum):
    NO_SOURCES = "no_sources"
    LISTING = "listing"
    OFF_BEAT = "off_beat"


# "<name> by <author> — ★ <stars>": a directory entry's title, never a story's.
_LISTING = re.compile(r"\s[—–-]\s★\s?\d")


def holds(
    events: Sequence[Event], *, on_beat: re.Pattern[str], min_share: float
) -> dict[str, Hold]:
    """Why each event left out is left out, by event id. An event not in the result is published."""
    topical = {
        e.event_id: bool(e.cves) or bool(on_beat.search(f"{e.title} {e.summary}")) for e in events
    }
    carried: Counter[str] = Counter()
    carried_on_beat: Counter[str] = Counter()
    for e in events:
        for source_id in {s.source_id for s in e.sources}:
            carried[source_id] += 1
            carried_on_beat[source_id] += topical[e.event_id]

    def off_beat_source(source_id: str) -> bool:
        return carried_on_beat[source_id] < min_share * carried[source_id]

    held: dict[str, Hold] = {}
    for e in events:
        source_ids = {s.source_id for s in e.sources}
        if not source_ids:
            held[e.event_id] = Hold.NO_SOURCES
        elif _LISTING.search(e.title):
            held[e.event_id] = Hold.LISTING
        elif all(s.startswith(SOURCE_ID_PREFIX) for s in source_ids) and (
            e.beat is Beat.OTHER
            or (not topical[e.event_id] and all(map(off_beat_source, source_ids)))
        ):
            held[e.event_id] = Hold.OFF_BEAT
    return held
