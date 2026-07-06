"""T005–T009 — assertion meaningfulness and silent non-execution (BLDX-1400).

These checks close the gap between "the coverage tool is green" and "the
tests actually verify something." Two sub-families, both scanning every
``.py`` file under ``tests/``:

**Assertion meaningfulness** — a test can execute without ever checking an
outcome:

* ``T005`` AssertionFreeTest — a collected test's body is non-empty but
  contains no recognised assertion form.
* ``T006`` EmptyTestBody — a collected test's body is only a stub
  (``pass``/``...``/docstring). Checked *before* T005/T007 — a stub is
  reported as T006, never also as T005.
* ``T007`` VacuousAssertion — every assertion in a collected test is a
  constant-true expression (``assert True``, ``assert 1``) that can never
  fail.

**Silent non-execution** — a test can look present while never actually
running in CI:

* ``T008`` UncollectableTestFile — a file under ``tests/`` defines
  ``test*``/``Test*`` collectables but its filename doesn't match pytest's
  default collection glob (``test_*.py``/``*_test.py``), so it is never
  collected. When this fires, T005–T007/T009 are not evaluated for the same
  file — until the filename is fixed, nothing else about the file's content
  matters to CI.
* ``T009`` UnconditionalModuleSkip — a module-level
  ``pytest.skip(..., allow_module_level=True)`` that is not nested inside an
  ``if``/``try`` guard, unconditionally disabling every test in the file. The
  legitimate env-guarded e2e pattern (``if not os.environ.get(...):
  pytest.skip(..., allow_module_level=True)``) is *not* flagged — only a bare
  module-level call is.

Assertion vocabulary (T005/T007), intentionally broad — biased toward zero
false positives at WARN tier, matching T001's documented-limits approach:
a bare ``assert``; ``with pytest.raises/warns/deprecated_call(...)``; a call
whose attribute name starts with ``assert`` (``self.assertEqual``,
``mock.assert_called_once``, ``pandas.testing.assert_frame_equal``, a
project-local ``_assert_*`` helper) or is exactly ``fail``
(``pytest.fail``/``self.fail``); or one of the SDK integration-test scenario
helpers (``.equals``/``.contains``/``.exists``/``.is_dict``/``.is_string``/
``.is_true``/``.is_list``).

Discovery
---------
Walks the entire ``tests/`` tree (like T002/T003's ``sdr_test_checks``, not
just ``tests/integration/`` like T001) — an assertion-quality or
uncollectable-file defect can occur under any tier directory.

Inline suppression
-------------------
Add ``# conformance: ignore[T00N] <reason>`` on the offending line (the
``def`` line for T005–T007, the file's first line for T008, the ``skip(...)``
call's line for T009) or the comment-only line directly above it.

Known coverage limits (intentional — see module docstring of
``_ast_common._pytest_collection`` for the shared collection-mirror limits):

* Assertion resolution is syntactic, like T001's marker resolution — a
  dynamically-constructed assertion helper (built via ``getattr`` or stored in
  a variable before being called) is not recognised, which only produces a
  false negative (a real test is not flagged), never a false positive.
* T008/T009 only inspect module-level and class-level shapes; a test defined
  inside a helper function that itself constructs and returns a test function
  is not detected.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

from conformance.suite.checks._ast_common import (
    _parse_directives,
    is_collectable_test_file,
    is_test_class,
    is_test_function,
    make_cli_main,
    make_finding,
)
from conformance.suite.schema.findings import Finding

SERIES = "T"
RULE_T005 = "T005"
RULE_T006 = "T006"
RULE_T007 = "T007"
RULE_T008 = "T008"
RULE_T009 = "T009"

# Call attribute names recognised as an assertion beyond a bare `assert`.
_ASSERT_CONTEXT_MANAGERS: frozenset[str] = frozenset(
    {"raises", "warns", "deprecated_call"}
)
_SCENARIO_HELPER_ATTRS: frozenset[str] = frozenset(
    {"equals", "contains", "exists", "is_dict", "is_string", "is_true", "is_list"}
)

__all__ = ["SERIES", "discover", "main", "scan_path", "scan_text"]


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def discover(root: Path) -> list[Path]:
    """Walk the entire ``tests/`` tree for Python source files.

    Broader than T001's ``tests/integration/``-only walk (mirrors T002/T003's
    ``sdr_test_checks.discover``) because assertion-quality and
    uncollectable-file defects can occur under any tier directory. Returns
    ``[]`` when ``tests/`` is absent.
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


