"""T014–T015 — coverage-config integrity (BLDX-1400).

A coverage percentage is only a meaningful signal if the gate that produces
it can actually fail (T014) and actually measures the code that ships
(T015). Both read ``[tool.coverage.*]`` from ``pyproject.toml`` — TOML,
parsed deterministically via :mod:`tomllib`.

* ``T014`` CoverageGateDisabled — ``[tool.coverage]`` is configured (any
  ``run``/``report``/... subtable present) but
  ``[tool.coverage.report].fail_under`` is absent or ``0`` — coverage is
  measured and reported but can never fail a run.
* ``T015`` CoverageOmitsProductCode — ``[tool.coverage.run].omit`` contains a
  pattern targeting real code under ``app/`` (not one of the recognised
  legitimate exclusions: ``app/generated/**``, test infra living under
  ``app/``), or ``[tool.coverage.run].source`` is configured but excludes the
  ``app/`` tree entirely.

Discovery
---------
Scans the repo-root ``pyproject.toml``. If absent, both rules no-op — a repo
with no coverage config at all is a different (unconfigured, not
misconfigured) concern, out of scope for this check. T014 additionally reads
every ``.github/workflows/*.yml``/``*.yaml`` in the repo for a CI-declared
coverage floor (see "CI-declared fail-under" below) — coverage.py's CLI flag
always overrides ``pyproject.toml``, so a repo can have a fully-enforced gate
with ``fail_under`` absent from ``pyproject.toml`` entirely.

CI-declared fail-under
-----------------------
Two conventions are in use across connector apps, both matched:

* ``connector-unit-tests`` composite action: a ``fail-under:`` input to the
  ``uses: atlanhq/application-sdk/.github/actions/connector-unit-tests@...``
  step, e.g. ``fail-under: "60"``.
* ``tests-reusable.yaml`` composite workflow: a ``--cov-fail-under=N`` flag
  embedded in the ``pytest-args`` input's value.

If either pattern matches anywhere under ``.github/workflows/``, that value
is the effective floor — it wins over ``[tool.coverage.report].fail_under``
in ``pyproject.toml`` (matching coverage.py's own CLI-overrides-config
precedence). T014 only fires when *neither* source declares a non-zero
floor. When multiple workflow files or matches are found, the first
non-``None`` match (files sorted by name) is used — connector apps have at
most one CI-enforced floor in practice, so this is not expected to matter.

Inline suppression
-------------------
Add ``# conformance: ignore[T014] <reason>`` / ``[T015]`` on the same line as
(or the line directly above) the relevant table header or key — see
``_ast_common.parse_toml_suppressions`` for the exact TOML-comment grammar
(identical directive syntax to the AST series; TOML's ``#`` comments slot in
naturally).

Known coverage limits (intentional — biased toward zero false positives):

* ``omit``/``source`` values that aren't string literals (e.g. built from a
  variable or f-string — not expressible in static TOML anyway) are ignored.
* T015's ``source``-excludes-``app/`` check only fires when ``source`` is
  configured at all; an absent ``source`` key (coverage.py's default,
  effectively "everything importable") is not flagged.
* T014's CI cross-check only recognises the two conventions above. A
  bespoke workflow enforcing coverage some other way (a custom script, a
  third composite action) is invisible to this check and may still produce
  a false positive — suppress with a reason in that case.
"""

from __future__ import annotations

import re
import sys
import tomllib
from pathlib import Path

from conformance.suite.checks._ast_common import (
    make_cli_main,
    make_toml_finding,
    parse_toml_suppressions,
)
from conformance.suite.schema.findings import Finding

SERIES = "T"
RULE_T014 = "T014"
RULE_T015 = "T015"

# Matches a `[tool.coverage]` / `[tool.coverage.report]` / ... table header,
# used only to pick a human-friendly anchor line for the finding.
_COVERAGE_TABLE_RE = re.compile(r"^\[tool\.coverage(\.\w+)?\]\s*(#.*)?$")

# The two CI-declared-floor conventions in use across connector apps — see
# "CI-declared fail-under" in the module docstring.
_CI_FAIL_UNDER_RE = re.compile(
    r"""
    fail-under:\s*["']?(?P<input_value>\d+(?:\.\d+)?)   # connector-unit-tests input
    |
    --cov-fail-under[=\s]+["']?(?P<flag_value>\d+(?:\.\d+)?)  # pytest-cov flag
    """,
    re.VERBOSE,
)

