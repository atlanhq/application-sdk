"""F016: every required preflight scenario is registered as a collectable test.

Conformance measures that the scenarios are *defined*; whether they *pass* is
the test gate's measure, and nothing here executes them. A scenario counts
when a pytest-collected test under ``tests/unit/`` — the tier the test gate's
unit job always runs — carries
``pytest.mark.preflight_conformance(rule="F016", scenario=..., entrypoint=...)``,
runs (no skip or xfail applies), and reachably calls the contract assertion the
scenario needs, imported from ``conformance.preflight_testing``.

The registration has to be statically resolvable. Two shapes are read:

* a marker decorating the test function (``@pytest.mark.preflight_conformance(...)``);
* a marker on a ``pytest.param`` inside ``pytest.mark.parametrize``, which is
  how one test registers a scenario once per entrypoint. The parametrize may
  come from a module-level helper whose body is a single ``return`` (the
  ``entrypoint_matrix("healthy")`` shape in atlan-metabase-app), and its
  parameter list may be a comprehension over a module-level tuple of names.
  When the parametrize supplies an ``entrypoint`` argument, the case's value
  must be the entrypoint its marker claims.

Anything the reader cannot determine — a registration, a skip condition, a
``pytestmark`` element — is reported and not counted, never silently dropped:
an unreadable registration is not evidence either way.
"""

from __future__ import annotations

import ast
import builtins
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

#: Stands in for a ``pytestmark`` element the reader cannot evaluate: its effect
#: on whether the test runs is unknown, so nothing it covers is counted.
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


@dataclass
class _Module:
    src: Source
    constants: dict[str, object]
    functions: dict[str, ast.FunctionDef | ast.AsyncFunctionDef]
    pytest_names: frozenset[str]
    mark_names: frozenset[str]
    param_names: frozenset[str]
    #: Local name → the tracked ``(module, callable)`` it is bound to.
    imported_names: dict[str, tuple[str, str]]
    #: Local dotted path → the tracked module it is bound to.
    imported_modules: dict[str, str]
    #: Module-level names bound to their evaluated pytest marks.
    mark_aliases: dict[str, tuple[_Mark, ...]] = field(default_factory=dict)
    #: Binding state when a module-level definition's decorators are evaluated.
    decorator_functions: dict[
        ast.AST, dict[str, ast.FunctionDef | ast.AsyncFunctionDef]
    ] = field(default_factory=dict)
    decorator_aliases: dict[ast.AST, dict[str, tuple[_Mark, ...]]] = field(
        default_factory=dict
    )


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


def _live_blocks(node: ast.stmt) -> list[list[ast.stmt]]:
    """``_nested_blocks`` without the branches a literal test never takes."""
    if isinstance(node, ast.If):
        truth = _constant_truth(node.test)
        if truth is True:
            return [node.body]
        if truth is False:
            return [node.orelse]
    if isinstance(node, ast.While) and _constant_truth(node.test) is False:
        return [node.orelse]
    if isinstance(node, (ast.For, ast.AsyncFor)) and _statically_empty(node.iter):
        return [node.orelse]
    return _nested_blocks(node)


def _statically_empty(node: ast.expr) -> bool:
    return (isinstance(node, (ast.Tuple, ast.List, ast.Set)) and not node.elts) or (
        isinstance(node, ast.Constant)
        and isinstance(node.value, (str, bytes))
        and not node.value
    )


#: Imported callables the scanner resolves by binding, not by spelling: the
#: contract assertions, and the context managers that can absorb an exception.
_PYTEST_RAISES = ("pytest", "raises")
_CONTEXTLIB_SUPPRESS = ("contextlib", "suppress")
_ASYNCIO_RUN = ("asyncio", "run")
_TRACKED_CALLABLES = frozenset(
    {
        *((_ASSERTION_MODULE, name) for name in _ASSERTIONS),
        _PYTEST_RAISES,
        _CONTEXTLIB_SUPPRESS,
        _ASYNCIO_RUN,
    }
)
_TRACKED_MODULES = frozenset({module for module, _ in _TRACKED_CALLABLES})


