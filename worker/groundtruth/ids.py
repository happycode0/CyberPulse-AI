"""CVE identifier normalisation shared by the registers."""

import re

from worker.models import CVE_ID_PATTERN

_CVE_ID = re.compile(CVE_ID_PATTERN, re.IGNORECASE)


def normalise_cve_id(value: object) -> str | None:
    """Return a canonical `CVE-YYYY-NNNN` id, or None if the value is not one.

    Registers are keyed on this, and two registers disagreeing about the spelling of an id is the
    same as one of them not holding the CVE at all: the lookup misses and the answer silently
    becomes `unknown`. EPSS publishes upper case, and feeds quote ids in every case and with
    stray whitespace, so both are normalised here rather than at each call site.

    Validating against the same `CVE_ID_PATTERN` that `CveRef.id` is validated against is the
    point of doing this centrally. An id that cannot be a `CveRef.id` must never become a register
    key, because the only way to use the entry later would be to construct a model that rejects it.

    Parameters
    ----------
    value : object
        Candidate id from a parsed payload; anything that is not a string is rejected.

    Returns
    -------
    str | None
        The upper-cased id, or None when it does not match the CVE id pattern.
    """
    if not isinstance(value, str):
        return None
    candidate = value.strip().upper()
    return candidate if _CVE_ID.match(candidate) else None
