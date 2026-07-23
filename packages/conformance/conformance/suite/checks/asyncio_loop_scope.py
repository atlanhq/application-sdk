"""T019 AsyncioTestLoopScopeUnset — flag a broadened pytest-asyncio *fixture*
loop scope with no matching *test* loop scope, **when a test actually drives
fixture-owned workflow execution from its own body**.

``pytest-asyncio`` has two independent knobs in
``[tool.pytest.ini_options]``:

* ``asyncio_default_fixture_loop_scope`` — the event loop async **fixtures**
  default to.
* ``asyncio_default_test_loop_scope`` — the event loop async **tests** default
  to. **When unset it defaults to ``"function"``** (a fresh loop per test).

Setting the fixture scope to ``session`` / ``package`` / ``module`` / ``class``
without also setting the test scope leaves the two on *different* loops: async
fixtures share one long-lived loop, but each test runs on its own
function-scoped loop. A session-scoped fixture that owns a live resource bound
to *its* loop — a Temporal worker/client — is then invisible to a test that
drives that resource **from its own body**: the test awaits work the fixture's
loop must service, but that loop is not being driven while the test's loop runs,
so nothing progresses and the test hangs until the suite timeout fires. It
surfaced on a canonical connector — a single ``REUSE`` integration test that
submitted a Temporal workflow from the test body hung for the full
``pytest-timeout`` while every sibling test (which read a class-fixture result)
passed.

Correlated, not config-only
----------------------------
The config mismatch alone is *not* a defect: a suite that runs all workflow
execution inside fixtures (session/class-scoped async fixtures) and only asserts
on the result in test bodies is on the safe path even with the test scope left
implicit. So — exactly like **T018**, which fires only when an ``addopts``
deselect actually removes tests that exist — this rule fires only when the risky
config **and** real evidence coincide: at least one collectable test *function*
(not a fixture) whose body drives workflow execution via an awaited
``execute_app`` / ``execute_workflow`` / ``start_workflow`` call. A repo with the
mismatched config but all execution behind fixtures (the metabase/mysql shape)
is left silent.

Remediation is a one-line config edit: set ``asyncio_default_test_loop_scope``
explicitly — usually to the same scope as the fixtures — so tests and their
fixtures share a loop. Moving the offending call into a same-scope fixture (the
test body only asserts on the result) also removes the hang and is the
prevailing pattern, but leaves the config trap for the next author; the config
fix is the durable one.

Inline suppression
------------------
Add ``# conformance: ignore[T019] <reason>`` on the
``asyncio_default_fixture_loop_scope`` line (or the line directly above it) when
the in-body driver is deliberately loop-safe and state that reason.

Known coverage limits (intentional — biased toward zero false positives at WARN
tier):

* **pyproject-only config.** Reads ``[tool.pytest.ini_options]`` from the
  repo-root ``pyproject.toml`` (the fleet convention). A ``pytest.ini`` /
  ``setup.cfg`` / ``tox.ini`` ``[pytest]`` section is not scanned.
* **Driver detection is syntactic and name-based.** Only awaited calls whose
  terminal name is ``execute_app`` / ``execute_workflow`` / ``start_workflow``
  (the SDK's workflow-submission surface) count as an in-body driver. A test
  that reaches the same infra through a differently-named helper is not matched
  (a documented false negative, consistent with T001/T018's syntactic limits).
  Collection mirrors pytest defaults (``test*`` functions, ``Test*`` classes,
  ``test_*.py`` / ``*_test.py`` files under ``tests/``).
"""

from __future__ import annotations

import ast
import re
import sys
import tomllib
from collections.abc import Iterable
from pathlib import Path

from conformance.suite.checks._ast_common import (
    is_test_class,
    is_test_function,
    make_cli_main,
    make_toml_finding,
    parse_toml_suppressions,
)
from conformance.suite.schema.findings import Finding

SERIES = "T"
RULE_T019 = "T019"

# The two pytest-asyncio ini keys this rule reasons about.
_FIXTURE_SCOPE_KEY = "asyncio_default_fixture_loop_scope"
_TEST_SCOPE_KEY = "asyncio_default_test_loop_scope"

