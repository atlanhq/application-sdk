"""F016: every required preflight scenario is registered as a collectable test.

Conformance measures that the scenarios are *defined*; whether they *pass* is
the test gate's measure, and nothing here executes them. A scenario counts
when a pytest-collected test under ``tests/unit/`` — the tier the test gate's
unit job always runs — carries
``pytest.mark.preflight_conformance(rule="F016", scenario=..., entrypoint=...)``,
runs (no skip or xfail applies), and calls the contract assertion the scenario
needs, imported from ``conformance.preflight_testing``.

The reader accepts an allowlist of shapes — the ones the reference apps
(atlan-openapi-app, atlan-mysql-app, atlan-metabase-app) use — and reports
anything else rather than modelling it. A shape outside the list is never
credited, however it would behave at runtime; rewriting it into a listed
shape is the fix.

Registration, on the test function, its class, or a ``pytestmark``:

* ``pytest.mark.preflight_conformance(...)`` with literal arguments;
* ``pytest.mark.parametrize`` whose ``pytest.param`` cases carry the marker,
  inline or returned by a module-level helper whose body is a single
  ``return`` (atlan-metabase-app's ``entrypoint_matrix("healthy")``). Its
  parameter list may be a comprehension over a module-level tuple of names.
  When it supplies an ``entrypoint`` argument, each case's value must be the
  entrypoint its marker claims;
* any other ``pytest.mark.*``: ``skip``, and ``skipif``/``xfail`` with a
  literal or absent condition, decide whether the test runs; the rest do not
  affect it;
* a decorator imported from another module (``respx.mock``, ``mock.patch``),
  which is taken not to skip the test.

Any other decorator — another same-module function, a lambda, a module-level
mark alias — leaves whether the test runs unknown: its scenarios are reported
and not counted.

Assertion: ``assert_preflight_result`` (plus ``assert_probe_lifetime`` for the
lifetime scenarios), called as a statement of the test body, or of a
top-level ``for`` loop over a provably non-empty iterable — a non-empty list
or tuple literal; a name bound once to a non-empty tuple (in the test or at
module level), or to a non-empty list in the test that nothing mentions
before the loop; ``range(n)`` with ``n >= 1``; or ``enumerate`` of any of
those. No statement before it may ``return``,
``yield``, leave the loop, or call ``pytest.skip``/``xfail``/``exit``/
``importorskip``. Those are the only ways a test passes without making the
call: anything else that stops it first — an exception, a ``pytest.raises``
that saw none — fails the test, and the test gate reports the failure. A call
inside ``if``/``try``/``with``, or made by a helper, is not counted.

A name resolves only when the module binds it exactly once — by the import
the reader expects — and the test does not rebind it. A star import leaves
every name unresolved.
"""

from __future__ import annotations

import ast
from collections import Counter
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field, replace
from enum import Enum
from pathlib import Path

from conformance.preflight_scenarios import SCENARIOS
from conformance.suite.checks._ast_common import make_finding
from conformance.suite.checks._ast_common._pytest_collection import (
    is_collectable_test_file,
    is_test_class,
    is_test_function,
)
from conformance.suite.schema.findings import Finding

from ._common import Registry, Source, binding_targets, entrypoint_contracts

RULE = "F016"
MARKER = "preflight_conformance"

#: The tier the test gate always runs. The unit job runs ``tests/unit/``
#: unconditionally; the integration job is conditional and caller-scoped, so
#: a scenario defined anywhere else is not guaranteed to be executed by it.
SCENARIO_TEST_ROOT = ("tests", "unit")

#: The contract assertion every registered scenario must make, plus the extra
#: one the lifetime scenarios need. Mirrors what the scenario matrix asks each
#: scenario to prove: a typed verdict, and for these three, that the probe
#: stayed inside its budget and left nothing running.
_ASSERTION_MODULE = "conformance.preflight_testing"
_RESULT_ASSERTION = "assert_preflight_result"
_LIFETIME_ASSERTION = "assert_probe_lifetime"
_ASSERTIONS = frozenset({_RESULT_ASSERTION, _LIFETIME_ASSERTION})
_LIFETIME_SCENARIOS = frozenset({"hung_probe", "cancellation_cleanup", "budget_retry"})

_RUNTIME_SKIPS = frozenset({"skip", "xfail"})
#: pytest calls that end a test without failing it.
_STOPS = frozenset({"skip", "xfail", "exit", "importorskip"})

#: Helper indirection is followed this deep; a registration nested further is
#: reported as unresolved rather than chased.
_MAX_DEPTH = 3


