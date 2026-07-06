"""T010–T013 — test-tier structure and placement (BLDX-1400).

The CI composite actions locate each test tier by directory convention:
``connector-unit-tests`` runs ``tests/unit``, the integration action commonly
scopes to ``tests/integration``, and the SDR/e2e actions default to
``tests/sdr``/``tests/e2e``/``tests/full_dag``. A tier that doesn't exist
where CI expects it silently contributes zero coverage — this is the
structural counterpart to T005–T009's per-test checks.

Per the agreed testing-tier architecture:

* ``T010`` MissingUnitTestSuite — no collectable tests under ``tests/unit/``.
  The universal floor; **not** exemptable.
* ``T011`` MissingIntegrationTestSuite — no collectable tests under
  ``tests/integration/``. Exemptable for scaffold/minimal apps.
* ``T012`` MissingE2ETestSuite — no collectable tests under ``tests/e2e/``.
  Exemptable the same way. Weakest of the three — e2e needs only one
  representative run.
* ``T013`` TestFileOutsideTierDir — a collectable test file lives under
  ``tests/`` but outside all four canonical tier directories (``unit``,
  ``integration``, ``e2e``, ``ui``).

Exemption mechanism (T011/T012 only)
-------------------------------------
``atlan.yaml`` is generated from the app's Pkl contract and must not be
hand-edited, so the opt-out for a scaffold/minimal app lives in the one
config file the conformance suite already reads for the D-series and for
T001's marker derivation — ``pyproject.toml``::

    [tool.conformance]
    exempt_test_tiers = ["integration", "e2e"]

``[tool.conformance]`` is the suite's canonical config-based exemption
surface for rules that detect something missing repo-wide (no single
offending line to suppress inline) — see ``docs/schema-contract.md`` §6.4
for the cross-rule convention this key follows.

Discovery
---------
Walks the entire ``tests/`` tree (same shape as ``test_quality.discover``) so
every tier — and any file placed outside all of them — is visible to the
single cross-artifact ``scan_all`` pass this series requires (tier presence is
inherently cross-file: you cannot know a tier is *missing* by looking at one
file at a time).

Inline suppression
-------------------
T013 findings are anchored on the offending file and support the usual
``# conformance: ignore[T013] <reason>`` directive. T010–T012 findings are
anchored on line 1 of ``pyproject.toml`` — since a same-line-or-above
directive only reaches line 1 from line 1 itself, a
``# conformance: ignore[T0NN] <reason>`` suppressing one of these must be the
file's first line. The ``exempt_test_tiers`` table entry above is the intended
mechanism for T011/T012; the inline directive is for a single ad hoc
exception.
"""

from __future__ import annotations

import ast
import sys
import tomllib
from pathlib import Path

from conformance.suite.checks._ast_common import (
    _IgnoreDirective,
    _parse_directives,
    is_collectable_test_file,
    is_test_class,
    is_test_function,
    make_cli_main,
    make_finding,
    make_toml_finding,
    parse_toml_suppressions,
)
from conformance.suite.schema.findings import Finding

SERIES = "T"
RULE_T010 = "T010"
RULE_T011 = "T011"
RULE_T012 = "T012"
RULE_T013 = "T013"

# The four canonical tier directories under tests/. "ui" has no dedicated
# missing-suite rule (UI testing is optional per the agreed architecture) but
# is a recognised placement for T013 purposes.
_TIER_DIRS: tuple[str, ...] = ("unit", "integration", "e2e", "ui")
# Tiers T010-T012 check presence for; unit (T010) is never exemptable.
_REQUIRED_TIERS: tuple[tuple[str, str], ...] = (
    (RULE_T011, "integration"),
    (RULE_T012, "e2e"),
)

