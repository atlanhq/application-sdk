"""O001 OrjsonOverStdlibJson — flag stdlib ``json.dumps``/``json.loads`` calls.

``orjson`` is a core SDK dependency and ~10x faster than stdlib ``json``; on hot
paths it is the recommended serialiser.  Only ``json.dumps`` / ``json.loads``
call sites are flagged, and only when ``json`` resolves to the stdlib module.
"""

from __future__ import annotations

import ast

from conformance.suite.checks._ast_common import (
    _IgnoreDirective,
    collect_import_origins,
    make_finding,
)
from conformance.suite.schema.findings import Finding

# json.<attr> call sites worth flagging.  orjson provides drop-in equivalents
# only for these two; json.dump/json.load (file-object APIs) have no orjson
# counterpart and are intentionally out of scope.
_JSON_CALL_ATTRS: frozenset[str] = frozenset({"dumps", "loads"})


def _collect_json_bindings(tree: ast.Module) -> tuple[frozenset[str], frozenset[str]]:
    """Return ``(module_names, func_names)`` for stdlib ``json`` bindings.

    * ``module_names`` — local names bound to the stdlib ``json`` module via
      ``import json`` / ``import json as x`` (so ``x.dumps(...)`` is flaggable).
    * ``func_names`` — local names bound to ``json.dumps`` / ``json.loads`` via
      ``from json import dumps`` / ``from json import loads as y``.

    Walks the whole module (not just top-level) so function-local imports are
    covered.  Reassignment of a bound name is not tracked — consistent with the
    E-series import resolution and acceptably low false-positive in practice.
    """
    module_names: set[str] = set()
    func_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "json":
                    module_names.add(alias.asname or "json")
        elif isinstance(node, ast.ImportFrom):
            if node.module != "json":
                continue
            for alias in node.names:
                if alias.name in _JSON_CALL_ATTRS:
                    func_names.add(alias.asname or alias.name)
    return frozenset(module_names), frozenset(func_names)


def _collect_shadowed_names(tree: ast.Module) -> frozenset[str]:
    """Return names the module binds by anything other than an import.

    A parameter, assignment, loop target, ``with ... as``, ``except ... as``,
    ``def``/``class`` name or match capture all rebind a name, so a call
    through it cannot be trusted to reach the imported module.  Collected
    module-wide: an exemption is withheld whenever the name is rebound
    anywhere, which keeps the finding (fails closed) rather than resolving
    per scope.
    """
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            names.add(node.id)
        elif isinstance(node, ast.arg):
            names.add(node.arg)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            names.add(node.name)
        elif isinstance(node, (ast.MatchAs, ast.MatchStar)) and node.name:
            names.add(node.name)
        elif isinstance(node, ast.MatchMapping) and node.rest:
            names.add(node.rest)
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            names.update(node.names)
    return frozenset(names)


def _is_orjson_dumps_call(
    call: ast.Call, origins: dict[str, str], shadowed: frozenset[str]
) -> bool:
    """Return whether ``call`` resolves to ``orjson.dumps`` through an import.

    Resolution goes through the shared :func:`collect_import_origins`, so
    ``import orjson as oj`` / ``oj.dumps(...)`` and
    ``from orjson import dumps as fast_dumps`` / ``fast_dumps(...)`` both
    resolve.  A name that is also rebound by a non-import statement is never
    trusted (see :func:`_collect_shadowed_names`).
    """
    func = call.func
    if isinstance(func, ast.Name):
        return func.id not in shadowed and origins.get(func.id) == "orjson.dumps"
    return (
        isinstance(func, ast.Attribute)
        and func.attr == "dumps"
        and isinstance(func.value, ast.Name)
        and func.value.id not in shadowed
        and origins.get(func.value.id) == "orjson"
    )


def _bound_call_attr(
    call: ast.Call,
    module_names: frozenset[str],
    func_names: frozenset[str],
) -> str | None:
    """Return the called JSON API attr if the call uses a collected binding."""
    func = call.func
    if (
        isinstance(func, ast.Attribute)
        and func.attr in _JSON_CALL_ATTRS
        and isinstance(func.value, ast.Name)
        and func.value.id in module_names
    ):
        return func.attr
    if isinstance(func, ast.Name) and func.id in func_names:
        return func.id
    return None


def _call_object_argument(call: ast.Call) -> ast.expr | None:
    """Return the object expression passed to stdlib ``json.dumps``."""
    if call.args:
        return call.args[0]
    return next(
        (keyword.value for keyword in call.keywords if keyword.arg == "obj"), None
    )


def _iter_calls(nodes: list[ast.AST]):
    """Yield calls in a body without descending into deferred function bodies."""
    pending = list(reversed(nodes))
    while pending:
        node = pending.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue
        if isinstance(node, ast.Call):
            yield node
        pending.extend(reversed(list(ast.iter_child_nodes(node))))


