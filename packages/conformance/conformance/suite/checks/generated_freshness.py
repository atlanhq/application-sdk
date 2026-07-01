"""K003/K004/K005 — generated-artifact freshness (BLDX-1414).

A repo-level (``scan_all``) check under the **K** series that guards the *outputs*
of ``pkl eval`` (``atlan.yaml``, ``app/generated/**``) rather than the ``.pkl``
source that ``legacy_contract`` (K001/K002) scans.  Multiple check modules under
one series letter is the established pattern (P = ``prescriptions`` +
``determinism``); this registration rides the existing ``K`` CI matrix leg.

What each rule catches — all **deterministic, no pkl toolchain required**:

* **K003 ContractLockDrift** — ``contract/PklProject`` pins a dependency at an
  exact ``@<version>`` that the resolved lock ``contract/PklProject.deps.json``
  does not match (or the lock is missing / lacks the dependency).  A stale lock
  means ``pkl eval`` regenerates from the wrong toolkit version.
* **K004 MissingGeneratedArtifact** — ``contract/app.pkl`` exists but an expected
  output (``atlan.yaml``, ``app/generated/manifest.json``,
  ``app/generated/_input.py``) is absent — the contract was never generated.
* **K005 GeneratedArtifactBannerStripped** — a generated text artifact
  (``atlan.yaml`` / ``app.yaml`` / ``app/generated/*.py`` other than
  ``__init__.py``) is missing its ``… DO NOT EDIT …`` provenance banner — a
  heuristic hand-edit signal.  Content-level hand-edits that keep the banner are
  invisible here; only the CI regenerate-and-diff gate proves full freshness.

Scope
-----
All three are ``APP``-scoped and gated on the relevant ``contract/`` file being
present, so they no-op on the SDK (no ``contract/`` at its root) and on any repo
that excludes ``contract/`` from the scan.

Suppression
-----------
K003/K004 anchor on pkl source (``PklProject`` / ``app.pkl``) and reuse the
``// conformance: ignore[...]`` parser from ``legacy_contract``.  K005 anchors on
the artifact file (``#`` comments in both ``.yaml`` and ``.py``) and uses the
``# conformance: ignore[K005] <reason>`` form.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

from conformance.suite.checks._ast_common import TOOL_VERSION, make_cli_main
from conformance.suite.checks.legacy_contract._directives_pkl import (
    _make_pkl_finding_suppressed,
    _parse_pkl_directives,
)
from conformance.suite.schema.findings import Finding

SERIES = "K"

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Repo-relative paths of the artifacts every generated app is expected to ship
# (K004). Kept to the outputs that both single- and multi-entrypoint apps emit.
_EXPECTED_OUTPUTS: tuple[str, ...] = (
    "atlan.yaml",
    "app/generated/manifest.json",
    "app/generated/_input.py",
)

# The provenance banner the contract toolkit stamps into every text artifact it
# writes.  Two variants exist in the fleet — "AUTO-GENERATED from contract/app.pkl
# — DO NOT EDIT MANUALLY." and "Generated from contract/app.pkl via
# contract-toolkit. DO NOT EDIT." — so K005 matches the shared invariant: a
# comment in the first few lines that says both "generated" and "do not edit".
_BANNER_GENERATED_RE = re.compile(r"generated", re.IGNORECASE)
_BANNER_DONOTEDIT_RE = re.compile(r"do not edit", re.IGNORECASE)
_BANNER_SCAN_LINES = 5

# ``#``-comment suppression (K005 — both YAML and Python use ``#``).
_HASH_SUPPRESS_RE = re.compile(
    r"#\s*conformance\s*:\s*ignore\s*(?:\[([^\]]*)\])?\s*(.*)",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Pkl URI parsing (K003)
# ---------------------------------------------------------------------------

_URI_RE = re.compile(r'uri\s*=\s*"([^"]+)"')


def _split_uri(uri: str) -> tuple[str, str] | None:
    """Return ``(scheme-less base, version)`` for a versioned pkl package URI.

    ``package://host/path/pkg@0.16.0`` → ``("host/path/pkg", "0.16.0")``.  The
    scheme is stripped so a ``package://`` pin and a ``projectpackage://`` lock
    entry for the same dependency compare equal.  Returns ``None`` for a URI with
    no ``@<version>`` suffix.
    """
    if "@" not in uri:
        return None
    base, _, version = uri.rpartition("@")
    if not version:
        return None
    # Strip the scheme (everything up to and including "://") if present.
    base = base.split("://", 1)[-1]
    return base, version


def _parse_pkl_project_pins(text: str) -> list[tuple[str, str, int]]:
    """Return ``[(base, version, lineno)]`` for every versioned dependency URI
    pinned in a ``PklProject`` file."""
    pins: list[tuple[str, str, int]] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        m = _URI_RE.search(line)
        if m is None:
            continue
        split = _split_uri(m.group(1))
        if split is None:
            continue
        pins.append((split[0], split[1], lineno))
    return pins


def _parse_deps_lock(text: str) -> dict[str, str] | None:
    """Return ``{base: resolved_version}`` from a ``PklProject.deps.json`` lock,
    or ``None`` when the file is not valid JSON (compare is then skipped)."""
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    resolved: dict[str, str] = {}
    deps = data.get("resolvedDependencies")
    if not isinstance(deps, dict):
        return {}
    for entry in deps.values():
        if not isinstance(entry, dict):
            continue
        uri = entry.get("uri")
        if not isinstance(uri, str):
            continue
        split = _split_uri(uri)
        if split is not None:
            resolved[split[0]] = split[1]
    return resolved


def _version_satisfied(pin: str, resolved: str) -> bool:
    """True when *resolved* is compatible with the *pin* (prefix match).

    A broad pin (``0`` or ``0.16``) is satisfied by any resolved version that
    extends it (``0.16.0``); only a fully-specified pin that disagrees is drift.
    """
    pin_parts = pin.split(".")
    resolved_parts = resolved.split(".")
    return resolved_parts[: len(pin_parts)] == pin_parts


# ---------------------------------------------------------------------------
# K005 banner / suppression helpers
# ---------------------------------------------------------------------------


def _has_banner(text: str) -> bool:
    """True when a single header line carries both provenance markers.

    Requiring both markers on the *same* line (rather than anywhere in the
    header window) avoids a false positive on a hand-written preamble that
    happens to mention "generated" and "do not edit" on separate lines.
    """
    lines = text.splitlines()[:_BANNER_SCAN_LINES]
    return any(
        _BANNER_GENERATED_RE.search(line) and _BANNER_DONOTEDIT_RE.search(line)
        for line in lines
    )


def _hash_suppressed(text: str, rule_id: str) -> tuple[bool, str | None]:
    """Return ``(suppressed, justification)`` for a ``#``-comment directive in the
    file header (K005 anchors on line 1, so only the header can suppress it)."""
    for line in text.splitlines()[: _BANNER_SCAN_LINES + 1]:
        idx = line.find("#")
        if idx == -1:
            continue
        m = _HASH_SUPPRESS_RE.match(line[idx:])
        if m is None:
            continue
        ids_blob = (m.group(1) or "").strip()
        justification = m.group(2).strip()
        if not justification:
            continue
        ids = {s.strip() for s in ids_blob.split(",") if s.strip()}
        if not ids or rule_id in ids:
            return True, justification
    return False, None


# ---------------------------------------------------------------------------
# Per-rule scans
# ---------------------------------------------------------------------------


def _scan_lock_drift(root: Path, present: set[str]) -> list[Finding]:
    """K003 — compare ``contract/PklProject`` pins against the resolved lock."""
    if "contract/PklProject" not in present:
        return []
    pkl_project = root / "contract" / "PklProject"
    try:
        text = pkl_project.read_text(encoding="utf-8")
    except OSError:
        return []
    pins = _parse_pkl_project_pins(text)
    if not pins:
        return []

    directives = _parse_pkl_directives(text)
    rel = "contract/PklProject"
    findings: list[Finding] = []

    deps_path = root / "contract" / "PklProject.deps.json"
    lock: dict[str, str] | None = None
    if deps_path.is_file():
        try:
            lock = _parse_deps_lock(deps_path.read_text(encoding="utf-8"))
        except OSError:
            lock = None

    for base, version, lineno in pins:
        if lock is None:
            message = (
                f"contract/PklProject pins '{base}@{version}' but the resolved "
                f"lock contract/PklProject.deps.json is missing or unparseable. "
                f"Run 'pkl project resolve' to regenerate the lock, then "
                f"'pkl eval -m . contract/app.pkl' to regenerate the artifacts. "
                f"Suppress with: // conformance: ignore[K003] <reason>"
            )
        else:
            resolved = lock.get(base)
            if resolved is None:
                message = (
                    f"contract/PklProject pins '{base}@{version}' but the "
                    f"dependency is absent from the resolved lock "
                    f"contract/PklProject.deps.json. Run 'pkl project resolve'. "
                    f"Suppress with: // conformance: ignore[K003] <reason>"
                )
            elif not _version_satisfied(version, resolved):
                message = (
                    f"contract/PklProject pins '{base}@{version}' but the lock "
                    f"contract/PklProject.deps.json resolved '@{resolved}'. The "
                    f"generated artifacts were built from the stale version. Run "
                    f"'pkl project resolve' then regenerate. "
                    f"Suppress with: // conformance: ignore[K003] <reason>"
                )
            else:
                continue

        suppressed, justification = _make_pkl_finding_suppressed(
            rule_id="K003", line=lineno, directives=directives
        )
        findings.append(
            Finding(
                rule_id="K003",
                file=rel,
                line=lineno,
                column=1,
                message=message,
                snippet=None,
                suppressed=suppressed,
                suppression_justification=justification,
            )
        )
    return findings


def _amends_line(text: str) -> int:
    """Return the 1-based line of the ``amends`` statement, or 1 if absent."""
    for lineno, line in enumerate(text.splitlines(), start=1):
        if line.lstrip().startswith("amends"):
            return lineno
    return 1


def _scan_missing_outputs(root: Path, present: set[str]) -> list[Finding]:
    """K004 — flag expected generated outputs that are absent while the contract
    exists."""
    if "contract/app.pkl" not in present:
        return []
    app_pkl = root / "contract" / "app.pkl"
    try:
        text = app_pkl.read_text(encoding="utf-8")
    except OSError:
        return []

    directives = _parse_pkl_directives(text)
    anchor = _amends_line(text)
    rel = "contract/app.pkl"
    findings: list[Finding] = []

    for expected in _EXPECTED_OUTPUTS:
        if (root / expected).is_file():
            continue
        suppressed, justification = _make_pkl_finding_suppressed(
            rule_id="K004", line=anchor, directives=directives
        )
        findings.append(
            Finding(
                rule_id="K004",
                file=rel,
                line=anchor,
                column=1,
                message=(
                    f"contract/app.pkl exists but the expected generated artifact "
                    f"'{expected}' is missing. Regenerate with "
                    f"'pkl eval -m . contract/app.pkl' and commit the result. "
                    f"Suppress with: // conformance: ignore[K004] <reason>"
                ),
                snippet=None,
                suppressed=suppressed,
                suppression_justification=justification,
            )
        )
    return findings


def _is_banner_bearing(rel: str) -> bool:
    """True for a generated text artifact expected to carry a provenance banner.

    ``atlan.yaml`` / ``app.yaml`` at the repo root, and any ``.py`` under
    ``app/generated/`` other than the (empty) ``__init__.py``.  ``.json`` outputs
    are excluded — JSON has no comment syntax to carry a banner.
    """
    if rel in ("atlan.yaml", "app.yaml"):
        return True
    if not rel.startswith("app/generated/"):
        return False
    if not rel.endswith(".py"):
        return False
    return Path(rel).name != "__init__.py"


def _scan_banners(root: Path, present: set[str]) -> list[Finding]:
    """K005 — flag banner-bearing generated artifacts whose banner is missing."""
    if "contract/app.pkl" not in present:
        return []
    findings: list[Finding] = []
    for rel in sorted(present):
        if "__pycache__" in rel or not _is_banner_bearing(rel):
            continue
        try:
            text = (root / rel).read_text(encoding="utf-8")
        except OSError:
            continue
        if _has_banner(text):
            continue
        suppressed, justification = _hash_suppressed(text, "K005")
        findings.append(
            Finding(
                rule_id="K005",
                file=rel,
                line=1,
                column=1,
                message=(
                    f"Generated artifact '{rel}' is missing its "
                    f"'DO NOT EDIT' provenance banner — it was likely hand-authored "
                    f"or hand-edited and will drift from the contract on the next "
                    f"regeneration. Regenerate with 'pkl eval -m . contract/app.pkl', "
                    f"or if this file is intentionally hand-maintained, suppress "
                    f"with: # conformance: ignore[K005] <reason>"
                ),
                snippet=None,
                suppressed=suppressed,
                suppression_justification=justification,
            )
        )
    return findings


# ---------------------------------------------------------------------------
# Scan API
# ---------------------------------------------------------------------------


def scan_all(paths: list[Path], root: Path) -> list[Finding]:
    """Run K003/K004/K005 over the discovered *paths*.

    ``paths`` is the post-exclusion discovery list; membership gates each rule so
    excluding ``contract/`` disables the whole check for a repo.
    """
    present: set[str] = set()
    for p in paths:
        try:
            present.add(p.relative_to(root).as_posix())
        except ValueError:
            continue
    findings: list[Finding] = []
    findings.extend(_scan_lock_drift(root, present))
    findings.extend(_scan_missing_outputs(root, present))
    findings.extend(_scan_banners(root, present))
    return findings


def scan_path(path: Path, root: Path) -> list[Finding]:
    """Single-path shim so the registration satisfies ``CheckRegistration``.

    K003/K004/K005 are inherently repo-level; scanning a lone file routes through
    :func:`scan_all` with that one path.
    """
    return scan_all([path], root)


def discover(root: Path) -> list[Path]:
    """Return the contract source + generated artifacts under *root*.

    Only the repo-root ``contract/``, ``atlan.yaml`` / ``app.yaml``, and
    ``app/generated/`` are considered — the app layout — so nested toolkit
    examples in the SDK are never scanned (and APP scope drops them anyway).
    """
    out: list[Path] = []
    contract = root / "contract"
    for name in ("PklProject", "PklProject.deps.json", "app.pkl"):
        candidate = contract / name
        if candidate.is_file():
            out.append(candidate)
    for name in ("atlan.yaml", "app.yaml"):
        candidate = root / name
        if candidate.is_file():
            out.append(candidate)
    generated = root / "app" / "generated"
    if generated.is_dir():
        out.extend(p for p in sorted(generated.rglob("*")) if p.is_file())
    return out


main = make_cli_main(
    scan_all=scan_all,
    description=(
        "K003/K004/K005 generated-artifact freshness: flag a stale Pkl lock "
        "(PklProject vs PklProject.deps.json), missing generated outputs, or a "
        "generated artifact whose provenance banner has been stripped (BLDX-1414)."
    ),
    discover=discover,
    default_scan_paths=(".",),
    default_tool_version=TOOL_VERSION,
)
"""CLI entry point for the generated-artifact freshness check."""


if __name__ == "__main__":
    sys.exit(main())
