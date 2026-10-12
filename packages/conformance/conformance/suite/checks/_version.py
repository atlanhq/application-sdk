"""Tiny version parsing/comparison shared across checks — no external dependency.

The conformance package is deliberately lean (it scans source text), so rather
than pull in ``packaging`` just to compare version strings, we parse the small,
well-behaved versions the SDK and toolkit actually write — ``"v3.2.0"``,
``"4.0"``, ``"3.18.0"`` — into comparable integer tuples. Used by the B-series
deprecation removal check and the K-series toolkit version floor.

:func:`locked_sdk_version` reads the ``atlan-application-sdk`` version an app's
``uv.lock`` resolves — the version that actually ships.  It is the one reader
for every rule that gates on the app's SDK: ``P051`` (an SDR app *below* the
interactive-setup floor) and ``P053`` (a prescription whose remedy only exists
*from* an SDK release).

Only the dotted numeric release segment is understood; any pre-release / local
suffix is ignored.  A string with no leading numeric component yields ``None``
(the caller treats "unparseable" as "do not compare").
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

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


#: The SDK distribution an app depends on. Matched name-normalised against the
#: ``[[package]]`` entries in the app's ``uv.lock``.
SDK_DISTRIBUTION = "atlan-application-sdk"


def _normalise_pkg_name(name: str) -> str:
    """PEP 503 name normalisation — runs of ``-``/``_``/``.`` fold to one ``-``."""
    return re.sub(r"[-_.]+", "-", name).lower()


def locked_sdk_version(root: Path) -> str | None:
    """Return the ``atlan-application-sdk`` version locked in ``root/uv.lock``.

    ``None`` means "can't confirm" — no lock, an unparseable lock, or no such
    package in it.  What a caller does with that is its own policy: ``P051``
    will not call an unreadable version a violation, and ``P053`` will not
    prescribe an API it cannot confirm the app has.  Both stay silent.
    """
    lock = root / "uv.lock"
    if not lock.is_file():
        return None
    try:
        doc = tomllib.loads(lock.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        return None
    packages = doc.get("package", [])
    if not isinstance(packages, list):
        # Valid TOML, wrong shape (``package = 1``): unreadable, not a crash.
        return None
    target = _normalise_pkg_name(SDK_DISTRIBUTION)
    for pkg in packages:
        if not isinstance(pkg, dict):
            continue
        if _normalise_pkg_name(str(pkg.get("name", ""))) == target:
            version = pkg.get("version")
            return str(version) if version is not None else None
    return None
