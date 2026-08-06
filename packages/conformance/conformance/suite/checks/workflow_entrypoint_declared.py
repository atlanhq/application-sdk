"""T020 UndeclaredWorkflowEntrypoint — a workflow scenario must name its entrypoint.

``Scenario(api="workflow", ...)`` means "POST /workflows/v1/start". It does not
say *which* ``@entrypoint`` gets started. On an app that declares more than one,
an undeclared scenario silently starts the app's default entrypoint — so a suite
believing it covers the miner may in fact be exercising the crawler, and passing.

The second cost is that coverage becomes unreadable. Which product workflow a
suite exercises is recoverable only by reading the test's source and resolving
base-class defaults through the MRO. A survey of ten connectors found 2 tests out
of ~2000 stating it in any machine-readable form, which is why no tooling can
report per-workflow integration coverage today.

``Scenario.entrypoint`` (or the suite-wide ``BaseIntegrationTest.entrypoint``)
fixes both. This rule flags workflow scenarios that declare neither.

Scope
-----
Only fires for apps that declare **more than one** ``@entrypoint``. On a
single-entrypoint app the target is unambiguous and declaring it is noise, so
those repos see nothing.

A scenario is considered to declare its entrypoint when it passes
``entrypoint=``, when it passes an explicit ``endpoint=`` (a full override,
already unambiguous), or when its enclosing ``Test*`` class sets a class-level
``entrypoint``.

Inline suppression
------------------
``# conformance: ignore[T020] <reason>`` on the ``Scenario(`` line or the
comment-only line directly above it.

Known coverage limits (intentional — biased toward zero false positives):

* Resolution is syntactic. A scenario built by a helper function, or spread via
  ``**kwargs`` / list comprehension, is not inspected and never flagged.
* Class-level inheritance is resolved only within the same file; a suite whose
  ``entrypoint`` comes from a base class in another module is not flagged
  (the app-level entrypoint count already gates most of that noise).
"""

from __future__ import annotations

import ast
from pathlib import Path

from conformance.suite.checks._ast_common import _parse_directives
from conformance.suite.checks._ast_common import is_test_class as _is_test_class
from conformance.suite.checks._ast_common import make_cli_main, make_finding
from conformance.suite.schema.findings import Finding

SERIES = "T"
RULE_T020 = "T020"

#: Decorator that declares a product workflow on an App subclass.
_ENTRYPOINT_DECORATOR = "entrypoint"

#: Keywords on a ``Scenario(...)`` call that make the target unambiguous.
_DECLARING_KEYWORDS = frozenset({"entrypoint", "endpoint"})

__all__ = [
    "SERIES",
    "app_entrypoint_count",
    "discover",
    "main",
    "scan_all",
    "scan_path",
    "scan_text",
]


# ---------------------------------------------------------------------------
# App-side: how many entrypoints does this app declare?
# ---------------------------------------------------------------------------


def _has_entrypoint_decorator(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    for dec in node.decorator_list:
        target = dec.func if isinstance(dec, ast.Call) else dec
        name = (
            target.attr
            if isinstance(target, ast.Attribute)
            else getattr(target, "id", None)
        )
        if name == _ENTRYPOINT_DECORATOR:
            return True
    return False


def app_entrypoint_count(root: Path) -> int:
    """Number of distinct ``@entrypoint`` methods the app declares.

    Returns ``0`` when there is no ``app/`` package, which makes the rule
    no-op for non-app repos.
    """
    app_dir = root / "app"
    if not app_dir.is_dir():
        return 0

    names: set[str] = set()
    for path in app_dir.rglob("*.py"):
        if any(p in {"__pycache__", ".venv", "node_modules"} for p in path.parts):
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError, OSError):
            continue
        for node in ast.walk(tree):
            if isinstance(
                node, (ast.FunctionDef, ast.AsyncFunctionDef)
            ) and _has_entrypoint_decorator(node):
                names.add(node.name)
    return len(names)