def _unconditional_children(node: ast.AST) -> list[ast.AST]:
    """Return the children of ``node`` that always run when ``node`` runs.

    Branch bodies (``if``/``else``, loop bodies, ``match`` cases), the
    non-first operands of ``and``/``or``, both arms of a conditional
    expression, comprehensions, nested ``try`` blocks and deferred bodies
    (``def``/``lambda``/``class``) are not on the unconditional path.
    """
    if isinstance(
        node,
        (
            ast.FunctionDef,
            ast.AsyncFunctionDef,
            ast.Lambda,
            ast.ClassDef,
            ast.Try,
            ast.TryStar,
            ast.ListComp,
            ast.SetComp,
            ast.DictComp,
            ast.GeneratorExp,
        ),
    ):
        return []
    if isinstance(node, (ast.If, ast.While, ast.IfExp)):
        return [node.test]
    if isinstance(node, (ast.For, ast.AsyncFor)):
        return [node.iter]
    if isinstance(node, ast.Match):
        return [node.subject]
    if isinstance(node, ast.BoolOp):
        return [node.values[0]]
    return list(ast.iter_child_nodes(node))


def _iter_unconditional_calls(node: ast.AST):
    """Yield calls in ``node`` that run whenever ``node`` itself runs."""
    pending: list[ast.AST] = [node]
    while pending:
        current = pending.pop()
        if isinstance(current, ast.Call):
            yield current
        pending.extend(_unconditional_children(current))


def _contains_raise(node: ast.AST) -> bool:
    """Return whether a statement holds an explicit ``raise`` (outside deferred bodies)."""
    pending: list[ast.AST] = [node]
    while pending:
        current = pending.pop()
        if isinstance(current, ast.Raise):
            return True
        if isinstance(
            current, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)
        ):
            continue
        pending.extend(ast.iter_child_nodes(current))
    return False


def _attempted_orjson_arguments(
    try_body: list[ast.stmt], origins: dict[str, str], shadowed: frozenset[str]
) -> set[str]:
    """Return argument dumps of the ``orjson.dumps`` calls that guard a handler.

    A call counts only when it is certain to run before the try body could
    raise ``TypeError`` explicitly: it must sit on the unconditional path of a
    statement, and scanning stops at the first statement containing a
    ``raise``.  So ``if c: orjson.dumps(x) else: raise TypeError()`` never
    exempts the handler.
    """
    attempted: set[str] = set()
    for stmt in try_body:
        if _contains_raise(stmt):
            break
        for call in _iter_unconditional_calls(stmt):
            if _is_orjson_dumps_call(call, origins, shadowed) and call.args:
                attempted.add(ast.dump(call.args[0], include_attributes=False))
    return attempted


def _is_type_error_handler(handler: ast.ExceptHandler) -> bool:
    """Return whether a handler catches the built-in ``TypeError`` directly."""
    return isinstance(handler.type, ast.Name) and handler.type.id == "TypeError"


def _collect_json_fallback_calls(
    tree: ast.Module,
    json_module_names: frozenset[str],
    json_func_names: frozenset[str],
) -> frozenset[int]:
    """Find stdlib dumps calls paired with an orjson attempt in a TypeError handler.

    The stdlib call is a compatibility fallback only when the same argument
    expression was attempted with ``orjson.dumps`` on the unconditional path
    of the associated try body (see :func:`_attempted_orjson_arguments`).
    """
    origins = collect_import_origins(tree)
    shadowed = _collect_shadowed_names(tree)
    if "TypeError" in shadowed:
        return frozenset()
    fallback_calls: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Try, ast.TryStar)):
            continue

        attempted_arguments = _attempted_orjson_arguments(node.body, origins, shadowed)
        if not attempted_arguments:
            continue

        for handler in node.handlers:
            if not _is_type_error_handler(handler):
                continue
            for call in _iter_calls(handler.body):
                if (
                    _bound_call_attr(call, json_module_names, json_func_names)
                    != "dumps"
                ):
                    continue
                argument = _call_object_argument(call)
                if (
                    argument is not None
                    and ast.dump(argument, include_attributes=False)
                    in attempted_arguments
                ):
                    fallback_calls.add(id(call))
    return frozenset(fallback_calls)


class OrjsonOverStdlibJsonChecker(ast.NodeVisitor):
    """Walk a module AST and emit O001 findings."""

    def __init__(
        self,
        filename: str,
        directives: dict[int, _IgnoreDirective],
        json_module_names: frozenset[str],
        json_func_names: frozenset[str],
        fallback_calls: frozenset[int],
    ) -> None:
        self._filename = filename
        self._directives = directives
        self._json_module_names = json_module_names
        self._json_func_names = json_func_names
        self._fallback_calls = fallback_calls
        self._findings: list[Finding] = []

    def visit_Call(self, node: ast.Call) -> None:
        attr = _bound_call_attr(node, self._json_module_names, self._json_func_names)
        if attr is not None and id(node) not in self._fallback_calls:
            self._findings.append(
                make_finding(
                    filename=self._filename,
                    rule_id="O001",
                    node=node,
                    message=(
                        f"json.{attr}() — prefer orjson.{attr}() (a core SDK "
                        "dependency, ~10x faster). Note orjson is not a drop-in: "
                        "dumps() returns bytes and uses option=orjson.OPT_* instead "
                        "of indent=/sort_keys=."
                    ),
                    directives=self._directives,
                )
            )
        self.generic_visit(node)