def _import_bindings(
    tree: ast.Module,
) -> tuple[dict[str, tuple[str, str]], dict[str, str]]:
    """Resolve which local names reach a tracked callable or its module.

    Returns local name → ``(module, attribute)`` for directly imported
    callables, and local dotted path → module for imported modules.

    Bindings are replayed in source order, into nested module-level blocks,
    so the state is what a test sees once the module has imported: a later
    import, definition or assignment of the same name — including the root of
    a dotted ``import conformance.preflight_testing`` — replaces the binding,
    and the scanner forgets it.
    """
    names: dict[str, tuple[str, str]] = {}
    modules: dict[str, str] = {}

    def rebind(local: str, *, keep_real_paths: bool = False) -> None:
        names.pop(local, None)
        for dotted, module in list(modules.items()):
            if dotted.split(".", 1)[0] != local:
                continue
            # `import a.x` rebinds `a` to the same package, so a dotted path
            # that spells the real module (`a.b`) survives; an alias does not.
            if not (keep_real_paths and dotted == module):
                del modules[dotted]

    def visit(stmts: list[ast.stmt]) -> None:
        for node in stmts:
            if isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    local = alias.asname or alias.name
                    rebind(local)
                    if node.level or alias.name == "*" or node.module is None:
                        continue
                    if (node.module, alias.name) in _TRACKED_CALLABLES:
                        names[local] = (node.module, alias.name)
                    elif f"{node.module}.{alias.name}" in _TRACKED_MODULES:
                        modules[local] = f"{node.module}.{alias.name}"
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.asname is not None:
                        rebind(alias.asname)
                        if alias.name in _TRACKED_MODULES:
                            modules[alias.asname] = alias.name
                        continue
                    rebind(alias.name.split(".", 1)[0], keep_real_paths=True)
                    if alias.name in _TRACKED_MODULES:
                        modules[alias.name] = alias.name
            else:
                for local in _bound_names(node):
                    rebind(local)
                for block in _live_blocks(node):
                    visit(block)

    visit(tree.body)
    return names, modules


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
    imported_names, imported_modules = _import_bindings(src.tree)
    mod = _Module(
        src=src,
        constants=_module_constants(src.tree),
        functions={},
        pytest_names=modules,
        mark_names=marks,
        param_names=params,
        imported_names=imported_names,
        imported_modules=imported_modules,
    )
    _replay_module_bindings(src.tree.body, mod)
    return mod


def _replay_module_bindings(stmts: list[ast.stmt], mod: _Module) -> None:
    """Replay helper and mark-alias bindings in source order."""
    for node in stmts:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            mod.decorator_functions[node] = dict(mod.functions)
            mod.decorator_aliases[node] = dict(mod.mark_aliases)
        targets = (
            node.targets
            if isinstance(node, ast.Assign)
            else [node.target]
            if isinstance(node, ast.AnnAssign)
            else []
        )
        value = getattr(node, "value", None)
        alias = (
            targets[0].id
            if len(targets) == 1 and isinstance(targets[0], ast.Name)
            else None
        )
        mark_value: tuple[_Mark, ...] | None = None
        if alias is not None and alias != "pytestmark" and value is not None:
            try:
                mark_value = _marks(value, mod, mod.constants)
            except _Unresolved:
                if _mentions_marks(value, mod):
                    mark_value = (_Mark(_UNRESOLVED_MARK),)
                elif isinstance(value, ast.Name) and value.id in mod.mark_aliases:
                    mark_value = mod.mark_aliases[value.id]
        if isinstance(node, (ast.For, ast.AsyncFor)):
            if _statically_empty(node.iter):
                _replay_module_bindings(node.orelse, mod)
                continue
            # The body may run zero or more times. A binding in it is therefore
            # neither the old value nor any one value the body might assign.
            uncertain = _block_bound_names([*node.body, *node.orelse])
            uncertain.update(
                sub.id for sub in ast.walk(node.target) if isinstance(sub, ast.Name)
            )
            for name in uncertain:
                mod.functions.pop(name, None)
                mod.mark_aliases[name] = (_Mark(_UNRESOLVED_MARK),)
            _replay_module_bindings(node.orelse, mod)
            continue
        bound = set(_bound_names(node))
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            bound |= {
                (a.asname or a.name).split(".", 1)[0]
                for a in node.names
                if a.name != "*"
            }
        for name in bound:
            mod.functions.pop(name, None)
            mod.mark_aliases.pop(name, None)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            mod.functions[node.name] = node
            continue
        if alias is not None and alias != "pytestmark" and mark_value is not None:
            mod.mark_aliases[alias] = mark_value
        for block in _live_blocks(node):
            _replay_module_bindings(block, mod)


def _block_bound_names(stmts: list[ast.stmt]) -> set[str]:
    names: set[str] = set()
    for stmt in stmts:
        names.update(_bound_names(stmt))
        if not isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            for block in _nested_blocks(stmt):
                names.update(_block_bound_names(block))
    return names