class _Unresolved(Exception):
    """A value the static reader cannot determine."""


class _Unknown:
    """A mark argument whose value the reader cannot determine."""

    def __repr__(self) -> str:
        return "<unknown>"


UNKNOWN = _Unknown()

#: Stands in for a decorator or ``pytestmark`` element outside the allowlist:
#: its effect on whether the test runs is unknown, so nothing it covers counts.
_UNRESOLVED_MARK = "<unresolved>"


class _Runs(Enum):
    YES = "runs"
    NO = "skipped"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class _Param:
    marks: tuple[_Mark, ...]
    #: The case's argument values, by argname; ``UNKNOWN`` where unreadable.
    values: Mapping[str, object]


@dataclass(frozen=True)
class _Mark:
    name: str
    args: tuple[object, ...] = ()
    kwargs: Mapping[str, object] = field(default_factory=dict)
    #: For ``parametrize``: the argnames and each case it declares.
    argnames: tuple[str, ...] = ()
    params: tuple[_Param, ...] = ()


@dataclass(frozen=True)
class _Registration:
    scenario: str
    entrypoint: str
    unsupported: bool
    runs: _Runs
    #: The ``entrypoint`` value(s) the runnable cases actually supply, when
    #: none of them is the one the marker claims.
    runs_as: str | None = None


@dataclass(frozen=True)
class _Module:
    """What the module's once-bound names resolve to."""

    src: Source
    constants: dict[str, object]
    #: Module-level functions: the candidate registration helpers.
    functions: dict[str, ast.FunctionDef | ast.AsyncFunctionDef]
    #: Module-level names bound to a list or tuple literal.
    sequences: dict[str, ast.List | ast.Tuple]
    pytest_names: frozenset[str]
    mark_names: frozenset[str]
    param_names: frozenset[str]
    #: Names from-imported from pytest that end a test without failing it.
    stop_names: frozenset[str]
    #: Local name → contract assertion it is bound to.
    assertion_names: dict[str, str]
    #: Local dotted paths bound to the ``conformance.preflight_testing`` module.
    assertion_modules: frozenset[str]
    #: Every name an import binds.
    imported: frozenset[str]
    #: Every name the module binds, however many times.
    bound: frozenset[str]


# --- module bindings ------------------------------------------------------------


def _bound_names(node: ast.stmt) -> set[str]:
    """Names a statement binds directly, other than by import."""
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return {node.name}
    targets: list[ast.expr] = []
    if isinstance(node, ast.Assign):
        targets = list(node.targets)
    elif isinstance(node, (ast.AnnAssign, ast.AugAssign, ast.For, ast.AsyncFor)):
        targets = [node.target]
    elif isinstance(node, (ast.With, ast.AsyncWith)):
        targets = [i.optional_vars for i in node.items if i.optional_vars is not None]
    elif isinstance(node, ast.Delete):
        targets = list(node.targets)
    elif isinstance(node, ast.Try | ast.TryStar):
        return {h.name for h in node.handlers if h.name}
    return {
        sub.id
        for target in targets
        for sub in ast.walk(target)
        if isinstance(sub, ast.Name)
    }


def _nested_blocks(node: ast.stmt) -> list[list[ast.stmt]]:
    """Statement blocks a compound statement runs in the enclosing scope."""
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return []
    blocks = [
        block
        for name in ("body", "orelse", "finalbody")
        if isinstance(block := getattr(node, name, None), list)
    ]
    blocks += [h.body for h in getattr(node, "handlers", None) or []]
    blocks += [c.body for c in getattr(node, "cases", None) or []]
    return blocks


def _module_statements(stmts: list[ast.stmt]) -> Iterator[ast.stmt]:
    """Every statement that runs in the module scope, nested blocks included."""
    for node in stmts:
        yield node
        for block in _nested_blocks(node):
            yield from _module_statements(block)


def _import_locals(node: ast.Import | ast.ImportFrom) -> Iterator[tuple[str, str]]:
    """``(local name, imported name)`` for each alias an import binds."""
    for alias in node.names:
        if isinstance(node, ast.Import):
            yield alias.asname or alias.name.split(".", 1)[0], alias.name
        elif alias.name != "*":
            yield alias.asname or alias.name, alias.name


def _binding_counts(tree: ast.Module) -> tuple[Counter[str], bool]:
    """How many times the module binds each name, and whether it star-imports."""
    counts: Counter[str] = Counter()
    star = False
    for node in _module_statements(tree.body):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            star |= any(alias.name == "*" for alias in node.names)
            counts.update(local for local, _ in _import_locals(node))
        else:
            counts.update(_bound_names(node))
    return counts, star


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


