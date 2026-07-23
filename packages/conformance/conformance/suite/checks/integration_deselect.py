"""T018 IntegrationTierDeselectedByAddopts — flag an ``addopts`` ``-m`` filter
that removes tests from the directory-scoped integration tier.

Since the Unit/Integration split (application-sdk#2852), the reusable Tests
workflow runs the integration tier **by directory** — the CI job invokes
``pytest tests/integration/`` with no ``-m`` re-selection (the unit tier is a
separate ``pytest tests/unit`` job). A ``[tool.pytest.ini_options].addopts``
``-m 'not <marker>'`` deselection still applies to *that* invocation, so any
test under ``tests/integration/`` carrying a deselected marker is removed from
the only job meant to run it:

* deselecting **every** collectable integration test → the job collects nothing
  and fails with ``pytest`` exit 5 (``no tests ran``);
* deselecting **some** → those tests run in no tier at all (the unit job never
  collects ``tests/integration/``; the integration job deselects them).

This is the inverse of :mod:`integration_marking` (T001), which wants the
``integration`` marker *present*. Both hold at once: keep the marker, but don't
``addopts``-deselect it — the directory is the tier boundary (see
``atlan-mysql-app`` / ``atlan-metabase-app``: marker present, no deselect).

Reuse
-----
The ``-m`` deselection parser and the per-test marker resolution are the
canonical implementations in :mod:`integration_marking` (T001), imported here so
both rules read markers identically rather than diverging.

Inline suppression
------------------
Add ``# conformance: ignore[T018] <reason>`` on the ``addopts`` line (or the
line directly above it).

Known coverage limits (intentional — biased toward zero false positives at WARN
tier; documented rather than silently capped):

* **``-m`` handling is syntactic**, inherited from T001: only ``not <marker>``
  terms are extracted (the standard ``not a and not b`` deselection form). A
  non-standard selection expression that doesn't use ``not`` is treated as
  deselecting nothing.
* **Marker resolution is syntactic** (module ``pytestmark`` / class decorator or
  ``pytestmark`` / function decorator); dynamically applied markers
  (``add_marker``, ``pytest.param(marks=...)``) are not resolved.
* **Collection mirrors pytest defaults** (``test*`` functions, ``Test*``
  classes, ``test_*.py`` / ``*_test.py`` files under ``tests/integration/``).
"""

from __future__ import annotations

import ast
import re
import sys
import tomllib
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from conformance.suite.checks._ast_common import (
    is_test_class,
    is_test_function,
    make_cli_main,
    make_toml_finding,
    parse_toml_suppressions,
)

# Canonical marker/addopts parsers live in the T001 check — import them so the
# two rules never diverge on how a marker or a `-m` deselection is read.
from conformance.suite.checks.integration_marking import (
    _decorator_markers,
    _deselected_markers,
    _pytestmark_markers_in_body,
)
from conformance.suite.checks.integration_marking import discover as _discover_tests
from conformance.suite.schema.findings import Finding

SERIES = "T"
RULE_T018 = "T018"

# ``addopts = ...`` assignment line (for anchoring the finding + suppressions).
_ADDOPTS_LINE_RE = re.compile(r"^\s*addopts\s*=")

# Cap how many test labels are listed in the message so a whole-suite
# deselection doesn't produce an unreadable wall of names.
_MAX_LISTED = 5

__all__ = [
    "SERIES",
    "IntegrationTest",
    "deselected_labels",
    "discover",
    "main",
    "scan_all",
    "scan_path",
    "scan_text",
]


@dataclass(frozen=True)
class IntegrationTest:
    """A collectable test under ``tests/integration/`` and its effective markers.

    ``markers`` is the union of module ``pytestmark``, enclosing-class markers,
    and the test's own decorator markers — mirroring pytest's marker hierarchy.
    """

    label: str
    markers: frozenset[str]


# ---------------------------------------------------------------------------
# Pure core (filesystem-free — unit-testable)
# ---------------------------------------------------------------------------


def deselected_labels(
    tests: Iterable[IntegrationTest], deselected_markers: Iterable[str]
) -> list[str]:
    """Labels of *tests* removed by a ``-m 'not …'`` *deselected_markers* set.

    A test is removed when it carries at least one deselected marker — the
    behaviour of the standard ``not a and not b`` conjunction form.
    """
    deselected = frozenset(deselected_markers)
    if not deselected:
        return []
    return [t.label for t in tests if t.markers & deselected]


def _message(deselected_markers: frozenset[str], removed: list[str], total: int) -> str:
    markers = ", ".join(sorted(deselected_markers))
    if removed and len(removed) >= total:
        impact = (
            f"deselects all {total} collectable test(s) under tests/integration/, "
            "so the directory-scoped integration CI job "
            "('pytest tests/integration/') collects nothing and fails with pytest "
            "exit code 5 (no tests ran)"
        )
    else:
        impact = (
            f"deselects {len(removed)} of {total} collectable test(s) under "
            "tests/integration/, so those tests run in no tier at all (the unit "
            "job never collects tests/integration/, and the integration job "
            "deselects them)"
        )
    listed = ", ".join(removed[:_MAX_LISTED])
    if len(removed) > _MAX_LISTED:
        listed += f", … (+{len(removed) - _MAX_LISTED} more)"
    return (
        f"pyproject addopts '-m not …' (markers: {markers}) {impact}. "
        f"Removed: {listed}. Remove the addopts '-m' deselection and mark "
        "integration tests with the standard 'integration' marker — the directory "
        "is the tier boundary (see atlan-mysql-app / atlan-metabase-app). "
        "Service-dependent tests should self-skip at runtime, not be "
        "addopts-deselected. See T018."
    )


