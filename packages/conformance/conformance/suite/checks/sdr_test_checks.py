"""T-series SDR test-quality checks (T002–T003, DISTR-752).

Cross-artifact checks for SDR integration-test quality:

* ``T002`` — MissingSdrTestClass: apps declaring ``self_deployed_runtime: true``
  in ``atlan.yaml`` must have a ``BaseSDRIntegrationTest`` subclass somewhere
  in their test suite.  Without one there is no test that validates manifest
  inputs, credential routing, or upload behaviour in an SDR-like environment.

* ``T003`` — DeprecatedSdrHarness: ``BaseSDRIntegrationTest`` is deprecated and
  will be removed in v4.0.  Any subclass under ``tests/`` is flagged and told to
  migrate to the agnostic e2e harness — a ``BaseE2ETest`` subclass (usually via a
  generated ``*GeneratedE2EBase``) run in agent mode (``mode = RunMode.AGENT``),
  which validates the self-deployed-runtime path end to end against a real tenant.

``scan_path`` is a no-op — T002 requires cross-file context (does *any* test
file declare the subclass?) plus ``atlan.yaml`` for the SDR gate.  The runner
must call ``scan_all``.

Discovery
---------
Unlike :mod:`conformance.suite.checks.integration_marking` (T001), which targets
only ``tests/integration/``, this series walks the entire ``tests/`` tree: an
``BaseSDRIntegrationTest`` subclass may live in a unit-test helper or in an
integration helper module, not just in a top-level integration test file.
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

from conformance.suite.checks._ast_common import (
    _parse_directives,
    make_cli_main,
    make_finding,
)
from conformance.suite.schema.findings import Finding

SERIES = "T"
RULE_T002 = "T002"
RULE_T003 = "T003"

_SDR_FLAG_RE = re.compile(
    r"^self_deployed_runtime:\s*(true|false)\b",
    re.MULTILINE | re.IGNORECASE,
)
_BASE_SDR_NAMES: frozenset[str] = frozenset({"BaseSDRIntegrationTest"})

__all__ = ["SERIES", "discover", "main", "scan_all", "scan_path"]


def discover(root: Path) -> list[Path]:
    """Walk tests/ for all Python source files.

    Broader than :func:`integration_marking.discover` (which targets only
    ``tests/integration/``) because a ``BaseSDRIntegrationTest`` subclass may
    live in any helper or fixture module under the test tree.
    """
    base = root / "tests"
    if not base.is_dir():
        return []
    paths: list[Path] = []
    for path in base.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        paths.append(path)
    return sorted(paths)


def _base_names(bases: list[ast.expr]) -> set[str]:
    names: set[str] = set()
    for base in bases:
        if isinstance(base, ast.Name):
            names.add(base.id)
        elif isinstance(base, ast.Attribute):
            names.add(base.attr)
    return names


def _is_sdr_subclass(node: ast.ClassDef) -> bool:
    return bool(_base_names(node.bases) & _BASE_SDR_NAMES)


def _is_sdr_app(root: Path) -> bool:
    atlan_yaml = root / "atlan.yaml"
    if not atlan_yaml.is_file():
        return False
    try:
        text = atlan_yaml.read_text(encoding="utf-8")
    except OSError:
        return False
    m = _SDR_FLAG_RE.search(text)
    return m is not None and m.group(1).lower() == "true"


def _scan_file(path: Path, root: Path) -> tuple[list[Finding], bool]:
    """Scan one test file: emit T003 for each deprecated-harness subclass and
    report whether the file declares any SDR subclass (for the T002 gate).

    Returns ``(findings, has_sdr_subclass)``.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return [], False

    try:
        tree = ast.parse(text, filename=str(path))
    except SyntaxError:
        return [], False

    try:
        rel = path.relative_to(root)
    except ValueError:
        rel = path
    rel_str = str(rel)

    directives = _parse_directives(text)
    findings: list[Finding] = []
    has_sdr_subclass = False

    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        if not _is_sdr_subclass(node):
            continue
        has_sdr_subclass = True
        findings.append(
            make_finding(
                filename=rel_str,
                rule_id=RULE_T003,
                node=node,
                message=(
                    f"class {node.name!r} subclasses BaseSDRIntegrationTest, which is "
                    "deprecated and will be removed in v4.0. Migrate to the agnostic "
                    "e2e harness: a BaseE2ETest subclass (from application_sdk.testing.e2e, "
                    "usually via a generated *GeneratedE2EBase) with mode = RunMode.AGENT. "
                    "Add the agent-mode e2e test first and confirm T002 passes, then delete "
                    "this SDR test. See application_sdk.testing.e2e.BaseE2ETest."
                ),
                directives=directives,
            )
        )

    return findings, has_sdr_subclass


def scan_path(path: Path, root: Path) -> list[Finding]:  # noqa: ARG001
    """No-op: T002/T003 require cross-artifact analysis; use scan_all."""
    return []


def scan_all(paths: list[Path], root: Path) -> list[Finding]:
    """Check T002 and T003 for the repo at root.

    Parameters
    ----------
    paths:
        Python source files under ``tests/`` (as returned by :func:`discover`).
    root:
        Repo root — used to locate ``atlan.yaml`` for the T002 SDR gate.
    """
    findings: list[Finding] = []
    any_sdr_subclass = False

    for path in paths:
        file_findings, has_sdr = _scan_file(path, root)
        findings.extend(file_findings)
        if has_sdr:
            any_sdr_subclass = True

    if not any_sdr_subclass and _is_sdr_app(root):
        findings.append(
            Finding(
                rule_id=RULE_T002,
                file="atlan.yaml",
                line=1,
                column=1,
                message=(
                    "atlan.yaml declares self_deployed_runtime: true but no "
                    "BaseSDRIntegrationTest subclass was found under tests/. SDR apps "
                    "must have an SDR integration test to validate manifest inputs, "
                    "credential routing, and upload behaviour in an SDR-like environment. "
                    "Subclass BaseSDRIntegrationTest from application_sdk.testing.sdr.base "
                    "and set manifest_path to the path of the committed manifest.json."
                ),
            )
        )

    return findings


main = make_cli_main(
    scan_all=scan_all,
    discover=discover,
    description=(
        "T002/T003: SDR test-quality checks — missing SDR test class (T002) "
        "and legacy agent_spec_template usage (T003)."
    ),
)

if __name__ == "__main__":
    sys.exit(main())