def _mentions_marks(
    node: ast.AST,
    mod: _Module,
    seen: frozenset[str] = frozenset(),
    *,
    functions: Mapping[str, ast.FunctionDef | ast.AsyncFunctionDef] | None = None,
    aliases: Mapping[str, tuple[_Mark, ...]] | None = None,
) -> bool:
    """True when reachable *node* may evaluate a mark expression or alias."""
    functions = functions if functions is not None else mod.functions
    aliases = aliases if aliases is not None else mod.mark_aliases
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return any(
            _mentions_marks(
                stmt,
                mod,
                seen | {node.name},
                functions=functions,
                aliases=aliases,
            )
            for stmt in node.body
        )
    if isinstance(node, ast.Return) and isinstance(node.value, ast.Lambda):
        return _mentions_marks(
            node.value.body,
            mod,
            seen,
            functions=functions,
            aliases=aliases,
        )
    if isinstance(node, ast.Lambda):
        return False
    if isinstance(node, ast.If):
        truth = _constant_truth(node.test)
        branches = (
            node.body
            if truth is True
            else node.orelse
            if truth is False
            else [*node.body, *node.orelse]
        )
        return _mentions_marks(
            node.test, mod, seen, functions=functions, aliases=aliases
        ) or any(
            _mentions_marks(stmt, mod, seen, functions=functions, aliases=aliases)
            for stmt in branches
        )
    if isinstance(node, ast.While) and _constant_truth(node.test) is False:
        return _mentions_marks(
            node.test, mod, seen, functions=functions, aliases=aliases
        ) or any(
            _mentions_marks(stmt, mod, seen, functions=functions, aliases=aliases)
            for stmt in node.orelse
        )
    if isinstance(node, (ast.For, ast.AsyncFor)) and _statically_empty(node.iter):
        return _mentions_marks(
            node.iter, mod, seen, functions=functions, aliases=aliases
        ) or any(
            _mentions_marks(stmt, mod, seen, functions=functions, aliases=aliases)
            for stmt in node.orelse
        )
    if isinstance(node, (ast.IfExp, ast.BoolOp)):
        # A statically dead arm or short-circuited operand never evaluates.
        return any(
            _mentions_marks(part, mod, seen, functions=functions, aliases=aliases)
            for part in _live_operands(node)
        )
    if isinstance(node, ast.GeneratorExp):
        return bool(node.generators) and _mentions_marks(
            node.generators[0].iter,
            mod,
            seen,
            functions=functions,
            aliases=aliases,
        )
    if isinstance(node, ast.Name) and node.id in aliases:
        return True
    if isinstance(node, ast.Attribute) and _mark_name(node, mod) is not None:
        return True
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in functions
        and node.func.id not in seen
        and _mentions_marks(
            functions[node.func.id],
            mod,
            seen | {node.func.id},
            functions=functions,
            aliases=aliases,
        )
    ):
        return True
    return any(
        _mentions_marks(child, mod, seen, functions=functions, aliases=aliases)
        for child in ast.iter_child_nodes(node)
        if not isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
    )


def _live_operands(node: ast.IfExp | ast.BoolOp) -> list[ast.expr]:
    """The parts of a conditional or boolean expression that may evaluate."""
    if isinstance(node, ast.IfExp):
        truth = _constant_truth(node.test)
        if truth is True:
            return [node.test, node.body]
        if truth is False:
            return [node.test, node.orelse]
        return [node.test, node.body, node.orelse]
    live: list[ast.expr] = []
    for value in node.values:
        live.append(value)
        truth = _constant_truth(value)
        if (isinstance(node.op, ast.And) and truth is False) or (
            isinstance(node.op, ast.Or) and truth is True
        ):
            break
    return live


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
    node: ast.Call,
    mod: _Module,
    env: Mapping[str, object],
    depth: int,
    *,
    functions: Mapping[str, ast.FunctionDef | ast.AsyncFunctionDef] | None = None,
    aliases: Mapping[str, tuple[_Mark, ...]] | None = None,
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
            marks = (
                _marks(
                    marks_kw,
                    mod,
                    bound,
                    depth,
                    functions=functions,
                    aliases=aliases,
                )
                if marks_kw
                else ()
            )
            if len(elt.args) == len(argnames):
                values = {n: _value(v, bound) for n, v in zip(argnames, elt.args)}
            else:
                values = dict.fromkeys(argnames, UNKNOWN)
            params.append(_Param(marks, values))
        else:
            params.append(_Param((), _case_values(elt, argnames, bound)))
    return _Mark("parametrize", argnames=argnames, params=tuple(params))


def _marks(
    node: ast.expr,
    mod: _Module,
    env: Mapping[str, object],
    depth: int = 0,
    *,
    functions: Mapping[str, ast.FunctionDef | ast.AsyncFunctionDef] | None = None,
    aliases: Mapping[str, tuple[_Mark, ...]] | None = None,
) -> tuple[_Mark, ...]:
    """Evaluate a decorator or ``marks=`` expression to the marks it applies."""
    if depth > _MAX_DEPTH:
        raise _Unresolved("helper nesting")
    if isinstance(node, (ast.List, ast.Tuple)):
        return tuple(
            mark
            for elt in node.elts
            for mark in _marks(
                elt,
                mod,
                env,
                depth,
                functions=functions,
                aliases=aliases,
            )
        )
    name = _mark_name(node, mod)
    if name is not None:
        return (_Mark(name),)
    aliases = aliases if aliases is not None else mod.mark_aliases
    functions = functions if functions is not None else mod.functions
    if isinstance(node, ast.Name) and node.id in aliases:
        return aliases[node.id]
    if (
        isinstance(node, ast.IfExp)
        and (truth := _constant_truth(node.test)) is not None
    ):
        return _marks(
            node.body if truth else node.orelse,
            mod,
            env,
            depth,
            functions=functions,
            aliases=aliases,
        )
    if not isinstance(node, ast.Call):
        raise _Unresolved(ast.unparse(node))
    if isinstance(node.func, ast.Lambda):
        return _applied_lambda_marks(
            node.func, node, mod, env, depth, functions=functions, aliases=aliases
        )
    name = _mark_name(node.func, mod)
    if name == "parametrize":
        return (
            _parametrize(
                node,
                mod,
                env,
                depth,
                functions=functions,
                aliases=aliases,
            ),
        )
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
    if isinstance(node.func, ast.Name) and node.func.id in functions:
        return _helper_marks(
            functions[node.func.id],
            node,
            mod,
            env,
            depth,
            functions=functions,
            aliases=aliases,
        )
    raise _Unresolved(ast.unparse(node))