# ---------------------------------------------------------------------------
# TOML / filesystem helpers
# ---------------------------------------------------------------------------


def _addopts_value(text: str) -> object | None:
    """Return ``[tool.pytest.ini_options].addopts`` from *text*, or ``None``."""
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    tool = data.get("tool")
    pytest_tbl = tool.get("pytest") if isinstance(tool, dict) else None
    ini = pytest_tbl.get("ini_options") if isinstance(pytest_tbl, dict) else None
    return ini.get("addopts") if isinstance(ini, dict) else None


def _addopts_line(text: str) -> int:
    for i, line in enumerate(text.splitlines(), start=1):
        if _ADDOPTS_LINE_RE.match(line):
            return i
    return 1


def _integration_tests(root: Path) -> list[IntegrationTest]:
    """Collect ``tests/integration/`` tests and their effective markers.

    Reuses T001's ``discover`` (pytest default globs under tests/integration/)
    and its marker resolution so the two checks agree test-for-test.
    """
    tests: list[IntegrationTest] = []
    for path in _discover_tests(root):
        try:
            src = path.read_text(encoding="utf-8")
        except OSError:
            continue
        try:
            tree = ast.parse(src, filename=str(path))
        except SyntaxError:
            continue
        try:
            rel = path.relative_to(root)
        except ValueError:
            rel = path
        module_markers = _pytestmark_markers_in_body(tree.body)
        for node in tree.body:
            if is_test_function(node):
                markers = module_markers | _decorator_markers(node)
                tests.append(IntegrationTest(f"{rel}::{node.name}", frozenset(markers)))
            elif is_test_class(node):
                class_markers = (
                    module_markers
                    | _decorator_markers(node)
                    | _pytestmark_markers_in_body(node.body)
                )
                for sub in node.body:
                    if is_test_function(sub):
                        markers = class_markers | _decorator_markers(sub)
                        tests.append(
                            IntegrationTest(
                                f"{rel}::{node.name}::{sub.name}", frozenset(markers)
                            )
                        )
    return tests


# ---------------------------------------------------------------------------
# Scan API
# ---------------------------------------------------------------------------


def scan_text(
    text: str,
    file: str,
    *,
    integration_tests: Iterable[IntegrationTest] | None = None,
) -> list[Finding]:
    """Scan a ``pyproject.toml`` *text* for T018.

    *integration_tests* supplies the tests/integration/ collectables + markers
    (from the filesystem via :func:`scan_path`); a caller with only the TOML text
    (e.g. a unit test) passes them explicitly. When it is ``None`` the check has
    no test context and no-ops — the deselection is only a problem relative to
    what lives under ``tests/integration/``.
    """
    deselected = _deselected_markers(_addopts_value(text))
    if not deselected:
        return []
    tests = list(integration_tests) if integration_tests is not None else []
    removed = deselected_labels(tests, deselected)
    if not removed:
        return []
    return [
        make_toml_finding(
            rule_id=RULE_T018,
            file=file,
            line=_addopts_line(text),
            column=1,
            message=_message(deselected, removed, len(tests)),
            suppressions=parse_toml_suppressions(text),
        )
    ]


def scan_path(path: Path, root: Path) -> list[Finding]:
    """Scan the repo-root ``pyproject.toml`` for T018.

    Resolves the ``tests/integration/`` collectables from *root* so the finding
    reflects the real interaction between the app's ``addopts`` and its suite.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    try:
        rel = path.relative_to(root)
    except ValueError:
        rel = path
    return scan_text(text, str(rel), integration_tests=_integration_tests(root))


def scan_all(paths: list[Path], root: Path) -> list[Finding]:
    """Cross-file entry point (used by the standalone CLI)."""
    findings: list[Finding] = []
    for path in paths:
        if path.name == "pyproject.toml":
            findings.extend(scan_path(path, root))
    return findings


def discover(root: Path) -> list[Path]:
    """Discover the repo-root ``pyproject.toml`` (``[]`` when absent → no-op)."""
    pyproject = root / "pyproject.toml"
    return [pyproject] if pyproject.is_file() else []


main = make_cli_main(
    scan_all=scan_all,
    description=(
        "T018: flag a pyproject addopts '-m not <marker>' filter that deselects "
        "tests under tests/integration/ from the directory-scoped integration job."
    ),
    discover=discover,
    default_scan_paths=("pyproject.toml",),
)
"""CLI entry point for the T018 integration-deselect check."""


if __name__ == "__main__":
    sys.exit(main())