# Valid pytest-asyncio loop scopes (see pytest_asyncio.plugin._ScopeName). Any
# value other than "function" is "broadened" — fixtures then outlive a single
# test's loop, which is only safe if tests share that loop too.
_VALID_SCOPES = frozenset({"session", "package", "module", "class", "function"})

# SDK workflow-submission surface: an awaited call to one of these from a *test
# body* is what turns the config mismatch into a real hang (the resource lives
# on the fixture loop; the await runs on the test's function loop). Kept as a
# named set so the recognised entry points are explicit and extensible.
_WORKFLOW_DRIVER_NAMES = frozenset(
    {"execute_app", "execute_workflow", "start_workflow"}
)

# ``asyncio_default_fixture_loop_scope = ...`` assignment line (for anchoring the
# finding + its suppression directive).
_FIXTURE_SCOPE_LINE_RE = re.compile(rf"^\s*{re.escape(_FIXTURE_SCOPE_KEY)}\s*=")

# Cap how many driver test labels are listed in the message.
_MAX_LISTED = 5

__all__ = [
    "SERIES",
    "discover",
    "in_body_workflow_drivers",
    "main",
    "scan_all",
    "scan_path",
    "scan_text",
]


# ---------------------------------------------------------------------------
# Pure core (filesystem-free — unit-testable)
# ---------------------------------------------------------------------------


def _ini_options(text: str) -> dict[str, object] | None:
    """Return ``[tool.pytest.ini_options]`` from *text*, or ``None``."""
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    tool = data.get("tool")
    pytest_tbl = tool.get("pytest") if isinstance(tool, dict) else None
    ini = pytest_tbl.get("ini_options") if isinstance(pytest_tbl, dict) else None
    return ini if isinstance(ini, dict) else None


def _broadened_fixture_scope(text: str) -> str | None:
    """Return the broadened fixture loop scope iff the config is risky.

    "Risky" = ``asyncio_default_fixture_loop_scope`` set to a valid
    non-``function`` scope while ``asyncio_default_test_loop_scope`` is absent.
    Returns the fixture-scope string in that case, else ``None`` (an explicit
    test scope, a ``function`` fixture scope, an invalid value pytest-asyncio
    would itself reject, or no pytest table at all).
    """
    ini = _ini_options(text)
    if ini is None:
        return None
    fixture_scope = ini.get(_FIXTURE_SCOPE_KEY)
    if not isinstance(fixture_scope, str) or fixture_scope not in _VALID_SCOPES:
        return None
    if fixture_scope == "function":
        return None
    if _TEST_SCOPE_KEY in ini:
        return None
    return fixture_scope