# ---------------------------------------------------------------------------
# Test-side: does this workflow scenario declare its target?
# ---------------------------------------------------------------------------


def _is_scenario_call(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
    return name == "Scenario"


def _keyword_str(call: ast.Call, name: str) -> str | None:
    for kw in call.keywords:
        if kw.arg == name and isinstance(kw.value, ast.Constant):
            value = kw.value.value
            return value if isinstance(value, str) else None
    return None


def _declares_entrypoint(call: ast.Call) -> bool:
    return any(
        kw.arg in _DECLARING_KEYWORDS and kw.value is not None for kw in call.keywords
    )


def _classes_declaring_entrypoint(tree: ast.AST) -> set[int]:
    """Line ranges of ``Test*`` classes that set a class-level ``entrypoint``."""
    covered: set[int] = set()
    for node in ast.walk(tree):
        if not _is_test_class(node):
            continue
        declares = any(
            isinstance(stmt, (ast.Assign, ast.AnnAssign))
            and any(
                getattr(t, "id", None) == "entrypoint"
                for t in (
                    stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]
                )
            )
            for stmt in node.body
        )
        if declares:
            end = getattr(node, "end_lineno", node.lineno) or node.lineno
            covered.update(range(node.lineno, end + 1))
    return covered


def scan_text(source: str, filename: str, *, entrypoint_count: int) -> list[Finding]:
    """Flag workflow scenarios with no declared entrypoint.

    ``entrypoint_count`` is the app's declared ``@entrypoint`` total; the rule
    is a no-op below two.
    """
    if entrypoint_count < 2:
        return []
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []

    directives = _parse_directives(source)
    covered_by_class = _classes_declaring_entrypoint(tree)
    findings: list[Finding] = []

    for node in ast.walk(tree):
        if not _is_scenario_call(node):
            continue
        if _keyword_str(node, "api") != "workflow":
            continue
        if _declares_entrypoint(node) or node.lineno in covered_by_class:
            continue

        name = _keyword_str(node, "name") or "<unnamed>"
        findings.append(
            make_finding(
                filename=filename,
                rule_id=RULE_T020,
                node=node,
                message=(
                    f"workflow scenario {name!r} does not declare which app "
                    f"entrypoint it exercises, and this app declares "
                    f"{entrypoint_count}. It will start the app's default "
                    f"entrypoint. Set entrypoint=... on the Scenario, or a "
                    f"class-level entrypoint on the suite."
                ),
                directives=directives,
            )
        )

    return findings


def scan_path(path: Path, root: Path) -> list[Finding]:
    """Scan one test file, resolving the app's entrypoint count from ``root``."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    try:
        rel = path.relative_to(root)
    except ValueError:
        rel = path
    return scan_text(text, str(rel), entrypoint_count=app_entrypoint_count(root))


def discover(root: Path) -> list[Path]:
    """Discover pytest test files under ``tests/``.

    Scenario suites are not confined to ``tests/integration/`` — several
    connectors keep them under ``tests/e2e/`` or ``tests/sdr/`` — so this walks
    the whole tree rather than one tier.
    """
    base = root / "tests"
    if not base.is_dir():
        return []
    paths: list[Path] = []
    for path in base.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        name = path.name
        if name.startswith("test_") or name.endswith("_test.py"):
            paths.append(path)
    return sorted(paths)


def scan_all(paths: list[Path], root: Path) -> list[Finding]:
    """Scan every discovered test file, resolving the entrypoint count once.

    The app-side count is repo-global, so computing it per file would re-walk
    ``app/`` for every test module.
    """
    count = app_entrypoint_count(root)
    if count < 2:
        return []

    findings: list[Finding] = []
    for path in paths:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        try:
            rel = path.relative_to(root)
        except ValueError:
            rel = path
        findings.extend(scan_text(text, str(rel), entrypoint_count=count))
    return findings


main = make_cli_main(
    scan_all=scan_all,
    discover=discover,
    description=__doc__,
)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
