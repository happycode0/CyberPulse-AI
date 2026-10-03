"""MITRE ATT&CK and ATLAS: which release is current, and the techniques in it (PLAN.md §4, §7.3).

A model may suggest a technique only from these catalogues, so they are the register for
technique ids just as KEV is for exploitation. Pure: bytes in, answers out.

- ATT&CK comes from `mitre-attack/attack-stix-data`. Its `index.json` names every release with a
  versioned URL, so the 54 MB bundle is downloaded only when a new release appears.
- ATLAS comes from `mitre-atlas/atlas-data`, through `dist/manifest.yaml`. Never through
  `ATLAS-latest.yaml`: that is a symlink, and raw GitHub serves a symlink as its target's name.

Revoked and deprecated techniques are left out, so a suggestion can never name one.
"""

import json
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

import yaml

from worker.groundtruth.errors import GroundTruthError

ATTACK_INDEX_URL = (
    "https://raw.githubusercontent.com/mitre-attack/attack-stix-data/master/index.json"
)
ATLAS_BASE_URL = "https://raw.githubusercontent.com/mitre-atlas/atlas-data/main/dist/"
ATLAS_MANIFEST_URL = ATLAS_BASE_URL + "manifest.yaml"

ENTERPRISE_COLLECTION = "Enterprise ATT&CK"
# The ATLAS file format this parser reads. A new major format is left alone until the parser
# is checked against it.
ATLAS_FORMAT_MAJOR = "6"

# Fewer techniques than this means a truncated or wrong file, not a smaller matrix. Enterprise
# v19.2 has 697 live techniques and ATLAS 2026.09 has 208.
MIN_ATTACK_TECHNIQUES = 400
MIN_ATLAS_TECHNIQUES = 100


@dataclass(frozen=True)
class Release:
    """One published release of a matrix, before it is downloaded."""

    matrix: str  # 'enterprise' or 'atlas'
    version: str  # as stored: 'ATT&CK v19.2', 'ATLAS 2026.09'
    url: str
    released_at: datetime | None


@dataclass(frozen=True)
class Technique:
    id: str
    name: str
    tactics: tuple[str, ...]
    parent_id: str | None


@dataclass(frozen=True)
class Catalogue:
    release: Release
    techniques: tuple[Technique, ...]


def _timestamp(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=UTC)
    if isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    return None


def _json(body: bytes, what: str) -> Any:
    try:
        return json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise GroundTruthError(f"{what} is not valid JSON: {exc}") from None


def _yaml(body: bytes, what: str) -> Any:
    try:
        return yaml.safe_load(body)
    except (yaml.YAMLError, UnicodeDecodeError) as exc:
        raise GroundTruthError(f"{what} is not valid YAML: {exc}") from None


# ─── ATT&CK ───────────────────────────────────────────────────────────────────────────────────────


def latest_attack_release(body: bytes) -> Release:
    """The newest Enterprise ATT&CK release that `index.json` lists."""
    index = _json(body, "the ATT&CK index")
    try:
        collection = next(c for c in index["collections"] if c.get("name") == ENTERPRISE_COLLECTION)
        newest = collection["versions"][0]
        version, url = str(newest["version"]), str(newest["url"])
    except (KeyError, IndexError, TypeError, StopIteration):
        raise GroundTruthError("the ATT&CK index does not list an Enterprise release") from None
    if not url.startswith("https://"):
        raise GroundTruthError("the ATT&CK index gives a release URL that is not https")
    return Release("enterprise", f"ATT&CK v{version}", url, _timestamp(newest.get("modified")))


def _attack_id(obj: dict[str, Any]) -> str | None:
    for ref in obj.get("external_references") or ():
        if ref.get("source_name") == "mitre-attack" and ref.get("external_id"):
            return str(ref["external_id"])
    return None