def _applied_lambda_marks(
    func: ast.Lambda,
    call: ast.Call,
    mod: _Module,
    env: Mapping[str, object],
    depth: int,
    *,
    functions: Mapping[str, ast.FunctionDef | ast.AsyncFunctionDef],
    aliases: Mapping[str, tuple[_Mark, ...]],
) -> tuple[_Mark, ...]:
    """The marks a directly invoked lambda returns, its arguments bound.

    ``(lambda mark: mark)(pytest.mark.skip(...))`` returns the skip it was
    passed. A parameter bound to a mark expression reads as that mark; any
    other parameter shadows the module name it spells.
    """
    args = func.args
    params = [a.arg for a in (*args.posonlyargs, *args.args)]
    if (
        call.keywords
        or len(call.args) != len(params)
        or args.vararg
        or args.kwarg
        or args.kwonlyargs
        or any(isinstance(arg, ast.Starred) for arg in call.args)
    ):
        raise _Unresolved(ast.unparse(call))
    bound_aliases = {k: v for k, v in aliases.items() if k not in params}
    bound_functions = {k: v for k, v in functions.items() if k not in params}
    for param, arg in zip(params, call.args):
        try:
            bound_aliases[param] = _marks(
                arg, mod, env, depth, functions=functions, aliases=aliases
            )
        except _Unresolved:
            if _mentions_marks(arg, mod, functions=functions, aliases=aliases):
                bound_aliases[param] = (_Mark(_UNRESOLVED_MARK),)
    return _marks(
        func.body,
        mod,
        env,
        depth + 1,
        functions=bound_functions,
        aliases=bound_aliases,
    )


def _helper_marks(
    helper: ast.FunctionDef | ast.AsyncFunctionDef,
    call: ast.Call,
    mod: _Module,
    env: Mapping[str, object],
    depth: int,
    *,
    functions: Mapping[str, ast.FunctionDef | ast.AsyncFunctionDef],
    aliases: Mapping[str, tuple[_Mark, ...]],
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
    return _marks(
        body[0].value,
        mod,
        bound,
        depth + 1,
        functions=functions,
        aliases=aliases,
    )


def _names_marker(
    node: ast.AST,
    mod: _Module,
    functions: Mapping[str, ast.FunctionDef | ast.AsyncFunctionDef] | None = None,
    seen: frozenset[str] = frozenset(),
) -> bool:
    """True when *node*, or a reachable local helper it calls, names the marker."""
    functions = functions if functions is not None else mod.functions
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return any(
            _names_marker(stmt, mod, functions, seen | {node.name})
            for stmt in node.body
        )
    if isinstance(node, (ast.Lambda, ast.GeneratorExp)):
        return False
    if isinstance(node, ast.If):
        truth = _constant_truth(node.test)
        branches = (
            node.body
            if truth is True
            else node.orelse
            if truth is False
            else [*node.body, *node.orelse]
        )
        return _names_marker(node.test, mod, functions, seen) or any(
            _names_marker(stmt, mod, functions, seen) for stmt in branches
        )
    if isinstance(node, ast.While) and _constant_truth(node.test) is False:
        return _names_marker(node.test, mod, functions, seen) or any(
            _names_marker(stmt, mod, functions, seen) for stmt in node.orelse
        )
    if isinstance(node, (ast.For, ast.AsyncFor)) and _statically_empty(node.iter):
        return _names_marker(node.iter, mod, functions, seen) or any(
            _names_marker(stmt, mod, functions, seen) for stmt in node.orelse
        )
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == MARKER
    ):
        return True
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
        name = node.func.id
        if name in functions and name not in seen:
            return _names_marker(functions[name], mod, functions, seen | {name})
    return any(
        _names_marker(child, mod, functions, seen)
        for child in ast.iter_child_nodes(node)
        if not isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda))
    )


def _lenient_marks(
    node: ast.expr,
    mod: _Module,
    *,
    functions: Mapping[str, ast.FunctionDef | ast.AsyncFunctionDef] | None = None,
    aliases: Mapping[str, tuple[_Mark, ...]] | None = None,
) -> tuple[_Mark, ...]:
    """Marks from a ``pytestmark`` value, element by element.

    A resolvable element keeps its effect; an unreadable one becomes
    ``_UNRESOLVED_MARK`` rather than taking its siblings (a readable
    ``skip`` among them) down with it.
    """
    elements = node.elts if isinstance(node, (ast.List, ast.Tuple)) else [node]
    marks: list[_Mark] = []
    for elt in elements:
        try:
            marks.extend(
                _marks(elt, mod, mod.constants, functions=functions, aliases=aliases)
            )
        except _Unresolved:
            marks.append(_Mark(_UNRESOLVED_MARK))
    return tuple(marks)