def _collect_tests(
    tree: ast.Module,
) -> list[tuple[ast.FunctionDef | ast.AsyncFunctionDef, str]]:
    """Return ``(node, qualified_name)`` for every collected test in *tree*.

    Mirrors pytest's default collection: module-level ``test*`` functions,
    and ``test*`` methods of module-level ``Test*`` classes (one level deep —
    the same shape T001 mirrors).
    """
    collected: list[tuple[ast.FunctionDef | ast.AsyncFunctionDef, str]] = []
    for node in tree.body:
        if is_test_function(node):
            collected.append((node, node.name))
        elif is_test_class(node):
            for sub in node.body:
                if is_test_function(sub):
                    collected.append((sub, f"{node.name}.{sub.name}"))
    return collected


# ---------------------------------------------------------------------------
# T006 — stub body
# ---------------------------------------------------------------------------


def _is_stub_body(body: list[ast.stmt]) -> bool:
    """True when *body* is only a docstring plus ``pass``/``...`` statements."""
    stmts = list(body)
    if (
        stmts
        and isinstance(stmts[0], ast.Expr)
        and isinstance(stmts[0].value, ast.Constant)
        and isinstance(stmts[0].value.value, str)
    ):
        stmts = stmts[1:]
    if not stmts:
        return True
    for stmt in stmts:
        if isinstance(stmt, ast.Pass):
            continue
        if (
            isinstance(stmt, ast.Expr)
            and isinstance(stmt.value, ast.Constant)
            and stmt.value.value is Ellipsis
        ):
            continue
        return False
    return True


# ---------------------------------------------------------------------------
# T005 / T007 — assertion presence and vacuousness
# ---------------------------------------------------------------------------


def _is_vacuous_constant(expr: ast.expr) -> bool:
    """True when *expr* is a literal constant that is always truthy."""
    return isinstance(expr, ast.Constant) and bool(expr.value)


def _call_attr_name(call: ast.Call) -> str | None:
    func = call.func
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return None


def _call_is_assertion(call: ast.Call) -> bool:
    """True when *call* is a recognised assertion-vocabulary call."""
    name = _call_attr_name(call)
    if name is None:
        return False
    if name.lower().startswith("assert"):
        return True
    if name == "fail":
        return True
    return name in _SCENARIO_HELPER_ATTRS


def _withitem_is_assertion_cm(item: ast.withitem) -> bool:
    ctx = item.context_expr
    if not isinstance(ctx, ast.Call):
        return False
    name = _call_attr_name(ctx)
    return name in _ASSERT_CONTEXT_MANAGERS


def _assertion_signals(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
) -> tuple[bool, bool]:
    """Return ``(has_any_assertion, only_vacuous_plain_asserts)`` for *node*.

    ``only_vacuous_plain_asserts`` is meaningful only when ``has_any_assertion``
    is ``True``: it is set when every ``assert`` statement found is a constant-
    true literal and no other assertion form (context-manager, ``assert_*``
    call, scenario helper) is present.
    """
    has_assert_stmt = False
    has_nonvacuous_assert_stmt = False
    has_other_assertion = False
    for sub in ast.walk(node):
        if isinstance(sub, ast.Assert):
            has_assert_stmt = True
            if not _is_vacuous_constant(sub.test):
                has_nonvacuous_assert_stmt = True
        elif isinstance(sub, ast.With):
            if any(_withitem_is_assertion_cm(item) for item in sub.items):
                has_other_assertion = True
        elif isinstance(sub, ast.Call):
            if _call_is_assertion(sub):
                has_other_assertion = True
    has_any = has_assert_stmt or has_other_assertion
    only_vacuous = (
        has_assert_stmt and not has_nonvacuous_assert_stmt and not has_other_assertion
    )
    return has_any, only_vacuous


# ---------------------------------------------------------------------------
# T009 — unconditional module-level skip
# ---------------------------------------------------------------------------


def _is_unconditional_skip_stmt(stmt: ast.stmt) -> bool:
    """True when *stmt* is a direct ``pytest.skip(..., allow_module_level=True)`` call.

    Only a statement that appears *directly* in the module body qualifies —
    the same call nested inside an ``if``/``try`` (the legitimate env-guarded
    pattern) is reached through that guard's own body, not the module body,
    so it never reaches this function.
    """
    if not isinstance(stmt, ast.Expr) or not isinstance(stmt.value, ast.Call):
        return False
    call = stmt.value
    if _call_attr_name(call) != "skip":
        return False
    return any(
        kw.arg == "allow_module_level"
        and isinstance(kw.value, ast.Constant)
        and kw.value.value is True
        for kw in call.keywords
    )