__all__ = ["SERIES", "discover", "main", "scan_all", "scan_path"]


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def discover(root: Path) -> list[Path]:
    """Walk the entire ``tests/`` tree for Python source files.

    Returns ``[]`` when ``tests/`` is absent — a repo with no test directory
    at all still triggers T010 (see :func:`scan_all`), which reads
    ``tests/`` presence directly rather than depending on this list.
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


# ---------------------------------------------------------------------------
# Collection mirror
# ---------------------------------------------------------------------------


def _first_collected_test_node(tree: ast.Module) -> ast.stmt | None:
    """Return the first collected test node in *tree*, or ``None``.

    For a ``Test*`` class, prefers anchoring on an own ``test*`` method when
    one exists; otherwise, if the class subclasses anything, anchors on the
    class itself. A subclass with no own test methods is the canonical e2e
    pattern — e.g. ``class TestFooE2E(FooGeneratedE2EBase): ...`` inherits
    every ``test*`` method pytest actually collects and runs from the base
    class. Treating such a class as "no tests" would be a false positive
    against that exact reference pattern (see the T012 remediation guidance,
    which recommends it). A bare ``class Test*:`` with no base classes and no
    own test methods is a realistic non-issue (there is nothing for pytest to
    collect there either way) and is the only shape this still misses.
    """
    for node in tree.body:
        if is_test_function(node):
            return node
        if is_test_class(node):
            for sub in node.body:
                if is_test_function(sub):
                    return sub
            if node.bases:
                return node
    return None


def _scan_file(
    path: Path, root: Path
) -> tuple[ast.stmt, dict[int, _IgnoreDirective], str] | None:
    """Return ``(node, directives, rel_str)`` for *path*'s first collected test.

    ``None`` when the file isn't collectable by name, can't be read/parsed, or
    defines no collectable test — in every one of those cases the file
    contributes nothing to tier-presence or T013 analysis.
    """
    if not is_collectable_test_file(path.name):
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        tree = ast.parse(text, filename=str(path))
    except SyntaxError:
        return None
    node = _first_collected_test_node(tree)
    if node is None:
        return None
    try:
        rel = path.relative_to(root)
    except ValueError:
        rel = path
    return node, _parse_directives(text), str(rel)


# ---------------------------------------------------------------------------
# Exemption config
# ---------------------------------------------------------------------------


def _exempt_test_tiers(root: Path) -> frozenset[str]:
    """Read ``[tool.conformance].exempt_test_tiers`` from ``root/pyproject.toml``.

    Returns an empty set (nothing exempted) when the file is missing,
    unparseable, or declares no such list.
    """
    pyproject = root / "pyproject.toml"
    try:
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return frozenset()
    if not isinstance(data, dict):
        return frozenset()
    tiers = data.get("tool", {}).get("conformance", {}).get("exempt_test_tiers")
    if not isinstance(tiers, list):
        return frozenset()
    return frozenset(str(t).strip().lower() for t in tiers if str(t).strip())


# ---------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------


def _tier_missing_message(rule_id: str, tier: str) -> str:
    if rule_id == RULE_T010:
        return (
            f"No collectable pytest tests found under tests/{tier}/ — this is the "
            "universal floor of the testing-tier architecture and is not "
            "exemptable. Add tests/unit/test_<module>.py covering the app's helper "
            "functions and @task-decorated activities."
        )
    return (
        f"No collectable pytest tests found under tests/{tier}/. For a "
        "scaffold/minimal app with nothing to exercise at this tier yet, add "
        f'[tool.conformance] exempt_test_tiers = ["{tier}"] to pyproject.toml '
        "(state the reason in a comment above the table)."
    )


def _t013_message(rel: str) -> str:
    return (
        f"'{rel}' defines a collectable test but lives outside the four canonical "
        "tier directories (tests/unit, tests/integration, tests/e2e, tests/ui). "
        "Move it into the tier directory matching what it tests."
    )


# ---------------------------------------------------------------------------
# Scan API
# ---------------------------------------------------------------------------


def _tier_missing_finding(rule_id: str, tier: str, root: Path) -> Finding:
    pyproject = root / "pyproject.toml"
    try:
        text = pyproject.read_text(encoding="utf-8")
    except OSError:
        text = ""
    suppressions = parse_toml_suppressions(text) if text else {}
    return make_toml_finding(
        rule_id=rule_id,
        file="pyproject.toml",
        line=1,
        column=1,
        message=_tier_missing_message(rule_id, tier),
        suppressions=suppressions,
    )


def scan_path(path: Path, root: Path) -> list[Finding]:  # noqa: ARG001
    """No-op: T010–T013 require cross-artifact analysis; use scan_all."""
    return []


def scan_all(paths: list[Path], root: Path) -> list[Finding]:
    """Check T010–T013 for the repo at *root*.

    Parameters
    ----------
    paths:
        Python source files under ``tests/`` (as returned by :func:`discover`).
    root:
        Repo root — used to locate ``pyproject.toml`` for the T011/T012
        exemption and the T010–T012 suppression anchor.
    """
    tests_root = root / "tests"
    exempt = _exempt_test_tiers(root)
    findings: list[Finding] = []
    tier_has_tests: dict[str, bool] = {
        "unit": False,
        "integration": False,
        "e2e": False,
    }

    for path in paths:
        scanned = _scan_file(path, root)
        if scanned is None:
            continue
        node, directives, rel_str = scanned

        try:
            rel_parts = path.relative_to(tests_root).parts
        except ValueError:
            rel_parts = ()
        tier = rel_parts[0] if rel_parts and rel_parts[0] in _TIER_DIRS else None

        if tier is not None:
            if tier in tier_has_tests:
                tier_has_tests[tier] = True
        else:
            findings.append(
                make_finding(
                    filename=rel_str,
                    rule_id=RULE_T013,
                    node=node,
                    message=_t013_message(rel_str),
                    directives=directives,
                )
            )

    if not tier_has_tests["unit"]:
        findings.append(_tier_missing_finding(RULE_T010, "unit", root))

    for rule_id, tier in _REQUIRED_TIERS:
        if tier in exempt:
            continue
        if not tier_has_tests[tier]:
            findings.append(_tier_missing_finding(rule_id, tier, root))

    return findings


main = make_cli_main(
    scan_all=scan_all,
    discover=discover,
    description=(
        "T010-T013: scan tests/ for missing unit/integration/e2e suites and "
        "test files placed outside the canonical tier directories."
    ),
)

if __name__ == "__main__":
    sys.exit(main())
