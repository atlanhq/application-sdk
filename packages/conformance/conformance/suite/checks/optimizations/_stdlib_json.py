"""O001 OrjsonOverStdlibJson — flag stdlib ``json.dumps``/``json.loads`` calls.

``orjson`` is a core SDK dependency and ~10x faster than stdlib ``json``; on hot
paths it is the recommended serialiser.  Only ``json.dumps`` / ``json.loads``
call sites are flagged, and only when ``json`` resolves to the stdlib module.
"""

from __future__ import annotations

import ast

from conformance.suite.checks._ast_common import _IgnoreDirective, make_finding
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


def _collect_orjson_bindings(tree: ast.Module) -> tuple[frozenset[str], frozenset[str]]:
    """Return module and function names bound to ``orjson.dumps``."""
    module_names: set[str] = set()
    func_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "orjson":
                    module_names.add(alias.asname or "orjson")
        elif isinstance(node, ast.ImportFrom) and node.module == "orjson":
            for alias in node.names:
                if alias.name == "dumps":
                    func_names.add(alias.asname or alias.name)
    return frozenset(module_names), frozenset(func_names)


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


def _is_type_error_handler(handler: ast.ExceptHandler) -> bool:
    """Return whether a handler catches the built-in ``TypeError`` directly."""
    return isinstance(handler.type, ast.Name) and handler.type.id == "TypeError"


def _collect_json_fallback_calls(
    tree: ast.Module,
    json_module_names: frozenset[str],
    json_func_names: frozenset[str],
    orjson_module_names: frozenset[str],
    orjson_func_names: frozenset[str],
) -> frozenset[int]:
    """Find stdlib dumps calls paired with an orjson attempt in a TypeError handler.

    The stdlib call is a compatibility fallback only when the same argument
    expression was attempted with ``orjson.dumps`` in the associated try body.
    """
    fallback_calls: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Try):
            continue

        attempted_arguments = {
            ast.dump(call.args[0], include_attributes=False)
            for call in _iter_calls(node.body)
            if _bound_call_attr(call, orjson_module_names, orjson_func_names) == "dumps"
            and call.args
        }
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
