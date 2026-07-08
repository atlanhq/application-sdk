"""Baked-in contract-toolkit baseline — the offline source-of-truth for K007/K008.

The conformance suite ships as a standalone package and runs *inside consumer app
repos*, where the SDK's ``contract-toolkit/src/PklProject`` (which declares the
toolkit's canonical package URI and current published version) is **not** on disk.
The suite is also deterministic and offline, so it cannot fetch that value at scan
time.

The fix mirrors the B-series deprecation manifest: a generator
(``tools/generate_toolkit_baseline.py``) reads ``contract-toolkit/src/PklProject``
at SDK-dev time and writes the committed JSON this module loads, and a drift-guard
test (``tests/test_toolkit_baseline_drift.py``) fails CI if the toolkit is bumped
without regenerating.  At scan time in a consumer repo, ``load_baseline()`` reads
the JSON baked into the installed wheel via ``importlib.resources`` — no SDK
source and no network required.
"""

from __future__ import annotations

import importlib.resources as _ir
import json
import re
from dataclasses import dataclass
from pathlib import Path

# Committed JSON, relative to the ``conformance`` package root (ships in the wheel
# under ``conformance/data/`` — same mechanism as the deprecation manifest).
_BASELINE_RELPATH: tuple[str, ...] = ("data", "toolkit_baseline.json")

# The toolkit's own package manifest, relative to the SDK repo root.  Only read by
# the generator (SDK-dev time); never present in a consumer app repo.
TOOLKIT_PKLPROJECT_RELPATH: tuple[str, ...] = ("contract-toolkit", "src", "PklProject")


def _baseline_path() -> Path:
    return Path(str(_ir.files("conformance"))).joinpath(*_BASELINE_RELPATH)


BASELINE_PATH = _baseline_path()


@dataclass(frozen=True)
class ToolkitBaseline:
    """The canonical toolkit package base URI and latest published version.

    ``canonical_base`` is scheme-less (``package://`` stripped) so it compares
    directly against the base returned by ``generated_freshness._split_uri``.
    """

    canonical_base: str
    latest_version: str


_NAME_RE = re.compile(r'\bname\s*=\s*"([^"]+)"')
_VERSION_RE = re.compile(r'\bversion\s*=\s*"([^"]+)"')
_BASEURI_RE = re.compile(r'\bbaseUri\s*=\s*"([^"]+)"')


def build_baseline(sdk_root: Path) -> ToolkitBaseline:
    """Parse ``contract-toolkit/src/PklProject`` into a :class:`ToolkitBaseline`.

    Resolves the ``\\(name)`` interpolation in ``baseUri`` and strips the URI
    scheme so the stored base matches the scan-time comparison form.
    """
    pkl_project = sdk_root.joinpath(*TOOLKIT_PKLPROJECT_RELPATH)
    text = pkl_project.read_text(encoding="utf-8")

    name_m = _NAME_RE.search(text)
    version_m = _VERSION_RE.search(text)
    baseuri_m = _BASEURI_RE.search(text)
    if not (name_m and version_m and baseuri_m):
        raise ValueError(
            f"{pkl_project} is missing name/version/baseUri — cannot build the "
            f"toolkit baseline."
        )

    name = name_m.group(1)
    version = version_m.group(1)
    base_uri = baseuri_m.group(1).replace("\\(name)", name)
    canonical_base = base_uri.split("://", 1)[-1]
    return ToolkitBaseline(canonical_base=canonical_base, latest_version=version)


def serialize(baseline: ToolkitBaseline) -> str:
    """Deterministic JSON so ``--check`` is a stable staleness gate."""
    payload = {
        "canonical_base": baseline.canonical_base,
        "latest_version": baseline.latest_version,
    }
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


def load_baseline() -> ToolkitBaseline | None:
    """Load the committed baseline, or ``None`` when it is absent/unparseable.

    Returning ``None`` (rather than raising) lets K007/K008 no-op gracefully if
    the baked data ever goes missing — the suite must never crash a consumer's CI.
    """
    try:
        data = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    base = data.get("canonical_base")
    version = data.get("latest_version")
    if not isinstance(base, str) or not isinstance(version, str):
        return None
    return ToolkitBaseline(canonical_base=base, latest_version=version)