def _value(node: ast.expr, env: Mapping[str, object]) -> object:
    """``_literal``, or ``UNKNOWN`` where the value cannot be read."""
    try:
        return _literal(node, env)
    except _Unresolved:
        return UNKNOWN


def _single_assignment(node: ast.stmt) -> tuple[str, ast.expr] | None:
    """``name = value`` or ``name: T = value``, as ``(name, value)``."""
    if (
        isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name)
    ):
        return node.targets[0].id, node.value
    if (
        isinstance(node, ast.AnnAssign)
        and isinstance(node.target, ast.Name)
        and node.value is not None
    ):
        return node.target.id, node.value
    return None


def _load(src: Source) -> _Module:
    counts, star = _binding_counts(src.tree)

    def once(name: str) -> bool:
        return not star and counts[name] == 1

    pytest_names: set[str] = set()
    marks: set[str] = set()
    params: set[str] = set()
    stops: set[str] = set()
    assertion_names: dict[str, str] = {}
    assertion_modules: set[str] = set()
    imported: set[str] = set()
    for node in _module_statements(src.tree.body):
        if isinstance(node, ast.Import):
            for alias in node.names:
                local = alias.asname or alias.name.split(".", 1)[0]
                if not once(local):
                    continue
                imported.add(local)
                if alias.name == "pytest":
                    pytest_names.add(local)
                elif alias.name == _ASSERTION_MODULE:
                    # `import a.b` binds `a`, and the module is reached as `a.b`.
                    assertion_modules.add(alias.asname or alias.name)
        elif isinstance(node, ast.ImportFrom) and not node.level:
            for local, name in _import_locals(node):
                if not once(local):
                    continue
                imported.add(local)
                if node.module == "pytest":
                    if name == "mark":
                        marks.add(local)
                    elif name == "param":
                        params.add(local)
                    elif name in _STOPS:
                        stops.add(local)
                elif node.module == _ASSERTION_MODULE and name in _ASSERTIONS:
                    assertion_names[local] = name
                elif node.module == "conformance" and name == "preflight_testing":
                    assertion_modules.add(local)

    constants: dict[str, object] = {}
    sequences: dict[str, ast.List | ast.Tuple] = {}
    functions: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] = {}
    for node in src.tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if once(node.name):
                functions[node.name] = node
            continue
        assignment = _single_assignment(node)
        if assignment is None or not once(assignment[0]):
            continue
        name, value = assignment
        if isinstance(value, (ast.List, ast.Tuple)):
            sequences[name] = value
        try:
            constants[name] = _literal(value, constants)
        except _Unresolved:
            pass
    return _Module(
        src=src,
        constants=constants,
        functions=functions,
        sequences=sequences,
        pytest_names=frozenset(pytest_names),
        mark_names=frozenset(marks),
        param_names=frozenset(params),
        stop_names=frozenset(stops),
        assertion_names=assertion_names,
        assertion_modules=frozenset(assertion_modules),
        imported=frozenset(imported),
        bound=frozenset(counts),
    )


# --- registrations: decorators and pytestmark -----------------------------------


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


def _argnames(node: ast.expr, env: Mapping[str, object]) -> tuple[str, ...]:
    value = _literal(node, env)
    if isinstance(value, str):
        return tuple(part.strip() for part in value.split(",") if part.strip())
    if isinstance(value, tuple) and all(isinstance(v, str) for v in value):
        return tuple(str(v) for v in value)
    raise _Unresolved(ast.unparse(node))


def _case_values(
    elt: ast.expr, argnames: tuple[str, ...], env: Mapping[str, object]
) -> dict[str, object]:
    """The argument values one parametrize case binds, by argname."""
    if len(argnames) == 1:
        return {argnames[0]: _value(elt, env)}
    if isinstance(elt, (ast.Tuple, ast.List)) and len(elt.elts) == len(argnames):
        return {name: _value(v, env) for name, v in zip(argnames, elt.elts)}
    return dict.fromkeys(argnames, UNKNOWN)