__all__ = ["SERIES", "discover", "main", "scan_path", "scan_text"]


def discover(root: Path) -> list[Path]:
    """Discover the repo-root ``pyproject.toml``.

    Returns ``[]`` when absent, so this check simply no-ops on repos without
    one (there is no other file this series reads). T014 separately reads
    ``.github/workflows/`` via :func:`_ci_declared_fail_under` — that scan is
    driven by ``scan_path`` (which has ``root``), not by this discovery list,
    since it's cross-file context for a single ``pyproject.toml`` finding
    rather than an independently-scannable file.
    """
    pyproject = root / "pyproject.toml"
    return [pyproject] if pyproject.is_file() else []


def _ci_declared_fail_under(root: Path) -> float | None:
    """Return the coverage floor declared in the repo's own CI workflows, if any.

    Scans every ``.github/workflows/*.yml``/``*.yaml`` for either the
    ``connector-unit-tests`` action's ``fail-under:`` input or a
    ``--cov-fail-under=N`` flag embedded in a ``tests-reusable.yaml``
    ``pytest-args`` override (see the module docstring). Returns the first
    match found (files sorted by name), or ``None`` if neither convention
    appears anywhere.
    """
    workflows_dir = root / ".github" / "workflows"
    if not workflows_dir.is_dir():
        return None
    for path in sorted(workflows_dir.glob("*.yml")) + sorted(
        workflows_dir.glob("*.yaml")
    ):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        m = _CI_FAIL_UNDER_RE.search(text)
        if m is None:
            continue
        value = m.group("input_value") or m.group("flag_value")
        return float(value)
    return None


def _anchor_line(text: str) -> int:
    """Return the best-effort line to anchor a finding on.

    Prefers ``[tool.coverage.report]`` (T014's home), then
    ``[tool.coverage.run]`` (T015's home), then the bare ``[tool.coverage]``
    table, falling back to line 1 when no coverage table header is found at
    all (defensive — ``scan_text`` only calls this once it has already
    confirmed a ``coverage`` dict exists in the parsed TOML). Used only as a
    fallback when :func:`_key_line` can't find the specific offending key.
    """
    found: dict[str, int] = {}
    for i, line in enumerate(text.splitlines(), start=1):
        m = _COVERAGE_TABLE_RE.match(line.strip())
        if not m:
            continue
        suffix = m.group(1) or ""
        found.setdefault(suffix, i)
    for suffix in (".report", ".run", ""):
        if suffix in found:
            return found[suffix]
    return 1


def _key_line(text: str, key: str, fallback: int) -> int:
    """Return the line number of a top-level ``key = ...`` assignment.

    Anchoring on the specific key (rather than only the enclosing table
    header) lets a suppression directive be placed directly above the
    offending line — the natural place a reviewer would look — rather than
    forcing it onto the table header line.
    """
    pattern = re.compile(rf"^\s*{re.escape(key)}\s*=")
    for i, line in enumerate(text.splitlines(), start=1):
        if pattern.match(line):
            return i
    return fallback


# ---------------------------------------------------------------------------
# T015 — omit / source analysis
# ---------------------------------------------------------------------------

# An omit pattern rooted at app/ is legitimate when it targets generated
# contract artifacts or test infra that happens to live under app/ in some
# layouts — anything else rooted there is real product code.
_LEGIT_OMIT_MARKERS: tuple[str, ...] = ("generated",)


def _is_legit_app_omit(pattern: str) -> bool:
    norm = pattern.strip().strip("\"'").replace("\\", "/")
    segments = [seg for seg in norm.split("/") if seg]
    if any(marker in segments for marker in _LEGIT_OMIT_MARKERS):
        return True
    tail = norm.rsplit("/", 1)[-1]
    return tail.startswith(("test_", "conftest")) or tail.endswith("_test.py")


def _offending_omit_patterns(omit: object) -> list[str]:
    """Return ``omit`` entries that target real product code under app/."""
    if not isinstance(omit, list):
        return []
    offenders: list[str] = []
    for entry in omit:
        if not isinstance(entry, str):
            continue
        norm = entry.strip().strip("\"'").replace("\\", "/")
        if norm.startswith("app/") and not _is_legit_app_omit(entry):
            offenders.append(entry)
    return offenders