def _pytestmark(body: list[ast.stmt], mod: _Module) -> tuple[_Mark, ...]:
    """The marks a module or class body applies through ``pytestmark``.

    Any binding counts — a plain, chained or annotated assignment — and the
    last one wins, as it does at import time.
    """
    marks: tuple[_Mark, ...] = ()
    functions = mod.functions
    aliases = mod.mark_aliases
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
            marks = (
                *marks,
                *_lenient_marks(stmt.value, mod, functions=functions, aliases=aliases),
            )
            continue
        if value is not None:
            marks = _lenient_marks(value, mod, functions=functions, aliases=aliases)
    return marks


def _is_mark_expression(
    node: ast.expr,
    mod: _Module,
    *,
    functions: Mapping[str, ast.FunctionDef | ast.AsyncFunctionDef] | None = None,
    aliases: Mapping[str, tuple[_Mark, ...]] | None = None,
) -> bool:
    """True when *node* may apply pytest marks.

    That is a ``pytest.mark.*`` expression or a mark alias, or a same-module
    function — called as a factory or used bare — whose body builds one. An
    ordinary decorator factory (``def identity(): return lambda fn: fn``)
    applies no marks, so it is ignored rather than made unknown.
    """
    functions = functions if functions is not None else mod.functions
    aliases = aliases if aliases is not None else mod.mark_aliases
    if isinstance(node, ast.Lambda):
        return _mentions_marks(node.body, mod, functions=functions, aliases=aliases)
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Lambda):
        # Its body runs now, and its arguments are what that body may return.
        return _mentions_marks(
            node.func.body, mod, functions=functions, aliases=aliases
        ) or _mentions_marks(node, mod, functions=functions, aliases=aliases)
    if _mentions_marks(node, mod, functions=functions, aliases=aliases):
        return True
    target = node.func if isinstance(node, ast.Call) else node
    return (
        isinstance(target, ast.Name)
        and target.id in functions
        and _mentions_marks(
            functions[target.id], mod, functions=functions, aliases=aliases
        )
    )


def _decorator_marks(
    decorators: list[ast.expr], mod: _Module, owner: ast.AST | None = None
) -> tuple[tuple[_Mark, ...], list[ast.expr]]:
    """Resolved marks, and the decorators naming the marker that did not resolve.

    An unreadable decorator that applies marks — an unresolved parametrize, a
    ``skipif`` built at import time — is kept as ``_UNRESOLVED_MARK``: whether
    pytest collects a runnable case is then unknown, and nothing it covers is
    credited. Only decorators that apply no marks (``respx.mock``) are dropped.
    """
    marks: list[_Mark] = []
    unresolved: list[ast.expr] = []
    functions = (
        mod.decorator_functions.get(owner, mod.functions)
        if owner is not None
        else mod.functions
    )
    aliases = (
        mod.decorator_aliases.get(owner, mod.mark_aliases)
        if owner is not None
        else mod.mark_aliases
    )
    for dec in decorators:
        try:
            marks.extend(
                _marks(dec, mod, mod.constants, functions=functions, aliases=aliases)
            )
        except _Unresolved:
            if _names_marker(dec, mod, functions):
                unresolved.append(dec)
            elif _is_mark_expression(dec, mod, functions=functions, aliases=aliases):
                marks.append(_Mark(_UNRESOLVED_MARK))
    return tuple(marks), unresolved


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


# --- which assertions a test reachably makes ------------------------------------


def _constant_truth(test: ast.expr) -> bool | None:
    if isinstance(test, ast.Constant):
        return bool(test.value)
    return None


#: How a block can end. A block's flow is the set of ways it may end: it is
#: definitely over when ``FALLS`` is not in the set, whatever mix of the other
#: exits remains. An exception exit carries what is known of its type:
#:
#: * ``raises:<Name>`` — a ``raise`` of a spelled class;
#: * ``CALL_RAISES`` — a call may raise any exception at all;
#: * ``_NON_EXCEPTION`` — what an ``except Exception`` lets past a call:
#:   ``SystemExit`` and the rest of ``BaseException`` outside ``Exception``;
#: * ``_UNKNOWN_RAISE`` — a re-raise or a raised value of unreadable type.
#:
#: A handler or suppressor is credited with an explicit raise only when the
#: types definitely match; a call exception may be anything, so any typed
#: handler may catch it.
FALLS, RETURNS, JUMPS = "falls", "returns", "jumps"
_RAISE = "raises:"
CALL_RAISES = _RAISE + "*"
_NON_EXCEPTION = _RAISE + "!Exception"
_UNKNOWN_RAISE = _RAISE + "?"
_Flow = frozenset[str]
_FALL: _Flow = frozenset({FALLS})


class _Catch(Enum):
    MUST = "must"
    MAY = "may"
    NO = "no"


@dataclass(frozen=True)
class _Scope:
    """What a function body's names resolve to."""

    mod: _Module
    #: Names the function binds itself, hiding the module-level binding.
    shadowed: frozenset[str]