# ---------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------

_T005_HINT = (
    "Add an assertion on the outcome you care about (e.g. `assert result.count == "
    "3`), a `pytest.raises(...)` block, or a scenario-helper call — see T005 in "
    "docs/rules/tests.md for the full recognised vocabulary."
)
_T006_HINT = (
    "Implement the test, remove it, or mark it `@pytest.mark.skip(reason=...)`."
)
_T007_HINT = "Assert on a value that actually depends on the code under test, not a literal constant."
_T009_HINT = (
    "Guard the skip behind a real precondition (e.g. `if not os.environ.get(...)`) "
    "or mark individual tests `@pytest.mark.skip(reason=...)` instead of an "
    "unconditional module-level skip."
)


def _t005_message(qualname: str) -> str:
    return f"Test '{qualname}' has a non-empty body but no recognised assertion. {_T005_HINT}"


def _t006_message(qualname: str) -> str:
    return f"Test '{qualname}' body is a stub (pass/.../docstring only). {_T006_HINT}"


def _t007_message(qualname: str) -> str:
    return (
        f"Test '{qualname}' only asserts constant-true expressions that can never "
        f"fail. {_T007_HINT}"
    )


def _t008_message(filename: str) -> str:
    return (
        f"'{filename}' defines test*/Test* collectables but its filename does not "
        "match pytest's default collection glob (test_*.py / *_test.py) — it is "
        "never collected, so every test in it silently never runs. Rename the file "
        "to match the convention."
    )


def _t009_message() -> str:
    return (
        "Module-level pytest.skip(..., allow_module_level=True) is not guarded by "
        f"an if/try — it unconditionally disables every test in this file. {_T009_HINT}"
    )


# ---------------------------------------------------------------------------
# Scan API
# ---------------------------------------------------------------------------


def scan_text(text: str, file: str) -> list[Finding]:
    """Scan a single test file *text* and return all T005–T009 findings."""
    try:
        tree = ast.parse(text, filename=file)
    except SyntaxError:
        return []
    directives = _parse_directives(text)
    collected = _collect_tests(tree)

    if not is_collectable_test_file(Path(file).name):
        if not collected:
            return []
        anchor = collected[0][0]
        return [
            make_finding(
                filename=file,
                rule_id=RULE_T008,
                node=anchor,
                message=_t008_message(file),
                directives=directives,
            )
        ]

    findings: list[Finding] = []
    for node, qualname in collected:
        if _is_stub_body(node.body):
            findings.append(
                make_finding(
                    filename=file,
                    rule_id=RULE_T006,
                    node=node,
                    message=_t006_message(qualname),
                    directives=directives,
                )
            )
            continue
        has_any, only_vacuous = _assertion_signals(node)
        if not has_any:
            findings.append(
                make_finding(
                    filename=file,
                    rule_id=RULE_T005,
                    node=node,
                    message=_t005_message(qualname),
                    directives=directives,
                )
            )
        elif only_vacuous:
            findings.append(
                make_finding(
                    filename=file,
                    rule_id=RULE_T007,
                    node=node,
                    message=_t007_message(qualname),
                    directives=directives,
                )
            )

    for stmt in tree.body:
        if _is_unconditional_skip_stmt(stmt):
            findings.append(
                make_finding(
                    filename=file,
                    rule_id=RULE_T009,
                    node=stmt,
                    message=_t009_message(),
                    directives=directives,
                )
            )

    return findings


def scan_path(path: Path, root: Path) -> list[Finding]:
    """Scan a single test file, producing repo-root-relative URIs."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    try:
        rel = path.relative_to(root)
    except ValueError:
        rel = path
    return scan_text(text, str(rel))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

main = make_cli_main(
    scan_text,
    description=(
        "T005-T009: scan tests/ for assertion-free/stub/vacuous tests, "
        "uncollectable test files, and unconditional module-level skips."
    ),
    discover=discover,
    default_scan_paths=("tests",),
)
"""CLI entry point for the T005–T009 test-quality checks."""


if __name__ == "__main__":
    sys.exit(main())
