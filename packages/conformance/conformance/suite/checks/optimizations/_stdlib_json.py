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


class OrjsonOverStdlibJsonChecker(ast.NodeVisitor):
    """Walk a module AST and emit O001 findings."""

    def __init__(
        self,
        filename: str,
        directives: dict[int, _IgnoreDirective],
        json_module_names: frozenset[str],
        json_func_names: frozenset[str],
    ) -> None:
        self._filename = filename
        self._directives = directives
        self._json_module_names = json_module_names
        self._json_func_names = json_func_names
        self._findings: list[Finding] = []

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        attr: str | None = None
        if (
            isinstance(func, ast.Attribute)
            and func.attr in _JSON_CALL_ATTRS
            and isinstance(func.value, ast.Name)
            and func.value.id in self._json_module_names
        ):
            attr = func.attr
        elif isinstance(func, ast.Name) and func.id in self._json_func_names:
            attr = func.id
        if attr is not None:
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