def _expression(node: ast.AST, out: list[ast.AST]) -> None:
    """Collect the nodes evaluating *node* runs, leaving out deferred parts.

    A lambda body and a generator expression's element run only if something
    later calls or consumes them, and a statically short-circuited operand
    never runs; none of them counts.
    """
    if isinstance(node, ast.Lambda):
        return
    if isinstance(node, ast.GeneratorExp):
        _expression(node.generators[0].iter, out)
        return
    if isinstance(node, (ast.BoolOp, ast.IfExp)):
        for part in _live_operands(node):
            _expression(part, out)
        return
    out.append(node)
    for child in ast.iter_child_nodes(node):
        _expression(child, out)


def _call_raises(out: list[ast.AST], start: int) -> _Flow:
    """A call in the evaluated nodes may add an exception exit."""
    return (
        frozenset({CALL_RAISES})
        if any(isinstance(node, ast.Call) for node in out[start:])
        else frozenset()
    )


def _exceptions(flow: _Flow) -> set[str]:
    return {exit for exit in flow if exit.startswith(_RAISE)}


def _raised(exc: ast.expr | None) -> str:
    """The exception exit a ``raise`` statement takes."""
    target = exc.func if isinstance(exc, ast.Call) else exc
    if isinstance(target, (ast.Name, ast.Attribute)):
        return _RAISE + ast.unparse(target)
    return _UNKNOWN_RAISE


def _builtin_exception(name: str) -> type[BaseException] | None:
    value = getattr(builtins, name, None)
    if isinstance(value, type) and issubclass(value, BaseException):
        return value
    return None


def _catches(exit: str, accepted: tuple[str, ...] | None) -> _Catch:
    """Whether a handler for *accepted* types catches exception *exit*.

    ``None`` is a bare ``except:``. A type the scanner cannot place in the
    builtin hierarchy matches an explicit raise only by identical spelling.
    """
    if accepted is None or "BaseException" in accepted:
        return _Catch.MUST
    if exit == CALL_RAISES:
        return _Catch.MAY if accepted else _Catch.NO
    if exit == _NON_EXCEPTION:
        outside = [
            name
            for name in accepted
            if (cls := _builtin_exception(name)) is None
            or not issubclass(cls, Exception)
        ]
        return _Catch.MAY if outside else _Catch.NO
    if exit == _UNKNOWN_RAISE:
        return _Catch.NO
    kind = exit.removeprefix(_RAISE)
    raised = _builtin_exception(kind)
    for name in accepted:
        cls = _builtin_exception(name)
        if name == kind or (raised and cls and issubclass(raised, cls)):
            return _Catch.MUST
    return _Catch.NO


def _escapes(exit: str, accepted: tuple[str, ...] | None) -> str | None:
    """The exception exit left after the handler, or ``None`` when caught."""
    if _catches(exit, accepted) is _Catch.MUST:
        return None
    if exit == CALL_RAISES and accepted and "Exception" in accepted:
        return _NON_EXCEPTION
    return exit


def _handle(
    pending: set[str], accepted: tuple[str, ...] | None
) -> tuple[bool, set[str]]:
    """Whether a handler may run for *pending*, and what escapes it."""
    runs = any(_catches(exit, accepted) is not _Catch.NO for exit in pending)
    escaped = {e for exit in pending if (e := _escapes(exit, accepted)) is not None}
    return runs, escaped


def _handler_types(node: ast.expr | None) -> tuple[str, ...] | None:
    if node is None:
        return None
    elts = node.elts if isinstance(node, ast.Tuple) else [node]
    return tuple(ast.unparse(elt) for elt in elts)


def _resolve(func: ast.expr, scope: _Scope) -> tuple[str, str] | None:
    """The tracked ``(module, callable)`` *func* is bound to, if any."""
    if isinstance(func, ast.Name):
        if func.id in scope.shadowed:
            return None
        return scope.mod.imported_names.get(func.id)
    if isinstance(func, ast.Attribute):
        dotted = ast.unparse(func.value)
        # A local rebinding of the dotted path's root shadows the whole path.
        if dotted.split(".", 1)[0] in scope.shadowed:
            return None
        module = scope.mod.imported_modules.get(dotted)
        return (module, func.attr) if module is not None else None
    return None


@dataclass(frozen=True)
class _Suppressor:
    #: The exception types the context absorbs.
    accepted: tuple[str, ...]
    #: ``pytest.raises`` fails the test when its block exits normally.
    fails_on_exit: bool


def _suppressor(item: ast.withitem, scope: _Scope) -> _Suppressor | None:
    """A ``pytest.raises`` / ``contextlib.suppress`` context, resolved.

    ``None`` when the context is not one of them: an arbitrary context
    manager is not assumed to swallow anything.
    """
    expr = item.context_expr
    if not isinstance(expr, ast.Call):
        return None
    target = _resolve(expr.func, scope)
    if target == _PYTEST_RAISES:
        expected = expr.args[0] if expr.args else None
        for kw in expr.keywords:
            if kw.arg == "expected_exception":
                expected = kw.value
        accepted = () if expected is None else _handler_types(expected) or ()
        return _Suppressor(accepted, fails_on_exit=True)
    if target == _CONTEXTLIB_SUPPRESS:
        return _Suppressor(
            tuple(ast.unparse(arg) for arg in expr.args), fails_on_exit=False
        )
    return None


