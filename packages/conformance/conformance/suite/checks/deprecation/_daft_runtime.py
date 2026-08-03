"""B007 ``DaftOnlyDataframeApiUsage`` — daft APIs dead on the daft-less runtime.

Runs against *consumer apps* (scope ``app``).  On SDK >= 3.22 the ``[daft]``
extra is empty and SDK readers return **pandas** DataFrames, so daft-only
DataFrame APIs raise ``AttributeError`` on the frames apps actually receive —
latent breakage that imports and mocked unit tests never exercise (a
document-store connector hit every surface below in fleet testing, live on
main).  These are third-party daft APIs, not SDK symbols, so the generated
deprecated-symbol manifest (B001) cannot carry them; this module encodes them
directly.

Surfaces matched (only in files that import ``application_sdk`` somewhere —
a repo that never touches the SDK is not consuming SDK reader frames):

* ``frame.count_rows()`` — daft-only; pandas: ``len(frame)``.
* ``frame.to_pylist()`` — daft-only on reader frames; pandas:
  ``frame.to_dict("records")``.  Exempt when the receiver is demonstrably a
  pyarrow Table (a name bound from ``pa.Table.from_*`` / ``pa.table(...)`` /
  ``*.to_arrow_table()`` / ``*.combine_chunks()``, or such a call chained
  directly) — ``pyarrow.Table.to_pylist()`` is a real API the SDK itself uses.
* ``frame.names`` — daft-only; pandas: ``frame.columns``.  Only
  simple-variable receivers are matched: ``df.schema.names`` (pyarrow) and
  ``df.index.names`` (pandas) are legitimate attribute chains and never flag.
* ``DataframeType.daft`` — the deprecated no-op enum alias (routes to the
  pandas/pyarrow path; removal in v4.0).  Matched only when ``DataframeType``
  is imported from ``application_sdk``.

Matching is attribute-name-anchored (the accepted B001 posture at WARN);
suppress with ``# conformance: ignore[B007] <reason>`` where the receiver is
genuinely not an SDK reader frame.
"""

from __future__ import annotations

import ast

from conformance.suite.checks._ast_common import _IgnoreDirective, make_finding
from conformance.suite.schema.findings import Finding

_RULE_ID = "B007"

_SDK_IMPORT_ROOT = "application_sdk"

#: Daft-only method calls, mapped to their pandas migration.
_DAFT_ONLY_METHODS: dict[str, str] = {
    "count_rows": "use len(frame) on the pandas frame",
    "to_pylist": 'use frame.to_dict("records") on the pandas frame',
}

#: Callee attribute names whose result is a pyarrow Table — receivers bound
#: from these are exempt from the ``to_pylist`` match.
_PYARROW_PRODUCER_ATTRS = frozenset(
    {
        "from_pandas",
        "from_pylist",
        "from_arrays",
        "from_batches",
        "table",
        "to_arrow_table",
        "combine_chunks",
        "read_table",
    }
)