def _call_name(call: ast.Call) -> str | None:
    """Terminal name of a call target: ``f()`` -> 'f', ``a.b.f()`` -> 'f'."""
    func = call.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _drives_workflow_in_body(func: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """True if *func*'s body awaits a workflow-submission call.

    Matches ``await <...>execute_app(...)`` / ``execute_workflow`` /
    ``start_workflow`` anywhere in the function subtree — the in-body drive that
    turns the loop-scope mismatch into a hang.
    """
    for node in ast.walk(func):
        if not isinstance(node, ast.Await):
            continue
        value = node.value
        if isinstance(value, ast.Call) and _call_name(value) in _WORKFLOW_DRIVER_NAMES:
            return True
    return False


def _drivers_in_module(tree: ast.Module, rel: str) -> list[str]:
    """Labels of test functions in *tree* that drive a workflow in-body."""
    labels: list[str] = []
    for node in tree.body:
        if is_test_function(node):
            if _drives_workflow_in_body(node):
                labels.append(f"{rel}::{node.name}")
        elif is_test_class(node):
            for sub in node.body:
                if is_test_function(sub) and _drives_workflow_in_body(sub):
                    labels.append(f"{rel}::{node.name}::{sub.name}")
    return labels


def _message(fixture_scope: str, drivers: list[str]) -> str:
    listed = ", ".join(drivers[:_MAX_LISTED])
    if len(drivers) > _MAX_LISTED:
        listed += f", … (+{len(drivers) - _MAX_LISTED} more)"
    return (
        f"pyproject sets {_FIXTURE_SCOPE_KEY}='{fixture_scope}' but leaves "
        f"{_TEST_SCOPE_KEY} unset (it defaults to 'function'), and "
        f"{len(drivers)} test(s) drive workflow execution from the test body "
        f"(await execute_app/execute_workflow/start_workflow): {listed}. Those "
        f"tests run on a function-scoped loop while the fixture-owned worker/"
        f"client lives on the '{fixture_scope}'-scoped loop, so the awaited work "
        "is never polled and the test hangs until the suite timeout fires. Set "
        f"{_TEST_SCOPE_KEY} explicitly (usually '{fixture_scope}', to match the "
        "fixtures), or move the workflow call into a same-scope fixture and "
        "assert on its result. See T019."
    )


def scan_text(
    text: str,
    file: str,
    *,
    in_body_drivers: Iterable[str] | None = None,
) -> list[Finding]:
    """Scan a ``pyproject.toml`` *text* for T019.

    Fires only when the config is risky (:func:`_broadened_fixture_scope`) **and**
    *in_body_drivers* is non-empty — the labels of tests that drive workflow
    execution in-body (supplied from the filesystem by :func:`scan_path`). A
    text-only caller that passes nothing gets no findings: the mismatch is only a
    hazard relative to a test that actually drives fixture-owned work in-body
    (mirrors T018's test-context requirement).
    """
    fixture_scope = _broadened_fixture_scope(text)
    if fixture_scope is None:
        return []
    drivers = list(in_body_drivers) if in_body_drivers is not None else []
    if not drivers:
        return []
    return [
        make_toml_finding(
            rule_id=RULE_T019,
            file=file,
            line=_fixture_scope_line(text),
            column=1,
            message=_message(fixture_scope, drivers),
            suppressions=parse_toml_suppressions(text),
        )
    ]


def _fixture_scope_line(text: str) -> int:
    for i, line in enumerate(text.splitlines(), start=1):
        if _FIXTURE_SCOPE_LINE_RE.match(line):
            return i
    return 1


# ---------------------------------------------------------------------------
# Filesystem scan API
# ---------------------------------------------------------------------------


def in_body_workflow_drivers(root: Path) -> list[str]:
    """Collect ``tests/`` test functions that drive a workflow in-body.

    Walks pytest's default test files (``test_*.py`` / ``*_test.py``) under
    ``root/tests`` and returns ``file::test`` / ``file::Class::test`` labels for
    those whose body awaits ``execute_app`` / ``execute_workflow`` /
    ``start_workflow``. Fixtures never match (they are not ``test*`` functions),
    so the safe "execute inside a fixture" pattern is correctly ignored.
    """
    tests_dir = root / "tests"
    if not tests_dir.is_dir():
        return []
    labels: list[str] = []
    for path in sorted(tests_dir.rglob("*.py")):
        if not (path.name.startswith("test_") or path.name.endswith("_test.py")):
            continue
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
        labels.extend(_drivers_in_module(tree, str(rel)))
    return labels


def scan_path(path: Path, root: Path) -> list[Finding]:
    """Scan the repo-root ``pyproject.toml`` for T019.

    Resolves the in-body workflow drivers from *root* so the finding reflects the
    real interaction between the app's loop-scope config and its test suite.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    try:
        rel = path.relative_to(root)
    except ValueError:
        rel = path
    return scan_text(text, str(rel), in_body_drivers=in_body_workflow_drivers(root))


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
        "T019: flag a broadened pytest-asyncio fixture loop scope "
        "(asyncio_default_fixture_loop_scope) with no matching "
        "asyncio_default_test_loop_scope when a test drives workflow execution "
        "from the test body — which hangs on the fixture-owned worker/client."
    ),
    discover=discover,
    default_scan_paths=("pyproject.toml",),
)
"""CLI entry point for the T019 asyncio-loop-scope check."""


if __name__ == "__main__":
    sys.exit(main())