def _parametrize(
    node: ast.Call, mod: _Module, env: Mapping[str, object], depth: int
) -> _Mark:
    argnames_node = node.args[0] if node.args else None
    argvalues_node = node.args[1] if len(node.args) > 1 else None
    for kw in node.keywords:
        if kw.arg == "argnames":
            argnames_node = kw.value
        elif kw.arg == "argvalues":
            argvalues_node = kw.value
    if argnames_node is None or argvalues_node is None:
        raise _Unresolved(ast.unparse(node))
    argnames = _argnames(argnames_node, env)
    params: list[_Param] = []
    for elt, bound in _elements(argvalues_node, env):
        if isinstance(elt, ast.Call) and _is_param(elt.func, mod):
            marks_kw = next(
                (kw.value for kw in elt.keywords if kw.arg == "marks"), None
            )
            marks = _marks(marks_kw, mod, bound, depth) if marks_kw else ()
            if len(elt.args) == len(argnames):
                values = {n: _value(v, bound) for n, v in zip(argnames, elt.args)}
            else:
                values = dict.fromkeys(argnames, UNKNOWN)
            params.append(_Param(marks, values))
        else:
            params.append(_Param((), _case_values(elt, argnames, bound)))
    return _Mark("parametrize", argnames=argnames, params=tuple(params))


def _marks(
    node: ast.expr, mod: _Module, env: Mapping[str, object], depth: int = 0
) -> tuple[_Mark, ...]:
    """Evaluate a decorator or ``marks=`` expression to the marks it applies."""
    if depth > _MAX_DEPTH:
        raise _Unresolved("helper nesting")
    if isinstance(node, (ast.List, ast.Tuple)):
        return tuple(mark for elt in node.elts for mark in _marks(elt, mod, env, depth))
    name = _mark_name(node, mod)
    if name is not None:
        return (_Mark(name),)
    if not isinstance(node, ast.Call):
        raise _Unresolved(ast.unparse(node))
    name = _mark_name(node.func, mod)
    if name == "parametrize":
        return (_parametrize(node, mod, env, depth),)
    if name is not None:
        return (
            _Mark(
                name,
                args=tuple(_value(arg, env) for arg in node.args),
                kwargs={
                    kw.arg: _value(kw.value, env)
                    for kw in node.keywords
                    if kw.arg is not None
                },
            ),
        )
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
    if isinstance(helper, ast.AsyncFunctionDef) or helper.decorator_list:
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
    """True when *node*, or the same-module helper it calls, spells the marker."""
    target = node.func if isinstance(node, ast.Call) else node
    source: ast.AST = node
    if isinstance(target, ast.Name) and target.id in mod.functions:
        source = mod.functions[target.id]
    return any(
        isinstance(sub, ast.Attribute) and sub.attr == MARKER
        for sub in ast.walk(source)
    )


def _is_imported(node: ast.expr, mod: _Module) -> bool:
    """A decorator reached through a name another module supplied.

    ``respx.mock``, ``mock.patch("x")``: its root is an import, not pytest and
    not code in this module, so it is taken not to skip the test.
    """
    target = node.func if isinstance(node, ast.Call) else node
    while isinstance(target, ast.Attribute):
        target = target.value
    return (
        isinstance(target, ast.Name)
        and target.id in mod.imported
        and target.id not in mod.pytest_names | mod.mark_names | mod.param_names
    )


def _decorator_marks(
    decorators: list[ast.expr], mod: _Module
) -> tuple[tuple[_Mark, ...], list[ast.expr]]:
    """Resolved marks, and the decorators naming the marker that did not resolve.

    A decorator outside the allowlist is kept as ``_UNRESOLVED_MARK``: whether
    pytest collects a runnable case is then unknown, and nothing it covers is
    credited. Only imported decorators are dropped.
    """
    marks: list[_Mark] = []
    unresolved: list[ast.expr] = []
    for dec in decorators:
        try:
            marks.extend(_marks(dec, mod, mod.constants))
        except _Unresolved:
            if _names_marker(dec, mod):
                unresolved.append(dec)
            elif not _is_imported(dec, mod):
                marks.append(_Mark(_UNRESOLVED_MARK))
    return tuple(marks), unresolved


def _lenient_marks(node: ast.expr, mod: _Module) -> tuple[_Mark, ...]:
    """Marks from a ``pytestmark`` value, element by element.

    A resolvable element keeps its effect; an unreadable one becomes
    ``_UNRESOLVED_MARK`` rather than taking its siblings (a readable ``skip``
    among them) down with it.
    """
    elements = node.elts if isinstance(node, (ast.List, ast.Tuple)) else [node]
    marks: list[_Mark] = []
    for elt in elements:
        try:
            marks.extend(_marks(elt, mod, mod.constants))
        except _Unresolved:
            marks.append(_Mark(_UNRESOLVED_MARK))
    return tuple(marks)


