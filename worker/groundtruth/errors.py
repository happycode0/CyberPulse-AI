"""One error type for the ground-truth registers, so an unread register cannot pass for an empty one."""


class GroundTruthError(Exception):
    """Raised when a ground-truth payload cannot be read into a usable register.

    The collectors return `[]` for a payload they cannot parse, which is right for them: a feed
    that yields no items contributes no events and the run carries on. A register is the opposite
    case. Its answers are statements about CVEs that the register was never asked about one at a
    time, so "the catalogue does not contain CVE-X" is only true if the catalogue was really read.
    Returning an empty register on failure would turn a 404 into 1,730 CVEs silently reported as
    not exploited, which is the fabricated zero PLAN.md §2 exists to forbid. Raising makes the
    caller choose between retrying and recording `unknown`; it removes the option of quietly
    publishing the wrong answer.
    """