def parse_attack_bundle(body: bytes, release: Release) -> Catalogue:
    """The live techniques in an Enterprise ATT&CK STIX bundle.

    The bundle's own collection object must carry the version the index promised, so a stale
    or mislabelled file is refused rather than loaded under the wrong name.
    """
    bundle = _json(body, "the ATT&CK bundle")
    objects = bundle.get("objects") if isinstance(bundle, dict) else None
    if not isinstance(objects, list):
        raise GroundTruthError("the ATT&CK bundle has no objects")
    collection = next((o for o in objects if o.get("type") == "x-mitre-collection"), None)
    if collection is None or f"ATT&CK v{collection.get('x_mitre_version')}" != release.version:
        raise GroundTruthError(f"the ATT&CK bundle is not {release.version}")

    techniques: dict[str, Technique] = {}
    for obj in objects:
        if obj.get("type") != "attack-pattern" or obj.get("revoked"):
            continue
        if obj.get("x_mitre_deprecated"):
            continue
        tid, name = _attack_id(obj), obj.get("name")
        if not tid or not name:
            continue
        tactics = tuple(
            p["phase_name"]
            for p in obj.get("kill_chain_phases") or ()
            if p.get("kill_chain_name") == "mitre-attack" and p.get("phase_name")
        )
        parent = tid.split(".", 1)[0] if "." in tid else None
        techniques[tid] = Technique(tid, str(name), tactics, parent)
    if len(techniques) < MIN_ATTACK_TECHNIQUES:
        raise GroundTruthError(
            f"the ATT&CK bundle has {len(techniques)} live techniques, too few to be complete"
        )
    return Catalogue(release, tuple(sorted(techniques.values(), key=lambda t: t.id)))


# ─── ATLAS ────────────────────────────────────────────────────────────────────────────────────────


def latest_atlas_release(body: bytes) -> Release:
    """The newest ATLAS release in `dist/manifest.yaml` that has a file in the format we read."""
    manifest = _yaml(body, "the ATLAS manifest")
    if not isinstance(manifest, list) or not manifest:
        raise GroundTruthError("the ATLAS manifest lists no releases")
    for entry in manifest:
        if not isinstance(entry, dict):
            continue
        for version in entry.get("versions") or ():
            path = str(version.get("path") or "")
            fmt = str(version.get("format-version") or "")
            if fmt.split(".", 1)[0] != ATLAS_FORMAT_MAJOR or not path.endswith(".yaml"):
                continue
            if path.startswith("/") or ".." in path or "latest" in path.lower():
                continue
            return Release(
                "atlas",
                f"ATLAS {entry.get('release')}",
                ATLAS_BASE_URL + path,
                _timestamp(entry.get("release-date")),
            )
    raise GroundTruthError(f"the ATLAS manifest has no format-{ATLAS_FORMAT_MAJOR} release")


def parse_atlas(body: bytes, release: Release) -> Catalogue:
    """The techniques in one ATLAS release file, with tactics from its `achieves` relationships."""
    data = _yaml(body, "the ATLAS file")
    if not isinstance(data, dict) or not isinstance(data.get("techniques"), dict):
        raise GroundTruthError("the ATLAS file has no techniques")
    stated = (data.get("collection") or {}).get("version")
    if f"ATLAS {stated}" != release.version:
        raise GroundTruthError(f"the ATLAS file is not {release.version}")

    tactics: dict[str, list[str]] = {}
    for rels in (data.get("relationships") or {}).values():
        for rel in (rels or {}).get("achieves") or ():
            tactics.setdefault(str(rel.get("source")), []).append(str(rel.get("target")))

    techniques = []
    for tid, obj in data["techniques"].items():
        if not isinstance(obj, dict) or not obj.get("name") or obj.get("deprecated"):
            continue
        tid = str(tid)
        parts = tid.split(".")
        parent = ".".join(parts[:2]) if len(parts) > 2 else None
        own = tactics.get(tid) or (tactics.get(parent, []) if parent else [])
        techniques.append(Technique(tid, str(obj["name"]), tuple(dict.fromkeys(own)), parent))
    if len(techniques) < MIN_ATLAS_TECHNIQUES:
        raise GroundTruthError(
            f"the ATLAS file has {len(techniques)} techniques, too few to be complete"
        )
    return Catalogue(release, tuple(sorted(techniques, key=lambda t: t.id)))