def _pytestmark(body: list[ast.stmt], mod: _Module) -> tuple[_Mark, ...]:
    """The marks a module or class body applies through ``pytestmark``.

    Any binding counts — a plain, chained or annotated assignment — and the
    last one wins, as it does at import time.
    """
    marks: tuple[_Mark, ...] = ()
    for stmt in body:
        value: ast.expr | None = None
        if isinstance(stmt, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "pytestmark" for t in stmt.targets
        ):
            value = stmt.value
        elif (
            isinstance(stmt, ast.AnnAssign)
            and isinstance(stmt.target, ast.Name)
            and stmt.target.id == "pytestmark"
            and stmt.value is not None
        ):
            value = stmt.value
        elif (
            isinstance(stmt, ast.AugAssign)
            and isinstance(stmt.target, ast.Name)
            and stmt.target.id == "pytestmark"
        ):
            marks = (*marks, *_lenient_marks(stmt.value, mod))
            continue
        if value is not None:
            marks = _lenient_marks(value, mod)
    return marks


# --- whether a mark lets the test run ------------------------------------------


def _condition(mark: _Mark) -> object:
    """A ``skipif``/``xfail`` condition: absent → ``True``; a string → unknown."""
    if mark.args:
        value = mark.args[0]
    elif "condition" in mark.kwargs:
        value = mark.kwargs["condition"]
    else:
        return True
    if isinstance(value, bool | int):
        return bool(value)
    # pytest evaluates a string condition itself; a non-literal is unreadable.
    return UNKNOWN


def _mark_runs(mark: _Mark) -> _Runs:
    if mark.name == _UNRESOLVED_MARK:
        return _Runs.UNKNOWN
    if mark.name == "skip":
        return _Runs.NO
    if mark.name in {"skipif", "xfail"}:
        condition = _condition(mark)
        if condition is UNKNOWN:
            return _Runs.UNKNOWN
        return _Runs.NO if condition else _Runs.YES
    return _Runs.YES


def _combine(*states: _Runs) -> _Runs:
    if _Runs.NO in states:
        return _Runs.NO
    if _Runs.UNKNOWN in states:
        return _Runs.UNKNOWN
    return _Runs.YES


def _marks_run(marks: tuple[_Mark, ...]) -> _Runs:
    return _combine(*(_mark_runs(m) for m in marks))


def _any_case_runs(parametrize: _Mark, exclude: _Param | None = None) -> _Runs:
    """Whether at least one case of *parametrize* runs (empty → none does)."""
    states = [_marks_run(p.marks) for p in parametrize.params if p is not exclude]
    if _Runs.YES in states:
        return _Runs.YES
    if _Runs.UNKNOWN in states:
        return _Runs.UNKNOWN
    return _Runs.NO


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


# --- which assertions a test makes ----------------------------------------------


_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)


def _own_nodes(stmts: list[ast.stmt]) -> Iterator[ast.AST]:
    """The nodes of *stmts* in their own scope: a nested definition is
    yielded, but not its body."""
    stack: list[ast.AST] = list(stmts)
    while stack:
        node = stack.pop()
        yield node
        if not isinstance(node, _SCOPES):
            stack.extend(ast.iter_child_nodes(node))


def _local_names(func: ast.FunctionDef | ast.AsyncFunctionDef) -> Counter[str]:
    """How many times *func* binds each name — its parameters included.

    A fixture parameter or a local named ``assert_preflight_result`` is not
    the contract assertion, whatever the module imported.
    """
    args = func.args
    counts: Counter[str] = Counter(
        a.arg
        for a in (
            *args.posonlyargs,
            *args.args,
            *args.kwonlyargs,
            args.vararg,
            args.kwarg,
        )
        if a is not None
    )
    for node in _own_nodes(func.body):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store | ast.Del):
            counts[node.id] += 1
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            counts[node.name] += 1
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            counts.update(local for local, _ in _import_locals(node))
        elif isinstance(node, ast.ExceptHandler) and node.name:
            counts[node.name] += 1
    return counts


def _assertion(stmt: ast.stmt, mod: _Module, local: Counter[str]) -> str | None:
    """The contract assertion *stmt* calls, when it is a bare call statement."""
    if not (isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call)):
        return None
    func = stmt.value.func
    if isinstance(func, ast.Name) and func.id not in local:
        return mod.assertion_names.get(func.id)
    if isinstance(func, ast.Attribute) and func.attr in _ASSERTIONS:
        dotted = ast.unparse(func.value)
        if dotted.split(".", 1)[0] not in local and dotted in mod.assertion_modules:
            return func.attr
    return None


