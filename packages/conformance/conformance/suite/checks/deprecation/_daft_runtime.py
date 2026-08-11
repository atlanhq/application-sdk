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

``DataframeType.daft`` is deliberately **not** here.  It is the SDK's own
symbol, and it was only ever hand-coded in this checker because nothing marked
it — a comment is invisible to ``gen-deprecations``.  It now carries a
``__deprecated_members__`` entry (see ``application_sdk/common/types.py``), so
it rides the generated manifest and B001 reports it module-aware, like every
other SDK deprecation.  Keeping a second hand-written copy here would put two
findings on one line and reopen the drift the manifest's byte-gate exists to
prevent.

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


def _is_pyarrow_producer_call(node: ast.expr) -> bool:
    """Whether *node* is a call whose result is (heuristically) a pyarrow Table."""
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in _PYARROW_PRODUCER_ATTRS
    )


#: Node types that open a new local binding scope. ``ast.Lambda`` opens one
#: too: its parameters bind in the lambda's scope, so a
#: ``f = lambda tables: [t.to_pylist() for t in tables]`` shadows a module-level
#: ``tables = [pa.table({}) ...]`` exactly as a ``def`` parameter does.
_FUNCTION_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)
_SCOPE_NODES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)


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


def _yields_pyarrow(value: ast.expr | None) -> bool:
    """Whether *value* is, or is a collection of, pyarrow Tables.

    A producer call directly (``pa.table({})``), or a literal/comprehension whose
    elements are producer calls (``[pa.table({}) for _ in range(3)]``).  The
    collection case matters because the elements are what get iterated into a
    later comprehension target, and ``to_pylist()`` on a real Table is the
    *non*-deprecated API this rule must leave alone.
    """
    if value is None:
        return False
    if _is_pyarrow_producer_call(value):
        return True
    if isinstance(value, (ast.ListComp, ast.SetComp, ast.GeneratorExp)):
        return _is_pyarrow_producer_call(value.elt)
    if isinstance(value, (ast.List, ast.Tuple, ast.Set)):
        return any(_is_pyarrow_producer_call(e) for e in value.elts)
    return False


