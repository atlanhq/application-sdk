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
_FUNCTION_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef)
_SCOPE_NODES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)


class _ScopeMap:
    """Nearest-enclosing-scope lookup for every node in a module.

    ``scope_of(node)`` returns the ``FunctionDef``/``AsyncFunctionDef``/
    ``ClassDef`` (or the module) a node sits in; ``parent_of(scope)`` walks
    outward, so a closure still sees a binding made in an enclosing function.

    Class bodies get their own scope and are **skipped** on the outward walk out
    of a function, matching real Python scoping: a method never sees a
    class-body name as a free variable.  Folding class bodies into the module
    scope would let ``class Foo: df = pa.table({})`` exempt an unrelated ``df``
    in every method of the file — the cross-scope hole this map exists to close.
    """

    __slots__ = ("_owner", "_parent", "_root")

    def __init__(self, tree: ast.Module) -> None:
        self._root = tree
        self._owner: dict[ast.AST, ast.AST] = {tree: tree}
        self._parent: dict[ast.AST, ast.AST | None] = {tree: None}

        def enclosing_for_function(scope: ast.AST) -> ast.AST:
            # Skip class scopes: a method's free variables resolve to the
            # nearest enclosing *function* or the module, never the class body.
            cur: ast.AST | None = scope
            while isinstance(cur, ast.ClassDef):
                cur = self._parent.get(cur)
            return cur if cur is not None else tree

        def walk(scope: ast.AST, node: ast.AST) -> None:
            for child in ast.iter_child_nodes(node):
                if isinstance(child, _SCOPE_NODES):
                    self._owner[child] = scope  # the def name binds in `scope`
                    self._parent[child] = (
                        enclosing_for_function(scope)
                        if isinstance(child, _FUNCTION_SCOPES)
                        else scope
                    )
                    walk(child, child)
                else:
                    self._owner[child] = scope
                    walk(scope, child)

        walk(tree, tree)

    def scope_of(self, node: ast.AST) -> ast.AST:
        return self._owner.get(node, self._root)

    def parent_of(self, scope: ast.AST) -> ast.AST | None:
        return self._parent.get(scope)


def _pyarrow_bindings_by_scope(
    tree: ast.Module, scopes: _ScopeMap
) -> dict[ast.AST, dict[str, list[tuple[int, bool]]]]:
    """Per scope, per name, the ``(lineno, is_pyarrow)`` of each simple binding.

    Collected per scope rather than module-wide: generic receiver names
    (``df``, ``table``, ``data``, ``result``) recur across functions, so a
    whole-file set would let a legitimately-pyarrow ``df`` in one function
    exempt a genuine SDK reader frame of the same name in another — the guard
    erasing the very findings it exists to protect.

    **Non-pyarrow** assignments are recorded too, so the exemption can be made
    order-aware: ``df = pa.table({}); df = frame; df.to_pylist()`` must flag,
    because ``df`` is a real SDK frame by the time it is used.
    """
    by_scope: dict[ast.AST, dict[str, list[tuple[int, bool]]]] = {}
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
        ):
            continue
        scope = scopes.scope_of(node)
        name = node.targets[0].id
        entry = by_scope.setdefault(scope, {}).setdefault(name, [])
        entry.append((node.lineno, _is_pyarrow_producer_call(node.value)))
    return by_scope


def _is_pyarrow_bound(
    name: str,
    node: ast.AST,
    scopes: _ScopeMap,
    by_scope: dict[ast.AST, dict[str, list[tuple[int, bool]]]],
) -> bool:
    """Whether *name* is pyarrow-bound at *node*, in its scope or an enclosing one.

    In the node's **own** scope the last binding before the use decides, so
    rebinding a pyarrow name to an SDK frame correctly voids the exemption.

    In an **enclosing** scope any binding counts, regardless of line order: a
    closure body executes when it is called, not where it is written, so a
    nested function defined above the ``table = pa.table({})`` it reads still
    sees that binding at call time. Applying the line filter outward flagged
    correct code.

    Known limit: "last binding before the use" assumes straight-line flow. A
    name assigned in both arms of an ``if``/``else`` or ``try``/``except`` is
    decided by whichever arm is written last, which can mis-decide in either
    direction. Full CFG modelling is out of scope for a WARN-tier detector.
    """
    use_line = getattr(node, "lineno", 0)
    own_scope = scopes.scope_of(node)
    scope: ast.AST | None = own_scope
    while scope is not None:
        bindings = by_scope.get(scope, {}).get(name)
        if bindings:
            if scope is own_scope:
                prior = [b for b in bindings if b[0] <= use_line]
                if prior:
                    return max(prior, key=lambda b: b[0])[1]
                # Bound only after this point — not in effect; look outward.
            else:
                return max(bindings, key=lambda b: b[0])[1]
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
    pyarrow_by_scope = _pyarrow_bindings_by_scope(tree, scopes)
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