def _stops(stmt: ast.stmt, mod: _Module, *, in_loop: bool) -> bool:
    """Whether *stmt* can end the test, or leave the loop, without failing it."""
    for node in _own_nodes([stmt]):
        if isinstance(node, (ast.Return, ast.Yield, ast.YieldFrom)):
            return True
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id in mod.stop_names:
                return True
            if (
                isinstance(func, ast.Attribute)
                and func.attr in _STOPS
                and isinstance(func.value, ast.Name)
                and func.value.id in mod.pytest_names
            ):
                return True
    return in_loop and _leaves_loop(stmt)


def _leaves_loop(stmt: ast.stmt) -> bool:
    """A ``break``/``continue`` in *stmt* that belongs to the enclosing loop."""
    if isinstance(stmt, (ast.Break, ast.Continue)):
        return True
    if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return False
    if isinstance(stmt, (ast.For, ast.AsyncFor, ast.While)):
        # A jump in a nested loop's body leaves only that loop; its else
        # clause runs in ours.
        return any(_leaves_loop(s) for s in stmt.orelse)
    return any(_leaves_loop(s) for block in _nested_blocks(stmt) for s in block)


def _builtin(name: str, mod: _Module, local: Counter[str]) -> bool:
    return name not in local and name not in mod.bound


def _mentions(stmts: list[ast.stmt], name: str) -> bool:
    """Whether any of *stmts*, nested scopes included, refers to *name*."""
    return any(
        isinstance(node, ast.Name) and node.id == name
        for stmt in stmts
        for node in ast.walk(stmt)
    )


def _non_empty(
    node: ast.expr,
    before: list[ast.stmt],
    mod: _Module,
    local: Counter[str],
) -> bool:
    """Whether iterating *node* provably runs the loop body at least once.

    A name counts when it is bound once to a non-empty literal that is still
    non-empty when the loop starts. A tuple always is. A list can be emptied
    by anything that reaches it, so a local list counts only when nothing
    between its binding and the loop mentions the name, and a module-level
    list — reachable from any code that runs first — never does.
    """
    if isinstance(node, (ast.List, ast.Tuple)):
        return any(not isinstance(elt, ast.Starred) for elt in node.elts)
    if isinstance(node, ast.Name):
        if local[node.id] == 1:
            for index, stmt in enumerate(before):
                assignment = _single_assignment(stmt)
                if assignment is None or assignment[0] != node.id:
                    continue
                value = assignment[1]
                if not isinstance(value, ast.Tuple) and _mentions(
                    before[index + 1 :], node.id
                ):
                    return False
                return _non_empty(value, before, mod, local)
            return False
        sequence = mod.sequences.get(node.id)
        if node.id not in local and isinstance(sequence, ast.Tuple):
            return _non_empty(sequence, before, mod, local)
        return False
    if not (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and _builtin(node.func.id, mod, local)
    ):
        return False
    if node.func.id == "range" and not node.keywords:
        bounds = [
            arg.value
            for arg in node.args
            if isinstance(arg, ast.Constant) and type(arg.value) is int
        ]
        if len(bounds) != len(node.args):
            return False
        if len(bounds) == 1:
            return bounds[0] >= 1
        return len(bounds) == 2 and bounds[1] > bounds[0]
    if (
        node.func.id == "enumerate"
        and len(node.args) == 1
        and all(kw.arg == "start" for kw in node.keywords)
    ):
        return _non_empty(node.args[0], before, mod, local)
    return False


def _assertions(func: ast.FunctionDef | ast.AsyncFunctionDef, mod: _Module) -> set[str]:
    """Contract assertions the test makes in an accepted shape."""
    if any(isinstance(n, (ast.Yield, ast.YieldFrom)) for n in _own_nodes(func.body)):
        return set()  # a generator test's body is never run by pytest
    local = _local_names(func)
    made: set[str] = set()
    for index, stmt in enumerate(func.body):
        if (name := _assertion(stmt, mod, local)) is not None:
            made.add(name)
        elif isinstance(stmt, ast.For) and _non_empty(
            stmt.iter, func.body[:index], mod, local
        ):
            for inner in stmt.body:
                if (name := _assertion(inner, mod, local)) is not None:
                    made.add(name)
                if _stops(inner, mod, in_loop=True):
                    break
        if _stops(stmt, mod, in_loop=False):
            break
    return made


# --- registrations --------------------------------------------------------------