def _block(stmts: list[ast.stmt], out: list[ast.AST], scope: _Scope) -> _Flow:
    """Collect the nodes a block can execute; return how it can end.

    Dead branches, nested scopes, deferred expressions and everything after
    a statement that cannot fall through are left out.
    """
    exits: set[str] = set()
    for stmt in stmts:
        flow = _statement(stmt, out, scope)
        exits |= flow - _FALL
        if FALLS not in flow:
            return frozenset(exits)
    return frozenset(exits | _FALL)


def _loop(body: _Flow, orelse: _Flow, *, infinite: bool) -> _Flow:
    """A loop ends the enclosing block only if it cannot finish or break."""
    if infinite and JUMPS not in body:
        # Only a return or raise leaves `while True:` without a break.
        return body - _FALL
    return (body - {JUMPS}) | orelse | _FALL


def _statement(stmt: ast.stmt, out: list[ast.AST], scope: _Scope) -> _Flow:
    if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        start = len(out)
        for dec in stmt.decorator_list:
            _expression(dec, out)
        return _FALL | _call_raises(out, start)
    if isinstance(stmt, ast.Return):
        start = len(out)
        if stmt.value is not None:
            _expression(stmt.value, out)
        return frozenset({RETURNS}) | _call_raises(out, start)
    if isinstance(stmt, ast.Raise):
        # Constructing the exception is not a separate exit: the raise's own
        # type is what a handler has to match.
        if stmt.exc is not None:
            _expression(stmt.exc, out)
        return frozenset({_raised(stmt.exc)})
    if isinstance(stmt, (ast.Break, ast.Continue)):
        return frozenset({JUMPS})
    if isinstance(stmt, ast.Assert):
        # The message is evaluated only when the assertion fails.
        start = len(out)
        _expression(stmt.test, out)
        flow = (
            frozenset({_RAISE + "AssertionError"})
            if _constant_truth(stmt.test) is False
            else _FALL
        )
        return flow | _call_raises(out, start)
    if isinstance(stmt, ast.If):
        start = len(out)
        _expression(stmt.test, out)
        test_flow = _call_raises(out, start)
        truth = _constant_truth(stmt.test)
        if truth is True:
            return _block(stmt.body, out, scope) | test_flow
        if truth is False:
            return _block(stmt.orelse, out, scope) | test_flow
        # Either branch may run: the if falls through only if one of them does.
        body = _block(stmt.body, out, scope)
        return body | _block(stmt.orelse, out, scope) | test_flow
    if isinstance(stmt, ast.While):
        start = len(out)
        _expression(stmt.test, out)
        test_flow = _call_raises(out, start)
        truth = _constant_truth(stmt.test)
        if truth is False:
            return _block(stmt.orelse, out, scope) | test_flow
        body = _block(stmt.body, out, scope)
        orelse = _block(stmt.orelse, out, scope) if truth is not True else frozenset()
        return _loop(body, orelse, infinite=truth is True) | test_flow
    if isinstance(stmt, (ast.For, ast.AsyncFor)):
        start = len(out)
        _expression(stmt.iter, out)
        iter_flow = _call_raises(out, start)
        if _statically_empty(stmt.iter):
            return _block(stmt.orelse, out, scope) | iter_flow
        body = _block(stmt.body, out, scope)  # may run zero times
        orelse = _block(stmt.orelse, out, scope)
        return _loop(body, orelse, infinite=False) | iter_flow
    if isinstance(stmt, (ast.With, ast.AsyncWith)):
        start = len(out)
        for item in stmt.items:
            _expression(item.context_expr, out)
        context_flow = _call_raises(out, start)
        body = _block(stmt.body, out, scope)
        # Only a known suppressor turns an exception it accepts into
        # fallthrough; the innermost context sees the body's exit first.
        for item in reversed(stmt.items):
            suppressor = _suppressor(item, scope)
            if suppressor is None:
                continue
            raised = _exceptions(body)
            absorbs, escaped = _handle(raised, suppressor.accepted)
            if suppressor.fails_on_exit and FALLS in body:
                # A normal exit is not a pass: pytest.raises raises Failed.
                body = (body - _FALL) | {_RAISE + "Failed"}
            if absorbs:
                body = (body - raised) | escaped | _FALL
        return body | context_flow
    if isinstance(stmt, ast.Try | ast.TryStar):
        body = _block(stmt.body, out, scope)
        pending = _exceptions(body)
        result = body - pending - _FALL
        if FALLS in body:
            result |= _block(stmt.orelse, out, scope)
        # Handlers are tried in order: each runs only for an exception that
        # may still reach it, and what it definitely catches stops there.
        for handler in stmt.handlers:
            runs, pending = _handle(pending, _handler_types(handler.type))
            if runs:
                result |= _block(handler.body, out, scope)
        result |= pending
        final = _block(stmt.finalbody, out, scope)
        if not stmt.finalbody:
            return frozenset(result)
        final_exits = final - {FALLS}
        return frozenset(final_exits | (result if FALLS in final else set()))
    if isinstance(stmt, ast.Match):
        start = len(out)
        _expression(stmt.subject, out)
        subject_flow = _call_raises(out, start)
        flows = [_block(case.body, out, scope) for case in stmt.cases]
        return frozenset().union(*flows) | _FALL | subject_flow
    start = len(out)
    for child in ast.iter_child_nodes(stmt):
        _expression(child, out)
    return _FALL | _call_raises(out, start)


