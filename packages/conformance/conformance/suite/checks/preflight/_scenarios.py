"""F016: every required preflight scenario is registered as a collectable test.

Conformance measures that the scenarios are *defined*; whether they *pass* is
the test gate's measure, and nothing here executes them. A scenario counts
when a pytest-collected test under ``tests/`` carries
``pytest.mark.preflight_conformance(rule="F016", scenario=..., entrypoint=...)``,
is not skipped, and calls the contract assertion the scenario needs.

The registration has to be statically resolvable. Two shapes are read:

* a marker decorating the test function (``@pytest.mark.preflight_conformance(...)``);
* a marker on a ``pytest.param`` inside ``pytest.mark.parametrize``, which is
  how one test registers a scenario once per entrypoint. The parametrize may
  come from a module-level helper whose body is a single ``return`` (the
  ``entrypoint_matrix("healthy")`` shape in atlan-metabase-app), and its
  parameter list may be a comprehension over a module-level tuple of names.

A marker that names ``preflight_conformance`` but does not resolve is reported,
never silently dropped: an unreadable registration is not evidence either way.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from conformance.preflight_scenarios import SCENARIOS
from conformance.suite.checks._ast_common import make_finding
from conformance.suite.checks._ast_common._pytest_collection import (
    is_collectable_test_file,
    is_test_class,
    is_test_function,
)
from conformance.suite.schema.findings import Finding

from ._common import Registry, Source, entrypoint_contracts

RULE = "F016"
MARKER = "preflight_conformance"

#: The contract assertion every registered scenario must make, plus the extra
#: one the lifetime scenarios need. Mirrors what the scenario matrix asks each
#: scenario to prove: a typed verdict, and for these three, that the probe
#: stayed inside its budget and left nothing running.
_RESULT_ASSERTION = "assert_preflight_result"
_LIFETIME_ASSERTION = "assert_probe_lifetime"
_LIFETIME_SCENARIOS = frozenset({"hung_probe", "cancellation_cleanup", "budget_retry"})

_SKIP_MARKS = frozenset({"skip", "skipif", "xfail"})
_RUNTIME_SKIPS = frozenset({"skip", "xfail"})

#: Helper indirection is followed this deep; a registration nested further is
#: reported as unresolved rather than chased.
_MAX_DEPTH = 3


class _Unresolved(Exception):
    """A value the static reader cannot determine."""


@dataclass(frozen=True)
class _Mark:
    name: str
    kwargs: Mapping[str, object] = field(default_factory=dict)
    #: For ``parametrize``: the marks carried by each ``pytest.param``.
    params: tuple[tuple[_Mark, ...], ...] = ()


@dataclass(frozen=True)
class _Registration:
    scenario: str
    entrypoint: str
    unsupported: bool
    skipped: bool
    node: ast.AST


@dataclass
class _Module:
    src: Source
    constants: dict[str, object]
    functions: dict[str, ast.FunctionDef | ast.AsyncFunctionDef]
    pytest_names: frozenset[str]
    mark_names: frozenset[str]
    param_names: frozenset[str]


def _pytest_bindings(
    tree: ast.Module,
) -> tuple[frozenset[str], frozenset[str], frozenset[str]]:
    """Local names bound to ``pytest``, ``pytest.mark`` and ``pytest.param``."""
    modules, marks, params = {"pytest"}, set(), set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "pytest":
                    modules.add(alias.asname or "pytest")
        elif isinstance(node, ast.ImportFrom) and node.module == "pytest":
            for alias in node.names:
                if alias.name == "mark":
                    marks.add(alias.asname or "mark")
                elif alias.name == "param":
                    params.add(alias.asname or "param")
    return frozenset(modules), frozenset(marks), frozenset(params)


def _literal(node: ast.expr, env: Mapping[str, object]) -> object:
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        if node.id in env:
            return env[node.id]
        raise _Unresolved(node.id)
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        return tuple(_literal(elt, env) for elt in node.elts)
    raise _Unresolved(ast.unparse(node))


def _module_constants(tree: ast.Module) -> dict[str, object]:
    constants: dict[str, object] = {}
    for node in tree.body:
        target: ast.expr | None = None
        value: ast.expr | None = None
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target, value = node.targets[0], node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            target, value = node.target, node.value
        if isinstance(target, ast.Name) and value is not None:
            try:
                constants[target.id] = _literal(value, constants)
            except _Unresolved:
                constants.pop(target.id, None)
    return constants


def _load(src: Source) -> _Module:
    modules, marks, params = _pytest_bindings(src.tree)
    return _Module(
        src=src,
        constants=_module_constants(src.tree),
        functions={
            node.name: node
            for node in src.tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        },
        pytest_names=modules,
        mark_names=marks,
        param_names=params,
    )


def _mark_name(node: ast.expr, mod: _Module) -> str | None:
    """``pytest.mark.X`` / ``mark.X`` → ``"X"``."""
    if not isinstance(node, ast.Attribute):
        return None
    base = node.value
    if isinstance(base, ast.Name) and base.id in mod.mark_names:
        return node.attr
    if (
        isinstance(base, ast.Attribute)
        and base.attr == "mark"
        and isinstance(base.value, ast.Name)
        and base.value.id in mod.pytest_names
    ):
        return node.attr
    return None


def _is_param(node: ast.expr, mod: _Module) -> bool:
    if isinstance(node, ast.Name):
        return node.id in mod.param_names
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "param"
        and isinstance(node.value, ast.Name)
        and node.value.id in mod.pytest_names
    )


def _elements(
    node: ast.expr, env: Mapping[str, object]
) -> Iterator[tuple[ast.expr, Mapping[str, object]]]:
    """Yield each element of a parametrize argument list with its bindings."""
    if isinstance(node, (ast.List, ast.Tuple)):
        for elt in node.elts:
            yield elt, env
        return
    if (
        isinstance(node, (ast.ListComp, ast.GeneratorExp))
        and len(node.generators) == 1
        and not node.generators[0].ifs
        and isinstance(node.generators[0].target, ast.Name)
    ):
        values = _literal(node.generators[0].iter, env)
        if not isinstance(values, tuple):
            raise _Unresolved(ast.unparse(node.generators[0].iter))
        name = node.generators[0].target.id
        for value in values:
            yield node.elt, {**env, name: value}
        return
    raise _Unresolved(ast.unparse(node))


def _marks(
    node: ast.expr, mod: _Module, env: Mapping[str, object], depth: int = 0
) -> tuple[_Mark, ...]:
    """Evaluate a decorator or ``marks=`` expression to the marks it applies."""
    if depth > _MAX_DEPTH:
        raise _Unresolved("helper nesting")
    if isinstance(node, (ast.List, ast.Tuple)):
        return tuple(m for elt in node.elts for m in _marks(elt, mod, env, depth))
    name = _mark_name(node, mod)
    if name is not None:
        return (_Mark(name),)
    if not isinstance(node, ast.Call):
        raise _Unresolved(ast.unparse(node))
    name = _mark_name(node.func, mod)
    if name == "parametrize":
        if len(node.args) < 2:
            raise _Unresolved(ast.unparse(node))
        params: list[tuple[_Mark, ...]] = []
        for elt, bound in _elements(node.args[1], env):
            if isinstance(elt, ast.Call) and _is_param(elt.func, mod):
                marks_kw = next(
                    (kw.value for kw in elt.keywords if kw.arg == "marks"), None
                )
                params.append(_marks(marks_kw, mod, bound, depth) if marks_kw else ())
            else:
                params.append(())
        return (_Mark("parametrize", params=tuple(params)),)
    if name is not None:
        kwargs = {
            kw.arg: _literal(kw.value, env)
            for kw in node.keywords
            if kw.arg is not None
        }
        return (_Mark(name, kwargs),)
    if isinstance(node.func, ast.Name) and node.func.id in mod.functions:
        return _helper_marks(mod.functions[node.func.id], node, mod, env, depth)
    raise _Unresolved(ast.unparse(node))


def _helper_marks(
    helper: ast.FunctionDef | ast.AsyncFunctionDef,
    call: ast.Call,
    mod: _Module,
    env: Mapping[str, object],
    depth: int,
) -> tuple[_Mark, ...]:
    """Evaluate a module-level helper whose body is a single ``return``."""
    body = [
        stmt
        for stmt in helper.body
        if not (isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant))
    ]
    if len(body) != 1 or not isinstance(body[0], ast.Return) or body[0].value is None:
        raise _Unresolved(helper.name)
    names = [arg.arg for arg in (*helper.args.posonlyargs, *helper.args.args)]
    if len(call.args) > len(names) or helper.args.vararg or helper.args.kwarg:
        raise _Unresolved(helper.name)
    bound: dict[str, object] = dict(mod.constants)
    for arg_name, value in zip(names, call.args):
        bound[arg_name] = _literal(value, env)
    for kw in call.keywords:
        if kw.arg is None:
            raise _Unresolved(helper.name)
        bound[kw.arg] = _literal(kw.value, env)
    defaults = helper.args.defaults
    for arg_name, default in zip(names[len(names) - len(defaults) :], defaults):
        bound.setdefault(arg_name, _literal(default, mod.constants))
    if any(arg_name not in bound for arg_name in names):
        raise _Unresolved(helper.name)
    return _marks(body[0].value, mod, bound, depth + 1)


def _names_marker(node: ast.expr, mod: _Module) -> bool:
    """True when *node*, or the module helper it calls, mentions the marker."""
    if MARKER in ast.unparse(node):
        return True
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in mod.functions
        and MARKER in ast.unparse(mod.functions[node.func.id])
    )


def _pytestmark(
    body: list[ast.stmt], mod: _Module
) -> tuple[tuple[_Mark, ...], ast.expr | None]:
    for stmt in body:
        if (
            isinstance(stmt, ast.Assign)
            and len(stmt.targets) == 1
            and isinstance(stmt.targets[0], ast.Name)
            and stmt.targets[0].id == "pytestmark"
        ):
            return _marks(stmt.value, mod, mod.constants), stmt.value
    return (), None


def _calls(func: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(func):
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name):
                names.add(node.func.id)
            elif isinstance(node.func, ast.Attribute):
                names.add(node.func.attr)
    return names


def _assertions(func: ast.FunctionDef | ast.AsyncFunctionDef, mod: _Module) -> set[str]:
    """Calls made by the test, and by same-module helpers it calls directly."""
    direct = _calls(func)
    reached = set(direct)
    for name in direct & mod.functions.keys():
        reached |= _calls(mod.functions[name])
    return reached


def _runtime_skip(func: ast.FunctionDef | ast.AsyncFunctionDef, mod: _Module) -> bool:
    """An unconditional ``pytest.skip()`` / ``pytest.xfail()`` in the test body."""
    for stmt in func.body:
        if (
            isinstance(stmt, ast.Expr)
            and isinstance(stmt.value, ast.Call)
            and isinstance(stmt.value.func, ast.Attribute)
            and stmt.value.func.attr in _RUNTIME_SKIPS
            and isinstance(stmt.value.func.value, ast.Name)
            and stmt.value.func.value.id in mod.pytest_names
        ):
            return True
    return False


def _registrations(
    marks: tuple[_Mark, ...], inherited_skip: bool, node: ast.AST
) -> Iterator[_Registration]:
    skipped = inherited_skip or any(m.name in _SKIP_MARKS for m in marks)
    own = [m for m in marks if m.name == MARKER]
    for mark in own:
        yield _as_registration(mark, skipped, node)
    for parametrize in (m for m in marks if m.name == "parametrize"):
        for param in parametrize.params:
            param_skipped = skipped or any(m.name in _SKIP_MARKS for m in param)
            for mark in (m for m in param if m.name == MARKER):
                yield _as_registration(mark, param_skipped, node)


def _as_registration(mark: _Mark, skipped: bool, node: ast.AST) -> _Registration:
    rule, scenario = mark.kwargs.get("rule"), mark.kwargs.get("scenario")
    entrypoint = mark.kwargs.get("entrypoint", "default")
    if not all(isinstance(v, str) for v in (rule, scenario, entrypoint)):
        raise _Unresolved(MARKER)
    if rule != RULE:
        # Retired F017/F018 registrations, or another rule's: not F016 coverage.
        return _Registration("", "", False, True, node)
    return _Registration(
        scenario=str(scenario),
        entrypoint=str(entrypoint).replace("-", "_"),
        unsupported=bool(mark.kwargs.get("unsupported")),
        skipped=skipped,
        node=node,
    )


def _finding(mod: _Module, node: ast.AST, message: str) -> Finding:
    return make_finding(
        filename=mod.src.rel,
        rule_id=RULE,
        node=node,
        message=message,
        directives=mod.src.directives,
    )


def _scan_test_module(
    mod: _Module, expected: set[tuple[str, str]], covered: set[tuple[str, str]]
) -> list[Finding]:
    findings: list[Finding] = []
    try:
        module_marks, _ = _pytestmark(mod.src.tree.body, mod)
    except _Unresolved:
        module_marks = ()
    module_skip = any(m.name in _SKIP_MARKS for m in module_marks)

    def visit(
        func: ast.FunctionDef | ast.AsyncFunctionDef,
        inherited: tuple[_Mark, ...],
    ) -> None:
        marks: list[_Mark] = list(inherited)
        for dec in func.decorator_list:
            try:
                marks.extend(_marks(dec, mod, mod.constants))
            except _Unresolved:
                if _names_marker(dec, mod):
                    findings.append(
                        _finding(
                            mod,
                            dec,
                            f"Preflight scenario registration on {func.name} is not "
                            "statically resolvable. Spell the marker's rule, scenario "
                            "and entrypoint as literals, directly or through a "
                            "module-level helper whose body is a single return; an "
                            "unreadable registration is not counted as coverage.",
                        )
                    )
        try:
            regs = list(
                _registrations(
                    tuple(marks), module_skip or _runtime_skip(func, mod), func
                )
            )
        except _Unresolved:
            findings.append(
                _finding(
                    mod,
                    func,
                    f"Preflight scenario registration on {func.name} has a rule, "
                    "scenario or entrypoint that is not a literal string; it is not "
                    "counted as coverage.",
                )
            )
            return
        regs = [r for r in regs if r.scenario]
        if not regs:
            return
        calls = _assertions(func, mod)
        for reg in regs:
            key = (reg.entrypoint, reg.scenario)
            label = f"{reg.scenario} for entrypoint {reg.entrypoint}"
            if key not in expected:
                findings.append(
                    _finding(
                        mod,
                        func,
                        f"{func.name} registers preflight scenario {label}, which is "
                        f"not in the F016 matrix (scenarios: {', '.join(SCENARIOS[RULE])}; "
                        f"entrypoints: {', '.join(sorted({e for e, _ in expected}))}).",
                    )
                )
            elif reg.unsupported:
                findings.append(
                    _finding(
                        mod,
                        func,
                        f"Preflight scenario {label} is declared unsupported; a "
                        "declared gap is still a gap in the matrix.",
                    )
                )
            elif reg.skipped:
                findings.append(
                    _finding(
                        mod,
                        func,
                        f"Preflight scenario {label} is registered on a skipped or "
                        f"xfail test ({func.name}); a test that does not run does not "
                        "define the scenario.",
                    )
                )
            else:
                required = {_RESULT_ASSERTION}
                if reg.scenario in _LIFETIME_SCENARIOS:
                    required.add(_LIFETIME_ASSERTION)
                missing = sorted(required - calls)
                if missing:
                    findings.append(
                        _finding(
                            mod,
                            func,
                            f"Preflight scenario {label} ({func.name}) never calls "
                            f"{', '.join(missing)} from conformance.preflight_testing; "
                            "registration without the contract assertion does not "
                            "define the scenario.",
                        )
                    )
                else:
                    covered.add(key)

    for node in mod.src.tree.body:
        if is_test_function(node):
            visit(node, module_marks)
        elif is_test_class(node) and not any(
            isinstance(stmt, ast.FunctionDef) and stmt.name == "__init__"
            for stmt in node.body
        ):
            try:
                class_marks = [
                    m
                    for dec in node.decorator_list
                    for m in _marks(dec, mod, mod.constants)
                ]
                body_marks, _ = _pytestmark(node.body, mod)
            except _Unresolved:
                class_marks, body_marks = [], ()
            inherited = (*module_marks, *class_marks, *body_marks)
            for stmt in node.body:
                if is_test_function(stmt):
                    visit(stmt, inherited)
    return findings


def is_scenario_test_path(rel: str) -> bool:
    """A pytest-collectable module under ``tests/``, where scenarios must live."""
    parts = Path(rel).parts
    return (
        len(parts) >= 2 and parts[0] == "tests" and is_collectable_test_file(parts[-1])
    )


def declares_preflight(reg: Registry) -> bool:
    """True when the app defines a ``preflight_check`` of its own to test.

    The scenarios drive the real handler, so an app that inherits the SDK
    default has nothing app-owned for them to exercise. Any definition counts,
    resolved or not: one the analysis could not resolve is F019's to report,
    and must not also make its scenarios optional.
    """
    return any(
        isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == "preflight_check"
        for src in reg.sources
        for node in ast.walk(src.tree)
    ) or any(
        isinstance(node, ast.Assign)
        and any(
            isinstance(t, ast.Name) and t.id == "preflight_check" for t in node.targets
        )
        for src in reg.sources
        for node in ast.walk(src.tree)
    )


def scan(reg: Registry, tests: Registry) -> list[Finding]:
    """Grade the scenario registrations in *tests* against the matrix *reg* implies."""
    if not declares_preflight(reg):
        return []
    entrypoints = tuple(entrypoint_contracts(reg)) or ("default",)
    expected = {(entry, name) for entry in entrypoints for name in SCENARIOS[RULE]}
    covered: set[tuple[str, str]] = set()
    findings: list[Finding] = []
    for src in tests.sources:
        if is_scenario_test_path(src.rel):
            findings.extend(_scan_test_module(_load(src), expected, covered))
    for entry, scenario in sorted(expected - covered):
        findings.append(
            Finding(
                rule_id=RULE,
                file="pyproject.toml",
                line=1,
                column=1,
                discriminator=f"{entry}:{scenario}",
                message=(
                    f"Preflight scenario {scenario} for entrypoint {entry} is not "
                    "registered. Add a collectable test under tests/ that drives the "
                    "real handler, calls assert_preflight_result, and is marked "
                    f'@pytest.mark.preflight_conformance(rule="F016", scenario="{scenario}"'
                    + ("" if entry == "default" else f', entrypoint="{entry}"')
                    + ")."
                ),
            )
        )
    return findings