def _registrations(
    marks: tuple[_Mark, ...], body_skips: bool
) -> Iterator[_Registration]:
    """Every scenario registration *marks* make, and whether each one runs."""
    parametrizes = [m for m in marks if m.name == "parametrize"]
    plain = tuple(m for m in marks if m.name != "parametrize")
    base = _Runs.NO if body_skips else _marks_run(plain)
    for mark in (m for m in plain if m.name == MARKER):
        # A function-level registration runs only if every parametrize it is
        # combined with has at least one case that runs — and, where one
        # supplies `entrypoint`, a runnable case with the claimed value.
        reg = _as_registration(
            mark, _combine(base, *(_any_case_runs(p) for p in parametrizes))
        )
        yield _match_entrypoint(reg, parametrizes)
    for index, parametrize in enumerate(parametrizes):
        others = [p for i, p in enumerate(parametrizes) if i != index]
        for param in parametrize.params:
            runs = _combine(
                base, _marks_run(param.marks), *(_any_case_runs(p) for p in others)
            )
            for mark in (m for m in param.marks if m.name == MARKER):
                reg = _as_registration(mark, runs)
                if "entrypoint" in param.values:
                    yield _check_value(reg, param.values["entrypoint"])
                else:
                    yield _match_entrypoint(reg, others)


def _normalise(value: object) -> object:
    return value.replace("-", "_") if isinstance(value, str) else value


def _check_value(reg: _Registration, actual: object) -> _Registration:
    """Hold a registration to the entrypoint value its own case runs with."""
    if actual is UNKNOWN:
        return replace(reg, runs=_combine(reg.runs, _Runs.UNKNOWN))
    if _normalise(actual) != reg.entrypoint:
        # Any known value that differs — a string, None, a number — is a
        # different entrypoint from the one the marker claims.
        return replace(reg, runs_as=repr(actual))
    return reg


def _match_entrypoint(reg: _Registration, parametrizes: list[_Mark]) -> _Registration:
    """Hold a registration to the `entrypoint` a combined parametrize supplies.

    Credit needs a case that runs with the claimed value. A case whose run
    state or value is unreadable leaves it unknown; a set of runnable cases
    that all supply other values is a mismatch.
    """
    for parametrize in parametrizes:
        if "entrypoint" not in parametrize.argnames:
            continue
        runnable = [
            (state, p.values.get("entrypoint", UNKNOWN))
            for p in parametrize.params
            if (state := _marks_run(p.marks)) is not _Runs.NO
        ]
        if any(
            state is _Runs.YES and _normalise(value) == reg.entrypoint
            for state, value in runnable
        ):
            continue
        if any(
            value is UNKNOWN or _normalise(value) == reg.entrypoint
            for _, value in runnable
        ):
            reg = replace(reg, runs=_combine(reg.runs, _Runs.UNKNOWN))
            continue
        if runnable:
            values = sorted({repr(v) for _, v in runnable})
            reg = replace(reg, runs_as=", ".join(values))
    return reg


def _as_registration(mark: _Mark, runs: _Runs) -> _Registration:
    rule, scenario = mark.kwargs.get("rule"), mark.kwargs.get("scenario")
    entrypoint = mark.kwargs.get("entrypoint", "default")
    if not all(isinstance(v, str) for v in (rule, scenario, entrypoint)):
        raise _Unresolved(MARKER)
    if rule != RULE:
        # Retired F017/F018 registrations, or another rule's: not F016 coverage.
        return _Registration("", "", False, _Runs.NO)
    return _Registration(
        scenario=str(scenario),
        entrypoint=str(entrypoint).replace("-", "_"),
        unsupported=mark.kwargs.get("unsupported") not in (None, False),
        runs=runs,
    )


def _finding(mod: _Module, node: ast.AST, message: str) -> Finding:
    return make_finding(
        filename=mod.src.rel,
        rule_id=RULE,
        node=node,
        message=message,
        directives=mod.src.directives,
    )


_UNRESOLVED_REGISTRATION = (
    "Preflight scenario registration on {name} is not statically resolvable. "
    "Spell the marker's rule, scenario and entrypoint as literals, directly or "
    "through a module-level helper whose body is a single return; an "
    "unreadable registration is not counted as coverage."
)


