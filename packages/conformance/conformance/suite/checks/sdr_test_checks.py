"""T-series SDR test-quality checks (T002–T003, DISTR-752).

Cross-artifact checks for SDR integration-test quality:

* ``T002`` — MissingSdrTestClass: apps declaring ``self_deployed_runtime: true``
  in ``atlan.yaml`` must exercise the SDR execution path from at least one test.
  Two harnesses satisfy this:

  - the legacy ``BaseSDRIntegrationTest`` subclass (boots a local Temporal dev
    server; runs in routine CI), or
  - the agnostic e2e harness — a ``BaseE2ETest`` subclass (usually via a
    generated ``*GeneratedE2EBase``) that runs in **agent mode**, detected by a
    class-level ``mode = RunMode.AGENT`` assignment.  ``RunMode.AGENT`` *is* the
    self-deployed-runtime path; ``RunMode.DIRECT`` is not and does not count.

  Without either there is no test that drives the SDR path (credential routing
  and upload behaviour in an SDR-like environment) at all.

* ``T003`` — SdrTestLegacyAgentSpec: a ``BaseSDRIntegrationTest`` subclass that
  sets ``agent_spec_template`` but not ``manifest_path`` bypasses manifest
  validation — the hand-crafted spec can satisfy SDR requirements even when the
  committed ``manifest.json`` is broken, which is exactly the MSSQL regression
  (atlan-mssql-app#177, DISTR-752).  Subclasses must switch to ``manifest_path``
  so the test reads inputs from the committed manifest.

``scan_path`` is a no-op — T002 requires cross-file context (does *any* test
file declare the subclass?) plus ``atlan.yaml`` for the SDR gate.  The runner
must call ``scan_all``.

Discovery
---------
Unlike :mod:`conformance.suite.checks.integration_marking` (T001), which targets
only ``tests/integration/``, this series walks the entire ``tests/`` tree: an
``BaseSDRIntegrationTest`` subclass or an agent-mode e2e class may live under
``tests/integration/``, ``tests/e2e/``, or a helper module — not just in a
top-level integration test file.

Note: the ``mode = RunMode.AGENT`` signal must appear in a test class under
``tests/`` (all current apps set it there).  An app that sets ``mode`` only in a
generated base under ``app/`` would not be seen, since discovery scans ``tests/``
only.
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


def _is_agent_mode_value(val: ast.expr) -> bool:
    """Return True when *val* is the ``RunMode.AGENT`` enum member.

    Matches the attribute form only (``RunMode.AGENT`` — any ``X.AGENT`` access).
    The bare string ``"agent"`` is intentionally *not* accepted: ``mode = "agent"``
    is overloaded (AI-agent tests, user-agent, …) and would be a false-negative
    surface, and the whole fleet uses the enum member.
    """
    return isinstance(val, ast.Attribute) and val.attr == "AGENT"


def _is_agent_mode_e2e(node: ast.ClassDef) -> bool:
    """Return True when the class runs the e2e harness in agent (SDR) mode.

    The agnostic e2e harness (``application_sdk.testing.e2e.BaseE2ETest``) drives
    the self-deployed-runtime path when a subclass sets ``mode = RunMode.AGENT``.
    We key off that class-level assignment rather than the base class, because the
    concrete base is usually a generated ``*GeneratedE2EBase`` under ``app/`` that
    the ``tests/`` scan never sees.  ``RunMode.DIRECT`` is *not* SDR coverage.
    Handles both ``mode = RunMode.AGENT`` and ``mode: RunMode = RunMode.AGENT``.
    """
    for item in node.body:
        if isinstance(item, ast.Assign):
            targets: list[ast.expr] = item.targets
            val = item.value
        elif isinstance(item, ast.AnnAssign):
            targets = [item.target]
            val = item.value
        else:
            continue
        if val is None:
            continue
        if not any(isinstance(t, ast.Name) and t.id == "mode" for t in targets):
            continue
        if _is_agent_mode_value(val):
            return True
    return False


def _is_nonempty_literal(val: ast.expr) -> bool:
    """Return True when *val* is a non-empty literal (constitutes a real assignment).

    Accepts Constant (truthy value), Dict (non-empty keys), List/Tuple/Set
    (non-empty elts), JoinedStr (non-empty f-string), and any other expression
    node (Name, Call, Subscript, …) — which is always considered non-empty.
    """
    if isinstance(val, ast.Constant):
        return bool(val.value)
    if isinstance(val, ast.Dict):
        return bool(val.keys)
    if isinstance(val, (ast.List, ast.Tuple, ast.Set)):
        return bool(val.elts)
    if isinstance(val, ast.JoinedStr):
        return bool(val.values)
    return True


def _class_var_state(node: ast.ClassDef) -> tuple[bool, bool]:
    """Return (has_agent_spec_template, has_manifest_path) for the class body.

    A ClassVar is considered *set* when it is assigned any non-empty literal
    in the class body (Constant, Dict, List, Tuple, JoinedStr, or any other
    expression).  Handles both plain ``Assign`` and ``AnnAssign`` nodes so
    that ``agent_spec_template: dict[str, Any] = {...}`` (which mirrors the
    base-class annotation) is detected correctly.
    """
    has_agent_spec = False
    has_manifest_path = False
    for item in node.body:
        if isinstance(item, ast.Assign):
            for target in item.targets:
                if not isinstance(target, ast.Name):
                    continue
                val = item.value
                if not _is_nonempty_literal(val):
                    continue
                if target.id == "agent_spec_template":
                    has_agent_spec = True
                elif target.id == "manifest_path":
                    has_manifest_path = True
        elif isinstance(item, ast.AnnAssign):
            target = item.target
            val = item.value
            if not isinstance(target, ast.Name) or val is None:
                continue
            if not _is_nonempty_literal(val):
                continue
            if target.id == "agent_spec_template":
                has_agent_spec = True
            elif target.id == "manifest_path":
                has_manifest_path = True
    return has_agent_spec, has_manifest_path


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
    """Scan one test file for T003 findings and detect any SDR coverage.

    SDR coverage is either a legacy ``BaseSDRIntegrationTest`` subclass or an
    agent-mode e2e class (``mode = RunMode.AGENT``).  T003 applies only to the
    legacy subclass — its ``agent_spec_template`` / ``manifest_path`` semantics
    do not exist on the e2e harness.

    Returns ``(findings, has_sdr_coverage)``.
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
    has_sdr_coverage = False

    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        is_legacy = _is_sdr_subclass(node)
        if is_legacy or _is_agent_mode_e2e(node):
            has_sdr_coverage = True
        if not is_legacy:
            continue
        has_agent_spec, has_manifest_path = _class_var_state(node)
        if has_agent_spec and not has_manifest_path:
            findings.append(
                make_finding(
                    filename=rel_str,
                    rule_id=RULE_T003,
                    node=node,
                    message=(
                        f"class {node.name!r} subclasses BaseSDRIntegrationTest and "
                        "sets agent_spec_template but not manifest_path. The hand-crafted "
                        "agent spec can satisfy SDR requirements even when the committed "
                        "manifest.json is broken — the MSSQL regression (DISTR-752) slipped "
                        "through exactly this way. Switch to manifest_path so the test reads "
                        "inputs from the committed manifest and validates agent_json and "
                        "all other DAG inputs. "
                        "See application_sdk.testing.sdr.base.BaseSDRIntegrationTest."
                    ),
                    directives=directives,
                )
            )

    return findings, has_sdr_coverage


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
    any_sdr_coverage = False

    for path in paths:
        file_findings, has_sdr = _scan_file(path, root)
        findings.extend(file_findings)
        if has_sdr:
            any_sdr_coverage = True

    if not any_sdr_coverage and _is_sdr_app(root):
        findings.append(
            Finding(
                rule_id=RULE_T002,
                file="atlan.yaml",
                line=1,
                column=1,
                message=(
                    "atlan.yaml declares self_deployed_runtime: true but no test "
                    "exercises the SDR path under tests/. SDR apps must drive the "
                    "self-deployed-runtime path from a test. Satisfy this with either: "
                    "(1) an agent-mode e2e test — a BaseE2ETest subclass (from "
                    "application_sdk.testing.e2e, usually via a generated "
                    "*GeneratedE2EBase) with mode = RunMode.AGENT (recommended); or "
                    "(2) a legacy BaseSDRIntegrationTest subclass from "
                    "application_sdk.testing.sdr.base with manifest_path set to the "
                    "committed manifest.json."
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
