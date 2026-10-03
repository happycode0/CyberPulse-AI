"""EPSS daily snapshot, read as a register of modelled exploitation likelihood.

EPSS answers a different question from KEV. KEV is an observation — CISA saw exploitation — while
EPSS is a model's estimate of the probability a CVE will be exploited in the next 30 days. The two
disagree often and legitimately, and neither substitutes for the other, so both are recorded.

The distinction that matters for honesty is what a miss means. EPSS only scores CVEs its model has
seen, so a CVE absent from the snapshot is unscored, not safe. `EpssSnapshot.lookup` therefore
returns `status="unknown"` with no score, which is the shape worker/db/migrations/001_initial.sql
describes for the `cve_scores` table: "A CVE without a known score has no row (or an epss row with
status 'unknown' and NULL score); an absent score is never stored as zero."
"""

import csv
import gzip
from dataclasses import dataclass
from datetime import datetime
from io import StringIO

from worker.collectors.dates import parse_date
from worker.groundtruth.errors import GroundTruthError
from worker.groundtruth.ids import normalise_cve_id
from worker.models import EpssScore

EPSS_SNAPSHOT_URL = "https://epss.empiricalsecurity.com/epss_scores-current.csv.gz"

# The published file is a .csv.gz, so the gzip layer belongs to the *payload* and is not the
# transport's Content-Encoding. httpx decodes Content-Encoding transparently, so depending on
# whether the server also advertises one, the body reaches us either still compressed or already
# decompressed. Sniffing the magic number handles both without caring which happened — and
# worker/collectors/http.py sends an explicit `Accept-Encoding: gzip`, which makes that ambiguity
# a permanent property of this source rather than a passing one.
GZIP_MAGIC = b"\x1f\x8b"


@dataclass(frozen=True)
class EpssSnapshot:
    """One day's EPSS scores.

    Attributes
    ----------
    model_version : str | None
        From the leading comment line, e.g. "v2026.06.15". EPSS rescales when the model changes,
        so a score is only comparable with another from the same model version.
    score_date : datetime | None
        When the scores were computed, in UTC. This is the field staleness is judged on: the
        "current" URL keeps serving yesterday's file without any HTTP error (PLAN.md §2.6).
    scores : dict[str, EpssScore]
        Scored CVEs, keyed by canonical id. A CVE the model has not scored is simply absent.
    skipped : int
        Rows that could not be used — a bad id, an unparseable number, or one out of range.
    """

    model_version: str | None
    score_date: datetime | None
    scores: dict[str, EpssScore]
    skipped: int = 0

    def lookup(self, cve_id: str) -> EpssScore:
        """Return this snapshot's score for one CVE, or an explicit unknown.

        Unlike `KevCatalogue.lookup`, a miss here is not knowledge of absence. EPSS scores the
        CVEs its model covers, so "not in the snapshot" means unscored — it does not mean the
        probability is low, and it certainly does not mean zero. The default `EpssScore` is
        `score=None, status="unknown"` precisely so that this cannot be read as 0.0 downstream.
        """
        key = normalise_cve_id(cve_id)
        if key is not None:
            scored = self.scores.get(key)
            if scored is not None:
                return scored
        # Fresh instance per call: EpssScore is mutable, and a shared default would let one
        # caller's edit leak into every later answer.
        return EpssScore()


def parse_epss_snapshot(body: bytes) -> EpssSnapshot:
    """Parse the EPSS CSV snapshot, gzipped or not, into a lookup register.

    Parameters
    ----------
    body : bytes
        Raw response body from `EPSS_SNAPSHOT_URL`. Decompressed first if it still carries the
        gzip magic number.

    Returns
    -------
    EpssSnapshot
        The parsed register. Unusable rows are skipped and counted.

    Raises
    ------
    GroundTruthError
        If the body cannot be decompressed, or yields no usable scores at all.
    """
    if body[:2] == GZIP_MAGIC:
        try:
            body = gzip.decompress(body)
        except (OSError, EOFError) as err:
            raise GroundTruthError(f"EPSS snapshot could not be decompressed: {err}") from err

    text = body.decode("utf-8", errors="replace")

    model_version: str | None = None
    score_date: datetime | None = None
    data_lines: list[str] = []

    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            # "#model_version:v2026.06.15,score_date:2026-09-30T12:00:21Z". Splitting the line on
            # commas is safe because an ISO timestamp contains none, and splitting each pair on
            # the *first* colon only is what keeps the timestamp's own colons intact.
            for pair in stripped.lstrip("#").split(","):
                key, _, value = pair.partition(":")
                key, value = key.strip(), value.strip()
                if key == "model_version" and value:
                    model_version = value
                elif key == "score_date" and value:
                    score_date = parse_date(value)
            continue
        data_lines.append(line)

    scores: dict[str, EpssScore] = {}
    skipped = 0
    for row in csv.reader(StringIO("\n".join(data_lines))):
        if not row:
            continue
        if row[0].strip().lower() == "cve":
            continue  # the "cve,epss,percentile" header
        if len(row) < 2:
            skipped += 1
            continue
        cve_id = normalise_cve_id(row[0])
        if cve_id is None:
            skipped += 1
            continue
        score = _as_probability(row[1])
        if score is None:
            skipped += 1
            continue
        scores[cve_id] = EpssScore(score=score, status="known")

    if not scores:
        # The live file carries ~290,000 rows, so an empty result is a changed format rather than
        # a quiet day. Raising keeps a register that knows nothing from answering every lookup.
        raise GroundTruthError(
            f"EPSS snapshot carried no usable scores ({len(data_lines)} rows rejected)"
        )

    # The `percentile` third column is deliberately dropped. EpssScore is `extra="forbid"` and is
    # mirrored in schemas/event.schema.json, so keeping it would mean changing the published event
    # schema — a decision to put to the user, not to make silently while parsing a CSV.
    return EpssSnapshot(
        model_version=model_version,
        score_date=score_date,
        scores=scores,
        skipped=skipped,
    )


def _as_probability(value: str) -> float | None:
    """Parse an EPSS probability, or None if it is not a number in [0, 1].

    The range check is not pedantry: `EpssScore.score` is constrained to [0, 1], so a row EPSS
    published out of range would raise a ValidationError mid-parse and lose the whole snapshot.
    Rejecting the row keeps the other 290,000. A genuine 0.0 is kept — it is a real probability,
    and the only unknown is an absent row.
    """
    try:
        score = float(value.strip())
    except (TypeError, ValueError):
        return None
    # This also rejects NaN and the infinities for free: every comparison against NaN is false,
    # so the chained check fails and the row is skipped rather than reaching EpssScore.
    if not 0.0 <= score <= 1.0:
        return None
    return score