def _iterable_element(
    iterable: ast.expr,
    node: ast.AST,
    scopes: _ScopeMap,
    by_scope: dict[ast.AST, dict[str, list[tuple[int, bool]]]],
) -> ast.expr | None:
    """A stand-in producer node when *iterable* yields pyarrow Tables.

    ``tables = [pa.table({}) for _ in range(3)]`` then
    ``[t.to_pylist() for t in tables]`` — ``t`` genuinely is a pyarrow Table, and
    ``to_pylist()`` on one is the *non*-deprecated API this rule must leave
    alone. Recognises the two reachable shapes: a comprehension whose element
    expression is a producer call, and a name already bound to such a
    comprehension.

    The name lookup resolves only within *node*'s scope and its enclosing
    chain — never a sibling scope: a ``tables = [pa.table({}) ...]`` binding in
    one function must not exempt ``[t.to_pylist() for t in tables]`` in a
    different function whose ``tables`` is an SDK reader frame. Bindings are
    collected per scope precisely so generic names cannot leak exemptions
    across functions; scanning every scope here would re-open that hole.

    The walk also stops at the first scope that binds the name **non-pyarrow
    last**: a ``def f(tables):`` parameter (recorded unknown/non-pyarrow)
    shadows a module-level ``tables = [pa.table({}) ...]`` at runtime, so the
    walk must not reach past it to clear the call. Returning ``None`` there
    lets the shadowing scope void the exemption exactly as Python scoping does.
    """
    if _yields_pyarrow(iterable):
        return ast.Call(
            func=ast.Attribute(value=ast.Name(id="pa"), attr="table"),
            args=[],
            keywords=[],
        )
    if isinstance(iterable, ast.Name):
        scope: ast.AST | None = scopes.scope_of(node)
        while scope is not None:
            bindings = by_scope.get(scope, {}).get(iterable.id)
            if bindings:
                # Last binding in this scope decides. Pyarrow → stand-in
                # producer; non-pyarrow (incl. a shadowing parameter) → stop.
                if max(bindings, key=lambda b: b[0])[1]:
                    return ast.Call(
                        func=ast.Attribute(value=ast.Name(id="pa"), attr="table"),
                        args=[],
                        keywords=[],
                    )
                return None
            scope = scopes.parent_of(scope)
        return None
    return None


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
    deferred: list[ast.AST] = []

    def record(node: ast.AST, target: ast.expr, value: ast.expr | None) -> None:
        if not isinstance(target, ast.Name):
            # Unpacking: `df, other = frame, 1`. Pair element-wise with the
            # value when it is also a sequence, else treat each binding as
            # unknown — and therefore NOT pyarrow, so a stale exemption dies.
            if isinstance(target, (ast.Tuple, ast.List)):
                if isinstance(value, (ast.Tuple, ast.List)) and len(value.elts) == len(
                    target.elts
                ):
                    values: list[ast.expr | None] = list(value.elts)
                else:
                    values = [None] * len(target.elts)
                for element, element_value in zip(target.elts, values):
                    record(node, element, element_value)
            return
        scope = scopes.scope_of(node)
        entry = by_scope.setdefault(scope, {}).setdefault(target.id, [])
        is_pyarrow = _yields_pyarrow(value)
        entry.append((getattr(node, "lineno", 0), is_pyarrow))

    for node in ast.walk(tree):
        # Function parameters: `def f(df):` binds `df` in the function's scope
        # with an unknown value. Record every parameter as
        # unknown-and-therefore-non-pyarrow so the parameter kills an enclosing
        # same-named pyarrow exemption — a module-level `tables = [pa.table({})]`
        # must not clear `[t.to_pylist() for t in tables]` inside
        # `def f(tables):`, where the parameter shadows the global at runtime
        # and holds whatever the caller passed (an SDK reader frame, say).
        # Recorded at the `def` line, which sorts before every use in the body:
        # `ast.walk` is breadth-first, so assignments in this body are already
        # recorded before a *nested* function's parameters — a rebind to
        # pyarrow inside the body therefore still wins the own-scope
        # last-binding-before-the-use rule and re-establishes the exemption
        # from that line on, matching the runtime shadow-then-rebind sequence.
        if isinstance(node, _FUNCTION_SCOPES):
            # `ast.Lambda` has no positional-only args.
            posonly = getattr(node.args, "posonlyargs", [])
            for arg in (
                *posonly,
                *node.args.args,
                *node.args.kwonlyargs,
                *([node.args.vararg] if node.args.vararg else []),
                *([node.args.kwarg] if node.args.kwarg else []),
            ):
                entry = by_scope.setdefault(node, {}).setdefault(arg.arg, [])
                entry.append((getattr(node, "lineno", 0), False))
        # Plain assignment, including the chained form `a = df = frame`.
        if isinstance(node, ast.Assign):
            for target in node.targets:
                record(node, target, node.value)
        # Annotated assignment with a value.
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            record(node, node.target, node.value)
        # Augmented assignment: `df += frame`. The result depends on the prior
        # value, which static reach cannot recover — record it as
        # unknown-and-therefore-non-pyarrow so a stale pyarrow exemption dies
        # instead of silently exempting the rebound name.
        elif isinstance(node, ast.AugAssign):
            record(node, node.target, None)
        # Walrus: `if (df := frame):`
        elif isinstance(node, ast.NamedExpr):
            record(node, node.target, node.value)
        # Loop target: `for t in [pa.table({})]:`. The same logical shape as a
        # comprehension generator — the element of a pyarrow-producing iterable
        # is itself pyarrow — so it gets the same treatment and the same helper.
        elif isinstance(node, (ast.For, ast.AsyncFor)):
            deferred.append(node)
        # Context manager: `with frame as df:` — pyarrow only if the bound
        # expression is itself a producer.
        elif isinstance(node, (ast.With, ast.AsyncWith)):
            for item in node.items:
                if item.optional_vars is not None:
                    record(node, item.optional_vars, item.context_expr)
        # Comprehension targets: `[t.to_pylist() for t in tables]`. The element
        # of a pyarrow-producing iterable is itself pyarrow, which is why the
        # iterable is inspected rather than assumed non-pyarrow.
        elif isinstance(
            node, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)
        ):
            deferred.append(node)

    # Comprehensions resolve last: their iterable may be a name bound by an
    # assignment later in the walk order, and `ast.walk` is breadth-first.
    for node in deferred:
        if isinstance(node, (ast.For, ast.AsyncFor)):
            record(
                node, node.target, _iterable_element(node.iter, node, scopes, by_scope)
            )
        else:
            for gen in node.generators:
                record(
                    node,
                    gen.target,
                    _iterable_element(gen.iter, node, scopes, by_scope),
                )

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

    return findings
