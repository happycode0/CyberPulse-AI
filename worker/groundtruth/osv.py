"""OSV and GitHub advisories for a CVE: which packages it affects and which versions fix it.

OSV (osv.dev) keeps its own record of each CVE, and that record's `aliases` name the GitHub
advisories (GHSA) for the same flaw. OSV mirrors those advisories too, so one register answers
for both, and nothing here needs a GitHub token. Pure: bytes in, answers out.

What is kept is checkable fact: advisory ids, affected packages, fixed versions, GitHub's own
reviewed rating. A package with no fixed version is recorded with `fixed: []`, which means
"no fix published", not "unaffected".
"""

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from worker.groundtruth.errors import GroundTruthError

OSV_RECORD_URL = "https://api.osv.dev/v1/vulns/{id}"

# How many GitHub advisories to fetch per CVE. One flaw rarely has more than one; a CVE in a
# monorepo can have a handful, and past three the rest add little.
MAX_GHSA_PER_CVE = 3
MAX_PACKAGES = 10
MAX_FIXED = 5
MAX_SUMMARY_CHARS = 300

_GHSA = re.compile(r"^GHSA(-[23456789cfghjmpqrvwx]{4}){3}$")
_SEVERITIES = {"CRITICAL": "critical", "HIGH": "high", "MODERATE": "medium", "LOW": "low"}


@dataclass(frozen=True)
class Package:
    ecosystem: str
    name: str
    fixed: tuple[str, ...]

    def as_json(self) -> dict[str, Any]:
        return {"ecosystem": self.ecosystem, "name": self.name, "fixed": list(self.fixed)}


@dataclass(frozen=True)
class Advisory:
    id: str
    source: str  # 'osv' or 'ghsa'
    summary: str | None
    severity: str | None
    reviewed: bool
    packages: tuple[Package, ...]
    published: datetime | None
    modified: datetime | None
    aliases: tuple[str, ...]
    # Set when the register has taken the advisory back. A withdrawn advisory is not recorded.
    withdrawn: bool = False

    @property
    def url(self) -> str:
        if self.source == "ghsa":
            return f"https://github.com/advisories/{self.id}"
        return f"https://osv.dev/vulnerability/{self.id}"

    @property
    def has_fix(self) -> bool:
        return any(p.fixed for p in self.packages)


def is_ghsa(advisory_id: str) -> bool:
    return bool(_GHSA.match(advisory_id))


def _timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    # OSV writes nanoseconds; Python reads at most microseconds.
    cleaned = re.sub(r"(\.\d{6})\d+", r"\1", value.replace("Z", "+00:00"))
    try:
        parsed = datetime.fromisoformat(cleaned)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _packages(affected: Any) -> tuple[Package, ...]:
    out: dict[tuple[str, str], list[str]] = {}
    for entry in affected if isinstance(affected, list) else ():
        package = entry.get("package") if isinstance(entry, dict) else None
        if not isinstance(package, dict) or not package.get("name") or not package.get("ecosystem"):
            continue  # a GIT range with no package: nothing a reader can install
        key = (str(package["ecosystem"]), str(package["name"]))
        fixed = out.setdefault(key, [])
        for r in entry.get("ranges") or ():
            for event in r.get("events") or ():
                if event.get("fixed") and str(event["fixed"]) not in fixed:
                    fixed.append(str(event["fixed"]))
    return tuple(
        Package(eco, name, tuple(fixed[:MAX_FIXED]))
        for (eco, name), fixed in list(out.items())[:MAX_PACKAGES]
    )


def parse_osv_record(body: bytes) -> Advisory:
    try:
        data = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise GroundTruthError(f"OSV record is not valid JSON: {exc}") from None
    if not isinstance(data, dict) or not isinstance(data.get("id"), str):
        raise GroundTruthError("OSV record has no id")
    advisory_id = data["id"]
    specific = (
        data.get("database_specific") if isinstance(data.get("database_specific"), dict) else {}
    )
    ghsa = is_ghsa(advisory_id)
    summary = data.get("summary")
    if isinstance(summary, str):
        summary = " ".join(summary.split())[:MAX_SUMMARY_CHARS] or None
    else:
        summary = None
    return Advisory(
        id=advisory_id,
        source="ghsa" if ghsa else "osv",
        summary=summary,
        severity=_SEVERITIES.get(str(specific.get("severity", "")).upper()) if ghsa else None,
        reviewed=bool(specific.get("github_reviewed")) if ghsa else False,
        packages=_packages(data.get("affected")),
        published=_timestamp(data.get("published")),
        modified=_timestamp(data.get("modified")),
        aliases=tuple(str(a) for a in data.get("aliases") or () if isinstance(a, str)),
        withdrawn=bool(data.get("withdrawn")),
    )


def ghsa_aliases(record: Advisory) -> tuple[str, ...]:
    """The GitHub advisories a CVE record names, in order, capped."""
    return tuple(a for a in record.aliases if is_ghsa(a))[:MAX_GHSA_PER_CVE]


def names_cve(advisory: Advisory, cve_id: str) -> bool:
    """Whether an advisory really is about this CVE. An alias list that does not name it means
    OSV's alias group has drifted, and the advisory is not recorded against the CVE."""
    return advisory.id == cve_id or cve_id in advisory.aliases