def _source_excludes_app(source: object) -> bool:
    """True when ``source`` is configured but no entry covers the app/ tree."""
    if not isinstance(source, list) or not source:
        return False
    for entry in source:
        if not isinstance(entry, str):
            continue
        norm = entry.strip().strip("\"'").replace("\\", "/")
        if norm in ("app", "app/*", "app/**") or norm.startswith("app/"):
            return False
    return True


# ---------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------


def _t014_message() -> str:
    return (
        "[tool.coverage] is configured but [tool.coverage.report].fail_under is "
        "absent or 0, and no CI workflow declares a fail-under/--cov-fail-under "
        "override — coverage is measured and reported but can never fail a run. "
        "Set fail_under to the repo's current measured percentage (or higher) and "
        "ratchet it up over time; the agreed target for unit tests is 90-100%."
    )


def _t015_message(offenders: list[str], excludes_app: bool) -> str:
    parts: list[str] = []
    if offenders:
        parts.append(
            "[tool.coverage.run].omit excludes real product code under app/: "
            + ", ".join(repr(o) for o in offenders)
        )
    if excludes_app:
        parts.append(
            "[tool.coverage.run].source is configured but no entry covers the "
            "app/ tree, so coverage never measures the app's own product code"
        )
    return (
        "; ".join(parts)
        + ". This inflates the reported coverage percentage without a single "
        "additional test being written. Narrow the exclusion to generated "
        "artifacts (app/generated/**) or test infra only."
    )


# ---------------------------------------------------------------------------
# Scan API
# ---------------------------------------------------------------------------


def scan_text(
    text: str, file: str, *, ci_fail_under: float | None = None
) -> list[Finding]:
    """Scan ``pyproject.toml`` *text* and return all T014–T015 findings.

    *ci_fail_under* is the coverage floor declared in the repo's own CI
    workflows (see :func:`_ci_declared_fail_under`), if any. When present it
    is the effective floor for T014 regardless of what ``pyproject.toml``
    says — coverage.py's CLI flag always overrides the config file. Callers
    that scan ``pyproject.toml`` in isolation (e.g. meta-tests, the CLI's
    single-file mode) simply omit it, which reproduces the pre-existing
    pyproject-only behaviour.
    """
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return []
    if not isinstance(data, dict):
        return []

    tool = data.get("tool")
    coverage = tool.get("coverage") if isinstance(tool, dict) else None
    if not isinstance(coverage, dict) or not coverage:
        return []

    suppressions = parse_toml_suppressions(text)
    table_anchor = _anchor_line(text)
    findings: list[Finding] = []

    report = coverage.get("report")
    pyproject_fail_under = (
        report.get("fail_under") if isinstance(report, dict) else None
    )
    effective_fail_under = (
        ci_fail_under if ci_fail_under is not None else pyproject_fail_under
    )
    if not effective_fail_under:
        findings.append(
            make_toml_finding(
                rule_id=RULE_T014,
                file=file,
                line=_key_line(text, "fail_under", table_anchor),
                column=1,
                message=_t014_message(),
                suppressions=suppressions,
            )
        )

    run = coverage.get("run") if isinstance(coverage.get("run"), dict) else {}
    offenders = (
        _offending_omit_patterns(run.get("omit")) if isinstance(run, dict) else []
    )
    excludes_app = (
        _source_excludes_app(run.get("source")) if isinstance(run, dict) else False
    )
    if offenders or excludes_app:
        key = "omit" if offenders else "source"
        findings.append(
            make_toml_finding(
                rule_id=RULE_T015,
                file=file,
                line=_key_line(text, key, table_anchor),
                column=1,
                message=_t015_message(offenders, excludes_app),
                suppressions=suppressions,
            )
        )

    return findings


def scan_path(path: Path, root: Path) -> list[Finding]:
    """Scan a single ``pyproject.toml`` file for T014–T015 findings."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    try:
        rel = path.relative_to(root)
    except ValueError:
        rel = path
    return scan_text(text, str(rel), ci_fail_under=_ci_declared_fail_under(root))


main = make_cli_main(
    scan_text,
    description="T014-T015: scan pyproject.toml for coverage-config integrity issues.",
    discover=discover,
    default_scan_paths=("pyproject.toml",),
)
"""CLI entry point for the T014–T015 coverage-config checks."""


if __name__ == "__main__":
    sys.exit(main())