def _calls(stmts: list[ast.stmt], scope: _Scope) -> list[ast.Call]:
    return [node for node in _reached(stmts, scope) if isinstance(node, ast.Call)]


def _reached(stmts: list[ast.stmt], scope: _Scope) -> list[ast.AST]:
    out: list[ast.AST] = []
    _block(stmts, out, scope)
    return out


def _driven(nodes: list[ast.AST], scope: _Scope) -> set[int]:
    """Ids of the calls whose coroutine is run: awaited, or passed to ``asyncio.run``."""
    driven: set[int] = set()
    for node in nodes:
        if isinstance(node, ast.Await) and isinstance(node.value, ast.Call):
            driven.add(id(node.value))
        elif (
            isinstance(node, ast.Call)
            and node.args
            and isinstance(node.args[0], ast.Call)
            and _resolve(node.func, scope) == _ASYNCIO_RUN
        ):
            driven.add(id(node.args[0]))
    return driven


def _runs_body(
    helper: ast.FunctionDef | ast.AsyncFunctionDef, call: ast.Call, driven: set[int]
) -> bool:
    """Whether *call* runs *helper*'s body.

    A generator's body waits for a consumer, and a coroutine's for an
    ``await`` (or ``asyncio.run``); a plain function runs when called.
    """
    if any(isinstance(node, (ast.Yield, ast.YieldFrom)) for node in _own_nodes(helper)):
        return False
    return not isinstance(helper, ast.AsyncFunctionDef) or id(call) in driven


def _own_nodes(func: ast.FunctionDef | ast.AsyncFunctionDef) -> Iterator[ast.AST]:
    """The nodes of *func*'s own scope, not those of nested functions."""
    stack: list[ast.AST] = list(func.body)
    while stack:
        node = stack.pop()
        yield node
        stack.extend(
            child
            for child in ast.iter_child_nodes(node)
            if not isinstance(
                child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)
            )
        )


def _assertion(call: ast.Call, scope: _Scope) -> str | None:
    """The contract assertion *call* invokes, resolved through its import."""
    target = _resolve(call.func, scope)
    if target is not None and target[0] == _ASSERTION_MODULE:
        return target[1] if target[1] in _ASSERTIONS else None
    return None


def _parameters(func: ast.FunctionDef | ast.AsyncFunctionDef) -> frozenset[str]:
    """Names local to *func* — its parameters and anything its body binds.

    A fixture parameter or a local rebinding named ``assert_preflight_result``
    is not the contract assertion, whatever the module imported.
    """
    args = func.args
    names = {
        a.arg
        for a in (
            *args.posonlyargs,
            *args.args,
            *args.kwonlyargs,
            args.vararg,
            args.kwarg,
        )
        if a is not None
    }
    for stmt in func.body:
        for node in ast.walk(stmt):
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
                names.add(node.id)
            elif isinstance(
                node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
            ):
                names.add(node.name)
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                names.update((a.asname or a.name).split(".")[0] for a in node.names)
    return frozenset(names)


def _assertions(func: ast.FunctionDef | ast.AsyncFunctionDef, mod: _Module) -> set[str]:
    """Contract assertions the test reachably calls, directly or through a
    same-module helper it calls directly and whose body that call runs."""
    made: set[str] = set()
    scope = _Scope(mod, _parameters(func))
    reached = _reached(func.body, scope)
    driven = _driven(reached, scope)
    for call in (node for node in reached if isinstance(node, ast.Call)):
        if (name := _assertion(call, scope)) is not None:
            made.add(name)
        elif (
            isinstance(call.func, ast.Name)
            and call.func.id not in scope.shadowed
            and call.func.id in mod.functions
        ):
            helper = mod.functions[call.func.id]
            if not _runs_body(helper, call, driven):
                continue
            inner = _Scope(mod, _parameters(helper))
            made |= {
                name
                for helper_call in _calls(helper.body, inner)
                if (name := _assertion(helper_call, inner)) is not None
            }
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
        own, unresolved = _decorator_marks(func.decorator_list, mod, owner=func)
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
                    "or xfail condition, a pytestmark element, or a case's "
                    f"entrypoint value is not a literal), so preflight scenario "
                    f"{label} is not counted as defined."
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
                    f"Preflight scenario {label} ({func.name}) never reachably calls "
                    f"{', '.join(missing)} from {_ASSERTION_MODULE}; registration "
                    "without the contract assertion does not define the scenario."
                )
            findings.append(_finding(mod, func, message))

    for node in mod.src.tree.body:
        if is_test_function(node):
            visit(node, module_marks)
        elif is_test_class(node) and not any(
            isinstance(stmt, ast.FunctionDef) and stmt.name == "__init__"
            for stmt in node.body
        ):
            class_marks, _ = _decorator_marks(node.decorator_list, mod, owner=node)
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
