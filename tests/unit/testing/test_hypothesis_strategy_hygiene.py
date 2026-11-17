"""Ratchets for the shipped Hypothesis strategies and the tests that use them (FND-4292).

Three rules, each read statically from the source:

* every strategy module has an importer under ``tests/`` or is deprecated
  (declares ``_DEPRECATED_CONSTANTS``), so no orphan ships unnoticed again;
* every ``st.integers()`` in a strategy module is bounded on both sides, so a
  strategy cannot draw a negative size or a value no driver holds;
* no test module loads a Hypothesis profile at module level. A module-level
  ``settings.load_profile`` replaces the one ``tests/unit/conftest.py`` loads
  for every test the xdist worker runs after it, so a test's settings would
  depend on which files shared its worker. Per-test ``@settings`` instead.
"""

from __future__ import annotations

import ast
import functools
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[3]
_STRATEGIES = _REPO / "application_sdk" / "testing" / "hypothesis" / "strategies"
_TESTS = _REPO / "tests"

#: Unbounded integers FND-4286 bounds, together with un-skipping the JSON
#: writer tests they break. Remove the entry when that lands.
_UNBOUNDED_INTEGERS_PENDING = {"outputs/json_output.py"}


def _strategy_modules() -> list[Path]:
    return sorted(p for p in _STRATEGIES.rglob("*.py") if p.stat().st_size > 0)


def _dotted(path: Path) -> str:
    parts = path.relative_to(_REPO).with_suffix("").parts
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _rel(path: Path) -> str:
    return path.relative_to(_STRATEGIES).as_posix()


def _imported_modules(tree: ast.Module) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
            found.update(f"{node.module}.{alias.name}" for alias in node.names)
        elif isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
    return found


@functools.cache
def _test_trees() -> tuple[tuple[Path, ast.Module], ...]:
    return tuple(
        (p, ast.parse(p.read_text(encoding="utf-8"), str(p)))
        for p in sorted(_TESTS.rglob("*.py"))
    )


def _is_deprecated(tree: ast.Module) -> bool:
    return any(
        isinstance(node, ast.AnnAssign | ast.Assign)
        and any(
            isinstance(t, ast.Name) and t.id == "_DEPRECATED_CONSTANTS"
            for t in (node.targets if isinstance(node, ast.Assign) else [node.target])
        )
        for node in tree.body
    )


def test_the_scan_sees_the_strategy_modules() -> None:
    # Guards the guard: a wrong root would make every test below pass on nothing.
    assert len(_strategy_modules()) >= 9


@pytest.mark.parametrize("module", _strategy_modules(), ids=_rel)
def test_every_strategy_module_has_an_importer_or_a_deprecation(module: Path) -> None:
    if _is_deprecated(ast.parse(module.read_text(encoding="utf-8"))):
        return
    dotted = _dotted(module)
    # The deprecation tests import every deprecated module; they are not users.
    importers = [
        path
        for path, tree in _test_trees()
        if path.name != "test_zero_caller_deprecations.py"
        and dotted in _imported_modules(tree)
    ]
    assert importers, (
        f"{dotted} has no importer under tests/ and is not deprecated; test it "
        "or deprecate it per docs/standards/symbols.md"
    )


def _unbounded_integers(tree: ast.Module) -> list[int]:
    lines = []
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "integers"
        ):
            continue
        given = {kw.arg for kw in node.keywords} | {
            ("min_value", "max_value")[i] for i in range(len(node.args[:2]))
        }
        if not {"min_value", "max_value"} <= given:
            lines.append(node.lineno)
    return lines


@pytest.mark.parametrize("module", _strategy_modules(), ids=_rel)
def test_every_integer_strategy_is_bounded(module: Path) -> None:
    if _rel(module) in _UNBOUNDED_INTEGERS_PENDING:
        pytest.skip("bounded by FND-4286")
    lines = _unbounded_integers(ast.parse(module.read_text(encoding="utf-8")))
    assert not lines, (
        f"{_rel(module)}: st.integers() without both min_value and max_value "
        f"at line(s) {lines}"
    )


def test_the_integer_scan_flags_an_unbounded_call() -> None:
    tree = ast.parse(
        "st.integers()\n"
        "st.integers(min_value=0)\n"
        "st.integers(0, 10)\n"
        "st.integers(min_value=0, max_value=10)\n"
    )
    assert _unbounded_integers(tree) == [1, 2]


def _module_level_profile_loads(tree: ast.Module) -> list[int]:
    lines = []
    for node in tree.body:
        for call in ast.walk(node):
            if (
                isinstance(call, ast.Call)
                and isinstance(call.func, ast.Attribute)
                and call.func.attr == "load_profile"
            ):
                # Anything inside a def or class body runs per call, not at import.
                if not isinstance(
                    node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef
                ):
                    lines.append(call.lineno)
    return lines


def test_no_test_module_loads_a_hypothesis_profile_at_module_level() -> None:
    offenders = {
        path.relative_to(_REPO).as_posix(): lines
        for path, tree in _test_trees()
        if path.name != "conftest.py" and (lines := _module_level_profile_loads(tree))
    }
    assert not offenders, (
        "module-level settings.load_profile replaces the unit profile for the "
        f"whole xdist worker; use a per-test @settings decorator: {offenders}"
    )


def test_the_profile_scan_flags_a_module_level_load() -> None:
    tree = ast.parse(
        "settings.load_profile('x')\ndef f():\n    settings.load_profile('y')\n"
    )
    assert _module_level_profile_loads(tree) == [1]
