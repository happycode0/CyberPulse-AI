"""Ground-truth registers: looked-up facts about a CVE, never generated ones.

The site tells readers that "CVSS scores, KEV status, EPSS and technique IDs are looked up from
the authoritative register, never generated" (site/index.html, HOW THIS RUNS). This package is
that register. Each module reads one published source and answers questions about a CVE that some
unrelated advisory happens to mention:

- `kev`   — is it on CISA's Known Exploited Vulnerabilities list, and since when
- `epss`  — what is today's modelled probability of exploitation
- `cvss`  — what base score does the most authoritative container carry (PLAN.md §2.5's chain:
            CNA record → CISA Vulnrichment ADP → NVD)

The shared rule across all three is that an absent answer is `unknown`. A CVE with no CVSS is not
a 0.0, a CVE EPSS has not modelled is not a 0% chance, and a register that failed to download
says nothing at all about anything — hence `GroundTruthError` rather than an empty register.
"""

from worker.groundtruth.cvss import resolve_cvss
from worker.groundtruth.epss import EpssSnapshot, parse_epss_snapshot
from worker.groundtruth.errors import GroundTruthError
from worker.groundtruth.kev import KevCatalogue, parse_kev_catalogue

__all__ = [
    "EpssSnapshot",
    "GroundTruthError",
    "KevCatalogue",
    "parse_epss_snapshot",
    "parse_kev_catalogue",
    "resolve_cvss",
]
