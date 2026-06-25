"""Tiny version parsing/comparison for the B-series — no external dependency.

The conformance package is deliberately lean (it scans source text), so rather
than pull in ``packaging`` just to compare deprecation removal versions, we parse
the small, well-behaved version strings the SDK actually writes — ``"v3.2.0"``,
``"4.0"``, ``"3.18.0"`` — into comparable integer tuples.

Only the dotted numeric release segment is understood; any pre-release / local
suffix is ignored.  A string with no leading numeric component yields ``None``
(the caller treats "unparseable" as "do not compare").
"""

from __future__ import annotations

import re

# Leading optional ``v``/``V`` then a dotted run of digits.  Stops at the first
# non ``[0-9.]`` character, so ``"4.0.0rc1"`` → ``(4, 0, 0)`` and trailing prose
# (``"v4.0 — see migration guide"``) is ignored.
_VERSION_RE = re.compile(r"[vV]?(\d+(?:\.\d+)*)")


def parse_version(text: str) -> tuple[int, ...] | None:
    """Parse a release string like ``"v3.2.0"`` into ``(3, 2, 0)``.

    Returns ``None`` when *text* has no leading numeric release component.
    """
    match = _VERSION_RE.match(text.strip())
    if match is None:
        return None
    return tuple(int(part) for part in match.group(1).split("."))


def version_reached(removal: tuple[int, ...], current: tuple[int, ...]) -> bool:
    """True if *current* has reached or passed the *removal* version.

    Comparison is component-wise with zero-padding, so ``(3, 2)`` and
    ``(3, 2, 0)`` compare equal and ``(3, 18, 0) >= (3, 2, 0)`` is ``True``.
    """
    width = max(len(removal), len(current))
    padded_removal = removal + (0,) * (width - len(removal))
    padded_current = current + (0,) * (width - len(current))
    return padded_current >= padded_removal
