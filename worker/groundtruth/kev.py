"""CISA Known Exploited Vulnerabilities catalogue, read as a register rather than as events.

The same payload is already a Stage 1 collector source (`cisa_kev` in config/sources.yaml, parser
`json_kev`), which turns each entry into a RawItem so a new KEV listing can be *reported* as an
event. This module reads it for the other job: answering "is CVE-2024-3400 on the list, and since
when" about a CVE that some unrelated advisory happens to mention. Stage 1 publishes the listing;
Stage 2 uses the catalogue to annotate everything else. Neither use replaces the other, which is
why the parsing is not shared — the collector needs titles and URLs per entry, this needs a
lookup table, and forcing one function to produce both would serve neither.

KEV is the strongest exploitation signal the pipeline has, because it is an observation rather
than a model: CISA lists a CVE because exploitation was seen in the wild. That is also why a
lookup miss here is meaningful in a way an EPSS miss is not — see `KevCatalogue.lookup`.
"""

import json
from dataclasses import dataclass
from datetime import date, datetime

from worker.collectors.dates import parse_date
from worker.groundtruth.errors import GroundTruthError
from worker.groundtruth.ids import normalise_cve_id
from worker.models import KevEntry

KEV_CATALOGUE_URL = (
    "https://www.cisa.gov/sites/default/files/feeds/known_exploited_vulnerabilities.json"
)


@dataclass(frozen=True)
class KevCatalogue:
    """One reading of the KEV catalogue.

    Attributes
    ----------
    version : str | None
        CISA's own `catalogVersion` (e.g. "2026.09.30"), used to tell one reading from another.
    released : datetime | None
        `dateReleased`, in UTC. PLAN.md §2.6 requires staleness to be judged on the age of the
        newest item rather than on HTTP status, and for this source that is this field: CISA
        serves 200 OK with a months-old catalogue just as readily as with a current one.
    declared_count : int | None
        The `count` the payload claims to carry, kept so it can be compared with what was parsed.
    entries : dict[str, KevEntry]
        Listed CVEs, keyed by canonical id.
    skipped : int
        Entries that could not be used, almost always a missing or malformed `cveID`.
    """

    version: str | None
    released: datetime | None
    declared_count: int | None
    entries: dict[str, KevEntry]
    skipped: int = 0

    def lookup(self, cve_id: str) -> KevEntry:
        """Return what this catalogue says about one CVE.

        A miss returns `listed=False`, and that is a real answer rather than a guess: the whole
        catalogue was read, so a CVE absent from it is one CISA has not listed. This is exactly
        the distinction `EpssSnapshot.lookup` cannot make — EPSS models a subset of CVEs, so a
        miss there means "not scored", while a miss here means "not listed". The register only
        gets to make that claim because `parse_kev_catalogue` raises instead of returning an
        empty catalogue; without that, every miss would be ambiguous.
        """
        key = normalise_cve_id(cve_id)
        if key is not None:
            listed = self.entries.get(key)
            if listed is not None:
                return listed
        # A fresh instance per call: KevEntry is a pydantic model and therefore mutable, and a
        # shared default would let one caller's edit become every later caller's answer.
        return KevEntry()

    @property
    def is_complete(self) -> bool:
        """Whether CISA's own count agrees with what was parsed.

        A truncated download is the failure this catches. It arrives as valid JSON with a short
        `vulnerabilities` array, so nothing raises and the register looks healthy — it simply
        reports a few thousand exploited CVEs as unlisted. The count CISA publishes alongside the
        array is the only cheap way to notice.
        """
        if self.declared_count is None:
            return True
        return self.declared_count == len(self.entries) + self.skipped


def parse_kev_catalogue(body: bytes) -> KevCatalogue:
    """Parse the KEV catalogue JSON into a lookup register.

    Parameters
    ----------
    body : bytes
        Raw response body from `KEV_CATALOGUE_URL`.

    Returns
    -------
    KevCatalogue
        The parsed register. Individual malformed entries are skipped and counted; only a payload
        that is unusable as a whole raises.

    Raises
    ------
    GroundTruthError
        If the body is not JSON, is not an object, carries no `vulnerabilities` array, or yields
        no usable entries at all.
    """
    try:
        data = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError) as err:
        raise GroundTruthError(f"KEV catalogue is not valid JSON: {err}") from err

    if not isinstance(data, dict):
        raise GroundTruthError(f"KEV catalogue is {type(data).__name__}, not a JSON object")

    vulnerabilities = data.get("vulnerabilities")
    if not isinstance(vulnerabilities, list):
        raise GroundTruthError("KEV catalogue has no vulnerabilities array")

    entries: dict[str, KevEntry] = {}
    skipped = 0
    for vuln in vulnerabilities:
        if not isinstance(vuln, dict):
            skipped += 1
            continue
        cve_id = normalise_cve_id(vuln.get("cveID"))
        if cve_id is None:
            skipped += 1
            continue
        # Dates are recorded leniently. A catalogue entry whose dateAdded CISA has mistyped is
        # still an entry: the CVE is listed, and losing that because one field will not parse
        # would be trading the strong claim for the weak one. `date_added=None` says the date is
        # unknown, which is true, while dropping the entry would say the CVE is not exploited.
        entries[cve_id] = KevEntry(
            listed=True,
            date_added=_as_date(vuln.get("dateAdded")),
            due_date=_as_date(vuln.get("dueDate")),
        )

    if not entries:
        # Never legitimately empty — the live catalogue carries ~1,730 entries — so an empty
        # result means the payload changed shape under us. Raising sends the caller to `unknown`
        # instead of letting a register that knows nothing answer "not exploited" to everything.
        raise GroundTruthError(
            f"KEV catalogue carried no usable entries ({len(vulnerabilities)} rejected)"
        )

    declared = data.get("count")
    return KevCatalogue(
        version=data.get("catalogVersion") if isinstance(data.get("catalogVersion"), str) else None,
        released=parse_date(data.get("dateReleased")),
        declared_count=declared if isinstance(declared, int) and not isinstance(declared, bool) else None,
        entries=entries,
        skipped=skipped,
    )


def _as_date(value: object) -> date | None:
    """Parse a catalogue date to a `date`, or None if it cannot be read.

    KevEntry stores dates rather than timestamps because that is all CISA publishes ("2024-04-12"),
    and inventing a midnight would imply a precision the source does not have.
    """
    if not isinstance(value, str):
        return None
    parsed = parse_date(value)
    return parsed.date() if parsed is not None else None
