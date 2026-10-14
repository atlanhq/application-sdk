"""P058 RawDictEntity — app emits an Atlas entity built as a raw dict.

``application_sdk.common.asset_serialization.entity_bytes`` applies the SDK's
central entity fixes to ``pyatlan_v9`` assets — among them a top-level
``status: ACTIVE`` (FND-3928), without which Atlas leaves a soft-deleted entity
DELETED when it reappears.  A hand-built ``{"typeName": ..., "attributes": ...}``
dict gets none of that unless its author remembers every key, and the failure
is silent.

Per-file, in hand-written app code only (``app/`` minus ``app/generated/``).

What counts as an entity dict
-----------------------------
A dict display whose literal string keys include both ``"typeName"`` and
``"attributes"``, or a ``dict(typeName=..., attributes=...)`` call.  A
``typeName`` alone — a reference filter, a ``uniqueAttributes`` ref — is not
one.

Where it fires
--------------
The same shape is also how Atlas encodes a struct value and a nested ref, so
the dict alone is not enough.  The rule fires only where the dict is emitted,
an allowlist of the sites real apps use:

* the argument of ``<x>.append(D)`` / ``<x>.insert(i, D)``, or an element of
  ``<x>.extend([...])`` (a list / tuple display or comprehension);
* the first argument of ``entity_bytes(D)``;
* ``yield D``;
* ``return D`` from a ``map_<entity>`` mapper.

``D`` is the dict itself or a simple name bound to one in the same scope
(``entity = {...}; entities.append(entity)``).  Binding order and control flow
are not modelled: any binding of the name to an entity dict in that scope
counts.  Everything else — a dict stored under a subscript, nested in another
dict, passed to ``model_validate`` or returned from a non-mapper helper — is
out of scope.

WARN tier.  A type ``pyatlan_v9`` does not model is the one sanctioned
carve-out: the dict must carry top-level ``"status": "ACTIVE"`` on a
non-delete upsert, suppressed inline with a reason.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import PurePath

from conformance.suite.checks._ast_common import _IgnoreDirective, make_finding
from conformance.suite.schema.findings import Finding

_ENTITY_KEYS: frozenset[str] = frozenset({"typeName", "attributes"})

#: List methods whose argument (by position) is one element added.
_ELEMENT_SINKS: dict[str, int] = {"append": 0, "insert": 1}

_SEAM = "entity_bytes"
_MAPPER_PREFIX = "map_"

_HINT = (
    "Build it with a `pyatlan_v9.model.assets` creator and serialize through "
    "`application_sdk.common.asset_serialization.entity_bytes`. If pyatlan_v9 "
    'does not model the type, the dict must carry top-level `"status": '
    '"ACTIVE"` on a non-delete upsert; suppress with '
    "`# conformance: ignore[P058] <reason>`"
)


def _in_app_source(filename: str) -> bool:
    """True for hand-written app code: under ``app/`` but not ``app/generated/``."""
    parts = PurePath(filename).parts
    if not parts or parts[0] != "app":
        return False
    return not (len(parts) > 2 and parts[1] == "generated")


def _entity_type_name(node: ast.expr) -> str | None:
    """The ``typeName`` an entity dict carries (``"?"`` if not literal), else ``None``."""
    if isinstance(node, ast.Dict):
        items = {
            k.value: v
            for k, v in zip(node.keys, node.values)
            if isinstance(k, ast.Constant) and isinstance(k.value, str)
        }
    elif (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "dict"
    ):
        items = {kw.arg: kw.value for kw in node.keywords if kw.arg is not None}
    else:
        return None
    if not _ENTITY_KEYS <= items.keys():
        return None
    type_name = items["typeName"]
    if isinstance(type_name, ast.Constant) and isinstance(type_name.value, str):
        return type_name.value
    return "?"


@dataclass
class _Scope:
    is_mapper: bool
    #: Entity dicts bound to each simple name in this scope.
    bound: dict[str, list[ast.expr]] = field(default_factory=dict)
    #: Names emitted at a sink in this scope.
    emitted_names: set[str] = field(default_factory=set)
    #: Entity dicts emitted at a sink directly.
    emitted: list[ast.expr] = field(default_factory=list)

    def flagged(self) -> list[ast.expr]:
        out = list(self.emitted)
        for name in self.emitted_names:
            out.extend(self.bound.get(name, ()))
        return out


class _Visitor(ast.NodeVisitor):
    def __init__(self) -> None:
        self._scope = _Scope(is_mapper=False)
        self.flagged: list[ast.expr] = []

    def _close(self, scope: _Scope) -> None:
        self.flagged.extend(scope.flagged())

    def finish(self) -> None:
        self._close(self._scope)

    # Scopes

    def _visit_scope(
        self, node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef
    ) -> None:
        saved = self._scope
        is_mapper = not isinstance(node, ast.ClassDef) and node.name.startswith(
            _MAPPER_PREFIX
        )
        self._scope = _Scope(is_mapper=is_mapper)
        self.generic_visit(node)
        self._close(self._scope)
        self._scope = saved

    visit_FunctionDef = _visit_scope
    visit_AsyncFunctionDef = _visit_scope
    visit_ClassDef = _visit_scope

    # Bindings

    def _bind(self, target: ast.expr, value: ast.expr) -> None:
        if isinstance(target, ast.Name) and _entity_type_name(value) is not None:
            self._scope.bound.setdefault(target.id, []).append(value)

    def visit_Assign(self, node: ast.Assign) -> None:
        for target in node.targets:
            self._bind(target, node.value)
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if node.value is not None:
            self._bind(node.target, node.value)
        self.generic_visit(node)

    # Sinks

    def _emit(self, value: ast.expr) -> None:
        if _entity_type_name(value) is not None:
            self._scope.emitted.append(value)
        elif isinstance(value, ast.Name):
            self._scope.emitted_names.add(value.id)

    def _emit_each(self, container: ast.expr) -> None:
        if isinstance(container, (ast.List, ast.Tuple)):
            for elt in container.elts:
                self._emit(elt)
        elif isinstance(container, (ast.ListComp, ast.GeneratorExp)):
            self._emit(container.elt)

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        if isinstance(func, ast.Attribute):
            index = _ELEMENT_SINKS.get(func.attr)
            if index is not None and len(node.args) > index:
                self._emit(node.args[index])
            elif func.attr == "extend" and node.args:
                self._emit_each(node.args[0])
        name = (
            func.id
            if isinstance(func, ast.Name)
            else func.attr
            if isinstance(func, ast.Attribute)
            else None
        )
        if name == _SEAM and node.args:
            self._emit(node.args[0])
        self.generic_visit(node)

    def visit_Yield(self, node: ast.Yield) -> None:
        if node.value is not None:
            self._emit(node.value)
        self.generic_visit(node)

    def visit_Return(self, node: ast.Return) -> None:
        if self._scope.is_mapper and node.value is not None:
            self._emit(node.value)
        self.generic_visit(node)


def check_p058(
    tree: ast.AST,
    filename: str,
    directives: dict[int, _IgnoreDirective],
) -> list[Finding]:
    """Emit P058 for each raw-dict Atlas entity app code emits."""
    if not _in_app_source(filename):
        return []
    visitor = _Visitor()
    visitor.visit(tree)
    visitor.finish()
    seen: set[int] = set()
    findings: list[Finding] = []
    for node in sorted(visitor.flagged, key=lambda n: (n.lineno, n.col_offset)):
        if id(node) in seen:
            continue
        seen.add(id(node))
        type_name = _entity_type_name(node)
        what = f"`{type_name}` entity" if type_name != "?" else "entity"
        findings.append(
            make_finding(
                filename=filename,
                rule_id="P058",
                node=node,
                message=(
                    f"{what} built as a raw dict — it skips what `entity_bytes` "
                    "applies to pyatlan assets, including top-level "
                    '`status: "ACTIVE"`, so a soft-deleted entity that reappears '
                    f"stays DELETED. {_HINT}."
                ),
                directives=directives,
            )
        )
    return findings