def _scan_test_module(
    mod: _Module, expected: set[tuple[str, str]], covered: set[tuple[str, str]]
) -> list[Finding]:
    findings: list[Finding] = []
    module_marks = _pytestmark(mod.src.tree.body, mod)

    def visit(
        func: ast.FunctionDef | ast.AsyncFunctionDef,
        inherited: tuple[_Mark, ...],
    ) -> None:
        own, unresolved = _decorator_marks(func.decorator_list, mod)
        for dec in unresolved:
            findings.append(
                _finding(mod, dec, _UNRESOLVED_REGISTRATION.format(name=func.name))
            )
        try:
            regs = list(_registrations((*inherited, *own), _runtime_skip(func, mod)))
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
        made = _assertions(func, mod)
        for reg in regs:
            key = (reg.entrypoint, reg.scenario)
            label = f"{reg.scenario} for entrypoint {reg.entrypoint}"
            if key not in expected:
                message = (
                    f"{func.name} registers preflight scenario {label}, which is "
                    f"not in the F016 matrix (scenarios: {', '.join(SCENARIOS[RULE])}; "
                    f"entrypoints: {', '.join(sorted({e for e, _ in expected}))})."
                )
            elif reg.runs_as is not None:
                message = (
                    f"{func.name} registers preflight scenario {label}, but its "
                    f"runnable cases run with entrypoint={reg.runs_as}; a case "
                    "defines only the entrypoint it actually runs."
                )
            elif reg.unsupported:
                message = (
                    f"Preflight scenario {label} is declared unsupported; a "
                    "declared gap is still a gap in the matrix."
                )
            elif reg.runs is _Runs.NO:
                message = (
                    f"Preflight scenario {label} is registered on a test that does "
                    f"not run ({func.name}: skipped, xfail, or no parametrized case "
                    "runs); a test that does not run does not define the scenario."
                )
            elif reg.runs is _Runs.UNKNOWN:
                message = (
                    f"Whether {func.name} runs cannot be read statically (a skipif "
                    "or xfail condition, a pytestmark element, a case's entrypoint "
                    "value, or a decorator defined in this module rather than "
                    f"imported), so preflight scenario {label} is not counted as "
                    "defined."
                )
            else:
                required = {_RESULT_ASSERTION}
                if reg.scenario in _LIFETIME_SCENARIOS:
                    required.add(_LIFETIME_ASSERTION)
                missing = sorted(required - made)
                if not missing:
                    covered.add(key)
                    continue
                message = (
                    f"Preflight scenario {label} ({func.name}) does not call "
                    f"{', '.join(missing)} from {_ASSERTION_MODULE} where F016 can "
                    "see it run: make it a statement of the test body, or of a "
                    "top-level for loop over a non-empty literal, with no return, "
                    "yield, loop exit or pytest.skip/xfail/exit before it. A call "
                    "made by a helper or inside if/try/with is not counted."
                )
            findings.append(_finding(mod, func, message))

    for node in mod.src.tree.body:
        if is_test_function(node):
            visit(node, module_marks)
        elif is_test_class(node) and not any(
            isinstance(stmt, ast.FunctionDef) and stmt.name == "__init__"
            for stmt in node.body
        ):
            class_marks, _ = _decorator_marks(node.decorator_list, mod)
            inherited = (*module_marks, *class_marks, *_pytestmark(node.body, mod))
            for stmt in node.body:
                if is_test_function(stmt):
                    visit(stmt, inherited)
    return findings


def is_scenario_test_path(rel: str) -> bool:
    """A pytest-collectable module under ``tests/unit/``, where scenarios must live."""
    parts = Path(rel).parts
    return (
        len(parts) > len(SCENARIO_TEST_ROOT)
        and parts[: len(SCENARIO_TEST_ROOT)] == SCENARIO_TEST_ROOT
        and is_collectable_test_file(parts[-1])
    )


def _binds_preflight(node: ast.AST) -> bool:
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return node.name == "preflight_check"
    if not isinstance(node, ast.stmt):
        return False
    return any(
        isinstance(t, ast.Name) and t.id == "preflight_check"
        for t in binding_targets(node)
    )


def declares_preflight(reg: Registry) -> bool:
    """True when the app defines a ``preflight_check`` of its own to test.

    The scenarios drive the real handler, so an app that inherits the SDK
    default has nothing app-owned for them to exercise. Any definition counts
    — a function, or a plain or annotated binding — resolved or not: one the
    analysis could not resolve is F019's to report, and must not also make its
    scenarios optional.
    """
    return any(
        _binds_preflight(node) for src in reg.sources for node in ast.walk(src.tree)
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
                    "registered. Add a collectable test under tests/unit/ that "
                    "drives the real handler, calls assert_preflight_result, and is "
                    f'marked @pytest.mark.preflight_conformance(rule="F016", '
                    f'scenario="{scenario}"'
                    + ("" if entry == "default" else f', entrypoint="{entry}"')
                    + ")."
                ),
            )
        )
    return findings