def _imports_sdk(tree: ast.Module) -> bool:
    """Whether the module imports ``application_sdk`` (any form)."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(
                alias.name == _SDK_IMPORT_ROOT
                or alias.name.startswith(_SDK_IMPORT_ROOT + ".")
                for alias in node.names
            ):
                return True
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if node.level == 0 and (
                mod == _SDK_IMPORT_ROOT or mod.startswith(_SDK_IMPORT_ROOT + ".")
            ):
                return True
    return False


def _dataframe_type_binding(tree: ast.Module) -> str | None:
    """Local name bound to the SDK ``DataframeType`` enum, if imported."""
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if node.level != 0 or not mod.startswith(_SDK_IMPORT_ROOT):
                continue
            for alias in node.names:
                if alias.name == "DataframeType":
                    return alias.asname or alias.name
    return None


def _is_pyarrow_producer_call(node: ast.expr) -> bool:
    """Whether *node* is a call whose result is (heuristically) a pyarrow Table."""
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in _PYARROW_PRODUCER_ATTRS
    )


#: Node types that open a new local binding scope.
_SCOPE_NODES = (ast.FunctionDef, ast.AsyncFunctionDef)


class _ScopeMap:
    """Nearest-enclosing-scope lookup for every node in a module.

    ``scope_of(node)`` returns the ``FunctionDef``/``AsyncFunctionDef`` (or the
    module) a node sits in; ``parent_of(scope)`` walks outward, so a closure can
    still see a binding made in an enclosing function.
    """

    __slots__ = ("_owner", "_parent", "_root")

    def __init__(self, tree: ast.Module) -> None:
        self._root = tree
        self._owner: dict[ast.AST, ast.AST] = {tree: tree}
        self._parent: dict[ast.AST, ast.AST | None] = {tree: None}

        def walk(scope: ast.AST, node: ast.AST) -> None:
            for child in ast.iter_child_nodes(node):
                if isinstance(child, _SCOPE_NODES):
                    self._owner[child] = scope  # the def name binds in `scope`
                    self._parent[child] = scope
                    walk(child, child)
                else:
                    self._owner[child] = scope
                    walk(scope, child)

        walk(tree, tree)

    def scope_of(self, node: ast.AST) -> ast.AST:
        return self._owner.get(node, self._root)

    def parent_of(self, scope: ast.AST) -> ast.AST | None:
        return self._parent.get(scope)


def _pyarrow_names_by_scope(
    tree: ast.Module, scopes: _ScopeMap
) -> dict[ast.AST, set[str]]:
    """Map each binding scope to names bound to a pyarrow producer *in that scope*.

    Collected per ``FunctionDef``/``AsyncFunctionDef`` (plus module level)
    rather than module-wide: generic receiver names (``df``, ``table``,
    ``data``, ``result``) recur across functions, so a whole-file set would let
    a legitimately-pyarrow ``df`` in one function exempt a genuine SDK reader
    frame of the same name in another — the guard erasing the very findings it
    exists to protect.
    """
    by_scope: dict[ast.AST, set[str]] = {}
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and _is_pyarrow_producer_call(node.value)
        ):
            scope = scopes.scope_of(node)
            by_scope.setdefault(scope, set()).add(node.targets[0].id)
    return by_scope


def _is_pyarrow_bound(
    name: str,
    node: ast.AST,
    scopes: _ScopeMap,
    by_scope: dict[ast.AST, set[str]],
) -> bool:
    """Whether *name* is pyarrow-bound in *node*'s scope or an enclosing one."""
    scope: ast.AST | None = scopes.scope_of(node)
    while scope is not None:
        if name in by_scope.get(scope, ()):
            return True
        scope = scopes.parent_of(scope)
    return False


def scan_daft_runtime(
    tree: ast.Module,
    file: str,
    directives: dict[int, _IgnoreDirective],
) -> list[Finding]:
    """Return B007 findings for *tree*."""
    if not _imports_sdk(tree):
        return []

    scopes = _ScopeMap(tree)
    pyarrow_by_scope = _pyarrow_names_by_scope(tree, scopes)
    dataframe_type_name = _dataframe_type_binding(tree)
    findings: list[Finding] = []

    def _flag(node: ast.AST, surface: str, migration: str) -> None:
        findings.append(
            make_finding(
                filename=file,
                rule_id=_RULE_ID,
                node=node,
                message=(
                    f"{surface} is a daft-only DataFrame API — dead on SDK >= 3.22, "
                    "where the [daft] extra is empty and SDK readers return pandas "
                    f"frames (AttributeError at runtime). Migrate: {migration}."
                ),
                directives=directives,
            )
        )

    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            attr = node.func.attr
            if attr not in _DAFT_ONLY_METHODS:
                continue
            receiver = node.func.value
            if attr == "to_pylist":
                # pyarrow.Table.to_pylist() is a real API: exempt receivers
                # demonstrably bound to / produced by a pyarrow call — but only
                # within the scope that binding was made in.
                if isinstance(receiver, ast.Name) and _is_pyarrow_bound(
                    receiver.id, node, scopes, pyarrow_by_scope
                ):
                    continue
                if _is_pyarrow_producer_call(receiver):
                    continue
            _flag(node, f".{attr}()", _DAFT_ONLY_METHODS[attr])
        elif isinstance(node, ast.Attribute) and node.attr == "names":
            # Only simple-variable receivers: df.schema.names / df.index.names
            # are legitimate pyarrow/pandas chains.  ``self``/``cls`` receivers
            # are the app's own attribute, never a reader frame.
            receiver = node.value
            if (
                isinstance(receiver, ast.Name)
                and receiver.id not in ("self", "cls")
                and not _is_pyarrow_bound(receiver.id, node, scopes, pyarrow_by_scope)
            ):
                _flag(node, ".names", "use frame.columns on the pandas frame")
        elif (
            isinstance(node, ast.Attribute)
            and node.attr == "daft"
            and isinstance(node.value, ast.Name)
            and dataframe_type_name is not None
            and node.value.id == dataframe_type_name
        ):
            _flag(
                node,
                "DataframeType.daft",
                "use DataframeType.pandas (daft is a deprecated no-op alias, "
                "removal in v4.0)",
            )

    return findings
