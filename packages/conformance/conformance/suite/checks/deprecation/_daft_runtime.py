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
  ``frame.to_dict("records")``.  Flagged **only on pandas evidence**, because
  ``pyarrow.Table.to_pylist()`` is a real API: the receiver is assigned from
  an SDK frame API (``ParquetFileReader`` / ``JsonFileReader`` ``.read()``,
  ``read_batches()``, the SQL client ``get_results()`` /
  ``get_batched_results()``, including ``async for … in``); or it comes from
  an app function in the same repo annotated ``-> pd.DataFrame`` (or an
  iterator of them), or, unannotated, whose body returns an SDK frame call
  directly (one level); or it is a parameter or variable annotated
  ``pd.DataFrame`` or a union holding it, outside an
  ``if isinstance(x, pa.<Type>)`` branch; or pandas created it
  (``pd.DataFrame(...)``, ``pd.read_*``, ``pd.concat``, ``.to_pandas()``).
  No evidence, no finding.  The pyarrow-receiver exemption below still
  applies on top.  Known miss: a frame passed through unannotated,
  multi-level helpers or stored on an object; it fails loudly with
  ``AttributeError`` the first time a test runs it.

  A receiver is pyarrow when it is a call to a pyarrow import alias
  (``pa.table(...)``, ``pq.ParquetFile(...)``) or to a producer method
  (``from_pandas``, ``read_table``, ``to_arrow_table``, ``to_arrow`` …); a
  method chain on such a value (``table.column("a")``,
  ``file.iter_batches()``), stopping at ``to_pandas`` / ``to_polars``; a name
  whose binding in effect at the use is one of these; or a parameter
  annotated with a pyarrow type (``pa.Table``, ``pa.Table | None``,
  ``Optional[pa.Table]``).  Imports are bindings like any other, scoped to
  where they sit: an ``import pyarrow as frame`` inside one function does not
  make a ``frame`` elsewhere pyarrow, and a local binding shadows a pyarrow
  import of the same name.  Collection elements count too: iterating
  ``[table.column("x") for ...]`` yields pyarrow columns.
* ``frame.names`` — daft-only; pandas: ``frame.columns``.  Only
  simple-variable receivers are matched (``df.schema.names`` and
  ``df.index.names`` are legitimate attribute chains), with the same pyarrow
  exemption.

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
from collections.abc import Callable, Iterable

from conformance.suite.checks._ast_common import _IgnoreDirective, make_finding
from conformance.suite.schema.findings import Finding

_RULE_ID = "B007"

_SDK_IMPORT_ROOT = "application_sdk"

#: Daft-only method calls, mapped to their pandas migration.
_DAFT_ONLY_METHODS: dict[str, str] = {
    "count_rows": "use len(frame) on the pandas frame",
    "to_pylist": 'use frame.to_dict("records") on the pandas frame',
}

#: Callee attribute names whose result is a pyarrow object whatever the
#: receiver — receivers bound from these are exempt from the ``to_pylist`` match.
#: Methods such as ``column`` or ``iter_batches`` are not here: they return
#: pyarrow only when their receiver is pyarrow, which the chain walk decides.
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
        "to_arrow",
    }
)

#: Methods whose result leaves pyarrow for a frame library, so derivation
#: through them stops: ``table.to_pandas().to_pylist()`` is a real violation.
_PYARROW_EXIT_ATTRS = frozenset({"to_pandas", "to_polars"})

_PYARROW_ROOT = "pyarrow"


def _is_pyarrow_module(name: str) -> bool:
    return name == _PYARROW_ROOT or name.startswith(_PYARROW_ROOT + ".")


def _import_bindings(node: ast.Import | ast.ImportFrom) -> list[tuple[str, bool]]:
    """The ``(local name, is_pyarrow)`` of each name an import statement binds.

    Every import is returned, not only pyarrow ones: ``from fixtures import pa``
    in a function shadows a module-level ``import pyarrow as pa`` there.
    ``from x import *`` binds nothing nameable and is skipped.
    """
    if isinstance(node, ast.Import):
        return [
            (alias.asname or alias.name.split(".")[0], _is_pyarrow_module(alias.name))
            for alias in node.names
        ]
    from_pyarrow = node.level == 0 and _is_pyarrow_module(node.module or "")
    return [
        (alias.asname or alias.name, from_pyarrow)
        for alias in node.names
        if alias.name != "*"
    ]


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


def _annotation_is_pyarrow(annotation: ast.expr, at: ast.AST, ctx: "_Ctx") -> bool:
    """``pa.Table`` / a name imported from pyarrow, alone or made optional.

    ``X | None``, ``Optional[X]`` and ``Union[X, None]`` count when every
    non-``None`` member is a pyarrow type.
    """
    if isinstance(annotation, ast.BinOp) and isinstance(annotation.op, ast.BitOr):
        members = [annotation.left, annotation.right]
    elif isinstance(annotation, ast.Subscript) and _subscript_name(annotation) in (
        "Optional",
        "Union",
    ):
        inner = annotation.slice
        members = list(inner.elts) if isinstance(inner, ast.Tuple) else [inner]
    else:
        base = annotation
        while isinstance(base, ast.Attribute):
            base = base.value
        return isinstance(base, ast.Name) and ctx.bound(base.id, at) is True
    typed = [
        m for m in members if not (isinstance(m, ast.Constant) and m.value is None)
    ]
    return bool(typed) and all(_annotation_is_pyarrow(m, at, ctx) for m in typed)


def _subscript_name(node: ast.Subscript) -> str:
    value = node.value
    if isinstance(value, ast.Attribute):
        return value.attr
    return value.id if isinstance(value, ast.Name) else ""


class _Ctx:
    __slots__ = ("bound",)

    def __init__(self, bound: Callable[[str, ast.AST], bool | None]) -> None:
        self.bound = bound


def _derives_from_pyarrow(
    expr: ast.expr | None, at: ast.AST, ctx: "_Ctx | None"
) -> bool:
    """Whether *expr* evaluates to a pyarrow object (Table, batch, array, schema)."""
    depth = 0
    while expr is not None and depth < 50:
        depth += 1
        if isinstance(expr, ast.Await):
            expr = expr.value
        elif isinstance(expr, ast.Call):
            func = expr.func
            if isinstance(func, ast.Attribute):
                if func.attr in _PYARROW_EXIT_ATTRS:
                    return False
                if func.attr in _PYARROW_PRODUCER_ATTRS:
                    return True
                expr = func.value
            elif isinstance(func, ast.Name):
                return ctx is not None and ctx.bound(func.id, at) is True
            else:
                return False
        elif isinstance(expr, ast.Attribute):
            if expr.attr in _PYARROW_EXIT_ATTRS:
                return False
            expr = expr.value
        elif isinstance(expr, ast.Subscript):
            expr = expr.value
        elif isinstance(expr, ast.Name):
            return ctx is not None and ctx.bound(expr.id, at) is True
        else:
            return False
    return False


def _yields_pyarrow(
    value: ast.expr | None, at: ast.AST | None = None, ctx: "_Ctx | None" = None
) -> bool:
    """Whether *value* is, or is a collection of, pyarrow objects.

    A pyarrow value directly (``pa.table({})``, ``table.column("x")``), or a
    literal/comprehension whose elements are (``[pa.table({}) for _ in
    range(3)]``, ``[table.column("x") for ...]``).  The collection case matters
    because the elements are what get iterated into a later comprehension
    target, and ``to_pylist()`` on a real Table is the *non*-deprecated API this
    rule must leave alone.
    """
    if value is None:
        return False

    def is_pyarrow(expr: ast.expr) -> bool:
        return _is_pyarrow_producer_call(expr) or (
            ctx is not None and _derives_from_pyarrow(expr, at, ctx)
        )

    if is_pyarrow(value):
        return True
    if isinstance(value, (ast.ListComp, ast.SetComp, ast.GeneratorExp)):
        return is_pyarrow(value.elt)
    if isinstance(value, (ast.List, ast.Tuple, ast.Set)):
        return any(is_pyarrow(e) for e in value.elts)
    return False


def _iterable_element(
    iterable: ast.expr,
    node: ast.AST,
    scopes: _ScopeMap,
    by_scope: dict[ast.AST, dict[str, list[tuple[float, bool]]]],
    ctx: "_Ctx | None" = None,
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
    if _yields_pyarrow(iterable, node, ctx):
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
    tree: ast.Module, scopes: _ScopeMap, ctx: "_Ctx | None" = None
) -> dict[ast.AST, dict[str, list[tuple[float, bool]]]]:
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
    by_scope: dict[ast.AST, dict[str, list[tuple[float, bool]]]] = {}
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
        is_pyarrow = _yields_pyarrow(value, node, ctx)
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
                # Annotations evaluate where the `def` is, so resolve their
                # names from the def node's own (enclosing) scope.
                annotated = (
                    ctx is not None
                    and arg.annotation is not None
                    and _annotation_is_pyarrow(arg.annotation, node, ctx)
                )
                entry.append((getattr(node, "lineno", 0) - 0.5, annotated))
        # Imports bind in the scope they sit in, like any assignment: a
        # `import pyarrow as frame` inside a helper must not make a module-level
        # `frame` look pyarrow.
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for name, is_pyarrow in _import_bindings(node):
                by_scope.setdefault(scopes.scope_of(node), {}).setdefault(
                    name, []
                ).append((getattr(node, "lineno", 0), is_pyarrow))
        # Plain assignment, including the chained form `a = df = frame`.
        elif isinstance(node, ast.Assign):
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
                node,
                node.target,
                _iterable_element(node.iter, node, scopes, by_scope, ctx),
            )
        else:
            for gen in node.generators:
                record(
                    node,
                    gen.target,
                    _iterable_element(gen.iter, node, scopes, by_scope, ctx),
                )

    return by_scope


def _is_pyarrow_bound(
    name: str,
    node: ast.AST,
    scopes: _ScopeMap,
    by_scope: dict[ast.AST, dict[str, list[tuple[float, bool]]]],
    strict: bool = False,
) -> bool | None:
    """Whether *name* is pyarrow-bound at *node*, in its scope or an enclosing one.

    ``None`` when no binding of *name* is in effect, so a caller can fall back
    to the module's pyarrow import aliases without letting them shadow a local
    binding.

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
                prior = [
                    b
                    for b in bindings
                    if (b[0] < use_line if strict else b[0] <= use_line)
                ]
                if prior:
                    return max(prior, key=lambda b: b[0])[1]
                # Bound only after this point — not in effect; look outward.
            else:
                return max(bindings, key=lambda b: b[0])[1]
        scope = scopes.parent_of(scope)
    return None


#: SDK reader classes whose ``.read()`` returns a pandas DataFrame.
_SDK_READER_CLASSES = frozenset({"ParquetFileReader", "JsonFileReader"})

#: SDK methods whose (awaited) result is a pandas DataFrame, on any receiver.
_SDK_FRAME_ATTRS = frozenset({"get_results"})

#: SDK methods that return an iterator of pandas DataFrames, on any receiver.
#: ``run_query`` is not here: the SDK SQL clients yield ``list[dict]`` batches.
_SDK_FRAME_ITER_ATTRS = frozenset({"read_batches", "get_batched_results"})

_ITERATOR_GENERICS = frozenset(
    {
        "Iterator",
        "AsyncIterator",
        "Iterable",
        "AsyncIterable",
        "Generator",
        "AsyncGenerator",
    }
)

_PANDAS_ROOT = "pandas"

_FRAME = "frame"
_FRAME_ITER = "frame_iter"
_READER = "reader"


class FrameSummary:
    """Repo-wide names of app functions that return pandas frames.

    One level only: a function counts when its return annotation is a pandas
    ``DataFrame`` (or an iterator of them), or, unannotated, when a ``return``
    in its own body is a direct SDK frame call.  A name is kept only when every
    definition of it in the repo agrees.
    """

    __slots__ = ("frame_fns", "frame_iter_fns")

    def __init__(
        self, frame_fns: frozenset[str], frame_iter_fns: frozenset[str]
    ) -> None:
        self.frame_fns = frame_fns
        self.frame_iter_fns = frame_iter_fns


_EMPTY_SUMMARY = FrameSummary(frozenset(), frozenset())


class _Aliases:
    """Module-wide local names bound to pandas, pyarrow and the SDK readers."""

    __slots__ = (
        "pandas_modules",
        "pandas_names",
        "pyarrow_modules",
        "pyarrow_names",
        "readers",
    )

    def __init__(self, tree: ast.Module) -> None:
        self.pandas_modules: set[str] = set()
        self.pandas_names: dict[str, str] = {}
        self.pyarrow_modules: set[str] = set()
        self.pyarrow_names: set[str] = set()
        self.readers: set[str] = set(_SDK_READER_CLASSES)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    local = alias.asname or alias.name.split(".")[0]
                    if alias.name == _PANDAS_ROOT or alias.name.startswith(
                        _PANDAS_ROOT + "."
                    ):
                        self.pandas_modules.add(local)
                    elif _is_pyarrow_module(alias.name):
                        self.pyarrow_modules.add(local)
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                module = node.module or ""
                for alias in node.names:
                    local = alias.asname or alias.name
                    if module == _PANDAS_ROOT or module.startswith(_PANDAS_ROOT + "."):
                        self.pandas_names[local] = alias.name
                    elif _is_pyarrow_module(module):
                        self.pyarrow_names.add(local)
                    elif alias.name in _SDK_READER_CLASSES:
                        self.readers.add(local)

    def _root(self, node: ast.expr) -> str | None:
        while isinstance(node, ast.Attribute):
            node = node.value
        return node.id if isinstance(node, ast.Name) else None

    def is_pandas_frame_type(self, node: ast.expr) -> bool:
        if isinstance(node, ast.Attribute):
            return node.attr == "DataFrame" and self._root(node) in self.pandas_modules
        if isinstance(node, ast.Name):
            return self.pandas_names.get(node.id) == "DataFrame"
        return False

    def is_pyarrow_type(self, node: ast.expr) -> bool:
        if isinstance(node, ast.Attribute):
            return self._root(node) in self.pyarrow_modules
        return isinstance(node, ast.Name) and node.id in self.pyarrow_names

    def is_pandas_factory(self, func: ast.expr) -> bool:
        """``pd.DataFrame`` / ``pd.read_*`` / ``pd.concat`` (or their imports)."""
        if isinstance(func, ast.Attribute):
            name = func.attr
            if self._root(func) not in self.pandas_modules:
                return False
        elif isinstance(func, ast.Name) and func.id in self.pandas_names:
            name = self.pandas_names[func.id]
        else:
            return False
        return name in ("DataFrame", "concat") or name.startswith("read_")


def _string_annotation(node: ast.expr) -> ast.expr:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        try:
            return ast.parse(node.value, mode="eval").body
        except SyntaxError:
            return node
    return node


def _union_members(node: ast.expr) -> list[ast.expr] | None:
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        return [node.left, node.right]
    if isinstance(node, ast.Subscript) and _subscript_name(node) in (
        "Optional",
        "Union",
    ):
        inner = node.slice
        return list(inner.elts) if isinstance(inner, ast.Tuple) else [inner]
    return None


def _annotation_has_frame(node: ast.expr, aliases: _Aliases) -> bool:
    """A pandas ``DataFrame`` annotation, or a union / ``Annotated`` holding one."""
    node = _string_annotation(node)
    if aliases.is_pandas_frame_type(node):
        return True
    members = _union_members(node)
    if members is not None:
        return any(_annotation_has_frame(m, aliases) for m in members)
    if isinstance(node, ast.Subscript) and _subscript_name(node) == "Annotated":
        inner = node.slice
        first = inner.elts[0] if isinstance(inner, ast.Tuple) and inner.elts else inner
        return _annotation_has_frame(first, aliases)
    return False


def _annotation_iterates_frames(node: ast.expr, aliases: _Aliases) -> bool:
    """``Iterator[pd.DataFrame]`` and friends, alone or in a union."""
    node = _string_annotation(node)
    members = _union_members(node)
    if members is not None:
        return any(_annotation_iterates_frames(m, aliases) for m in members)
    if isinstance(node, ast.Subscript) and _subscript_name(node) in _ITERATOR_GENERICS:
        inner = node.slice
        first = inner.elts[0] if isinstance(inner, ast.Tuple) and inner.elts else inner
        return _annotation_has_frame(first, aliases)
    return False


def _annotation_is_reader(node: ast.expr, aliases: _Aliases) -> bool:
    node = _string_annotation(node)
    members = _union_members(node)
    if members is not None:
        return any(_annotation_is_reader(m, aliases) for m in members)
    if isinstance(node, ast.Name):
        return node.id in aliases.readers
    return isinstance(node, ast.Attribute) and node.attr in _SDK_READER_CLASSES


def _annotation_kind(node: ast.expr, aliases: _Aliases) -> str | None:
    if _annotation_has_frame(node, aliases):
        return _FRAME
    if _annotation_iterates_frames(node, aliases):
        return _FRAME_ITER
    if _annotation_is_reader(node, aliases):
        return _READER
    return None


_Binding = tuple[float, Callable[[], "str | None"]]


class _PandasEvidence:
    """Whether a receiver has evidence of being a pandas frame (B007 ``to_pylist``).

    Bindings are scoped like the pyarrow exemption: in the use's own scope the
    last binding at or before the use decides; an enclosing scope's last
    binding counts regardless of line.  A name annotated as a pandas frame in a
    scope keeps that declared type for every use in the scope.
    """

    def __init__(
        self,
        tree: ast.Module,
        scopes: _ScopeMap,
        aliases: _Aliases,
        summary: FrameSummary,
    ) -> None:
        self._scopes = scopes
        self._aliases = aliases
        self._summary = summary
        self._bindings: dict[ast.AST, dict[str, list[_Binding]]] = {}
        self._declared: dict[ast.AST, set[str]] = {}
        self._memo: dict[int, str | None] = {}
        self._collect(tree)

    def _bind(
        self, scope: ast.AST, name: str, pos: float, kind: Callable[[], str | None]
    ) -> None:
        self._bindings.setdefault(scope, {}).setdefault(name, []).append((pos, kind))

    def _bind_target(
        self, at: ast.AST, target: ast.expr, pos: float, kind: Callable[[], str | None]
    ) -> None:
        scope = self._scopes.scope_of(at)
        if isinstance(target, ast.Name):
            self._bind(scope, target.id, pos, kind)
        elif isinstance(target, (ast.Tuple, ast.List)):
            for element in target.elts:
                self._bind_target(at, element, pos, lambda: None)
        elif isinstance(target, ast.Starred):
            self._bind_target(at, target.value, pos, lambda: None)

    def _bind_assignment(
        self, at: ast.AST, target: ast.expr, value: ast.expr, pos: float
    ) -> None:
        if (
            isinstance(target, (ast.Tuple, ast.List))
            and isinstance(value, (ast.Tuple, ast.List))
            and len(target.elts) == len(value.elts)
        ):
            for element, element_value in zip(target.elts, value.elts):
                self._bind_assignment(at, element, element_value, pos)
            return
        self._bind_target(at, target, pos, self._value_kind(value, at))

    def _collect(self, tree: ast.Module) -> None:
        aliases = self._aliases
        for node in ast.walk(tree):
            if isinstance(node, _FUNCTION_SCOPES):
                posonly = getattr(node.args, "posonlyargs", [])
                for arg in (
                    *posonly,
                    *node.args.args,
                    *node.args.kwonlyargs,
                    *([node.args.vararg] if node.args.vararg else []),
                    *([node.args.kwarg] if node.args.kwarg else []),
                ):
                    kind = (
                        _annotation_kind(arg.annotation, aliases)
                        if arg.annotation is not None
                        else None
                    )
                    if kind == _FRAME:
                        self._declared.setdefault(node, set()).add(arg.arg)
                    self._bind(node, arg.arg, node.lineno - 0.5, lambda k=kind: k)
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                for name, _ in _import_bindings(node):
                    self._bind(
                        self._scopes.scope_of(node),
                        name,
                        node.lineno + 0.5,
                        lambda: None,
                    )
            elif isinstance(node, ast.Assign):
                pos = (node.end_lineno or node.lineno) + 0.5
                for target in node.targets:
                    self._bind_assignment(node, target, node.value, pos)
            elif isinstance(node, ast.AnnAssign):
                pos = (node.end_lineno or node.lineno) + 0.5
                kind = _annotation_kind(node.annotation, aliases)
                if kind == _FRAME and isinstance(node.target, ast.Name):
                    self._declared.setdefault(self._scopes.scope_of(node), set()).add(
                        node.target.id
                    )
                if kind is not None:
                    self._bind_target(node, node.target, pos, lambda k=kind: k)
                elif node.value is not None:
                    self._bind_target(
                        node, node.target, pos, self._value_kind(node.value, node)
                    )
            elif isinstance(node, ast.AugAssign):
                self._bind_target(
                    node,
                    node.target,
                    (node.end_lineno or node.lineno) + 0.5,
                    self._value_kind(node.value, node),
                )
            elif isinstance(node, ast.NamedExpr):
                self._bind_target(
                    node, node.target, node.lineno, self._value_kind(node.value, node)
                )
            elif isinstance(node, (ast.With, ast.AsyncWith)):
                for item in node.items:
                    if item.optional_vars is not None:
                        self._bind_target(
                            node,
                            item.optional_vars,
                            node.lineno + 0.5,
                            self._value_kind(item.context_expr, node),
                        )
            elif isinstance(node, (ast.For, ast.AsyncFor)):
                self._bind_target(
                    node,
                    node.target,
                    node.lineno + 0.5,
                    self._element_kind(node.iter, node),
                )
            elif isinstance(
                node, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)
            ):
                for gen in node.generators:
                    self._bind_target(
                        node,
                        gen.target,
                        node.lineno - 0.5,
                        self._element_kind(gen.iter, node),
                    )

    def _value_kind(self, value: ast.expr, at: ast.AST) -> Callable[[], str | None]:
        def kind() -> str | None:
            if self.is_frame(value, at):
                return _FRAME
            if self.is_frame_iter(value, at):
                return _FRAME_ITER
            if self.is_reader(value, at):
                return _READER
            return None

        return kind

    def _element_kind(
        self, iterable: ast.expr, at: ast.AST
    ) -> Callable[[], str | None]:
        return lambda: _FRAME if self.is_frame_iter(iterable, at) else None

    def _resolve(self, binding: _Binding) -> str | None:
        key = id(binding)
        if key in self._memo:
            return self._memo[key]
        self._memo[key] = None
        result = binding[1]()
        self._memo[key] = result
        return result

    def name_kind(self, name: str, at: ast.AST) -> str | None:
        use = getattr(at, "lineno", 0)
        own = self._scopes.scope_of(at)
        scope: ast.AST | None = own
        while scope is not None:
            if name in self._declared.get(scope, ()):
                return _FRAME
            bindings = self._bindings.get(scope, {}).get(name)
            if bindings:
                if scope is own:
                    prior = [b for b in bindings if b[0] <= use]
                    if prior:
                        return self._resolve(max(prior, key=lambda b: b[0]))
                else:
                    return self._resolve(max(bindings, key=lambda b: b[0]))
            scope = self._scopes.parent_of(scope)
        return None

    def _called_name(self, func: ast.expr) -> str | None:
        if isinstance(func, ast.Attribute):
            return func.attr
        return func.id if isinstance(func, ast.Name) else None

    def is_sdk_frame_call(self, expr: ast.expr, at: ast.AST) -> bool:
        if isinstance(expr, ast.Await):
            expr = expr.value
        if not isinstance(expr, ast.Call) or not isinstance(expr.func, ast.Attribute):
            return False
        attr = expr.func.attr
        if attr in _SDK_FRAME_ATTRS:
            return True
        return attr == "read" and self.is_reader(expr.func.value, at)

    def is_sdk_frame_iter_call(self, expr: ast.expr) -> bool:
        if isinstance(expr, ast.Await):
            expr = expr.value
        return (
            isinstance(expr, ast.Call)
            and isinstance(expr.func, ast.Attribute)
            and expr.func.attr in _SDK_FRAME_ITER_ATTRS
        )

    def is_frame(self, expr: ast.expr, at: ast.AST) -> bool:
        if isinstance(expr, ast.Await):
            expr = expr.value
        if isinstance(expr, ast.Name):
            return self.name_kind(expr.id, at) == _FRAME
        if not isinstance(expr, ast.Call):
            return False
        if self.is_sdk_frame_call(expr, at):
            return True
        func = expr.func
        if isinstance(func, ast.Attribute) and func.attr == "to_pandas":
            return True
        if self._aliases.is_pandas_factory(func):
            return True
        return self._called_name(func) in self._summary.frame_fns

    def is_frame_iter(self, expr: ast.expr, at: ast.AST) -> bool:
        if isinstance(expr, ast.Await):
            expr = expr.value
        if isinstance(expr, ast.Name):
            return self.name_kind(expr.id, at) == _FRAME_ITER
        if not isinstance(expr, ast.Call):
            return False
        if self.is_sdk_frame_iter_call(expr):
            return True
        return self._called_name(expr.func) in self._summary.frame_iter_fns

    def is_reader(self, expr: ast.expr, at: ast.AST) -> bool:
        if isinstance(expr, ast.Name):
            return self.name_kind(expr.id, at) == _READER
        if not isinstance(expr, ast.Call):
            return False
        func = expr.func
        if isinstance(func, ast.Name):
            return func.id in self._aliases.readers
        return isinstance(func, ast.Attribute) and func.attr in _SDK_READER_CLASSES


def _own_returns(func: ast.FunctionDef | ast.AsyncFunctionDef) -> list[ast.Return]:
    found: list[ast.Return] = []
    stack: list[ast.AST] = list(func.body)
    while stack:
        node = stack.pop()
        if isinstance(node, ast.Return):
            found.append(node)
        if isinstance(
            node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)
        ):
            continue
        stack.extend(ast.iter_child_nodes(node))
    return found


def summarize_frame_functions(trees: Iterable[ast.Module]) -> FrameSummary:
    """Collect the repo's functions that return pandas frames (one level)."""
    verdicts: dict[str, set[str | None]] = {}
    for tree in trees:
        aliases = _Aliases(tree)
        evidence = _PandasEvidence(tree, _ScopeMap(tree), aliases, _EMPTY_SUMMARY)
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            verdict: str | None = None
            if node.returns is not None:
                if _annotation_has_frame(node.returns, aliases):
                    verdict = _FRAME
                elif _annotation_iterates_frames(node.returns, aliases):
                    verdict = _FRAME_ITER
            else:
                for ret in _own_returns(node):
                    if ret.value is None:
                        continue
                    if evidence.is_sdk_frame_call(ret.value, ret):
                        verdict = _FRAME
                    elif evidence.is_sdk_frame_iter_call(ret.value):
                        verdict = _FRAME_ITER
            verdicts.setdefault(node.name, set()).add(verdict)
    return FrameSummary(
        frozenset(n for n, v in verdicts.items() if v == {_FRAME}),
        frozenset(n for n, v in verdicts.items() if v == {_FRAME_ITER}),
    )


def _flatten_types(node: ast.expr) -> list[ast.expr]:
    if isinstance(node, ast.Tuple):
        return [m for e in node.elts for m in _flatten_types(e)]
    members = _union_members(node)
    if members is not None:
        return [m for e in members for m in _flatten_types(e)]
    return [node]


def _in_pyarrow_isinstance_branch(
    call: ast.AST,
    receiver: ast.expr,
    parents: dict[ast.AST, ast.AST],
    aliases: _Aliases,
) -> bool:
    """Whether *call* sits in the body of ``if isinstance(receiver, pa.<Type>)``."""
    if not isinstance(receiver, ast.Name):
        return False
    child: ast.AST = call
    node = parents.get(call)
    while node is not None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            return False
        if isinstance(node, ast.If) and child in node.body:
            test = node.test
            if (
                isinstance(test, ast.Call)
                and isinstance(test.func, ast.Name)
                and test.func.id == "isinstance"
                and len(test.args) == 2
                and isinstance(test.args[0], ast.Name)
                and test.args[0].id == receiver.id
            ):
                members = _flatten_types(test.args[1])
                if members and all(aliases.is_pyarrow_type(m) for m in members):
                    return True
        child = node
        node = parents.get(node)
    return False


def scan_daft_runtime(
    tree: ast.Module,
    file: str,
    directives: dict[int, _IgnoreDirective],
    frame_summary: FrameSummary | None = None,
) -> list[Finding]:
    """Return B007 findings for *tree*.

    *frame_summary* names the repo's pandas-frame-returning functions; when it
    is ``None`` only *tree* itself is summarised.
    """
    if not _imports_sdk(tree):
        return []

    scopes = _ScopeMap(tree)
    aliases = _Aliases(tree)
    evidence = _PandasEvidence(
        tree,
        scopes,
        aliases,
        frame_summary
        if frame_summary is not None
        else summarize_frame_functions([tree]),
    )
    parents = {
        child: parent
        for parent in ast.walk(tree)
        for child in ast.iter_child_nodes(parent)
    }
    pyarrow_by_scope = _pyarrow_bindings_by_scope(tree, scopes)
    binding_count = sum(
        len(b) for names in pyarrow_by_scope.values() for b in names.values()
    )
    for _ in range(binding_count + 1):
        prev = pyarrow_by_scope
        ctx = _Ctx(
            lambda name, at, prev=prev: _is_pyarrow_bound(
                name, at, scopes, prev, strict=True
            ),
        )
        pyarrow_by_scope = _pyarrow_bindings_by_scope(tree, scopes, ctx)
        if pyarrow_by_scope == prev:
            break
    ctx = _Ctx(lambda name, at: _is_pyarrow_bound(name, at, scopes, pyarrow_by_scope))
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
                if _derives_from_pyarrow(receiver, node, ctx):
                    continue
                if not evidence.is_frame(receiver, node):
                    continue
                if _in_pyarrow_isinstance_branch(node, receiver, parents, aliases):
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
                and not _derives_from_pyarrow(receiver, node, ctx)
            ):
                _flag(node, ".names", "use frame.columns on the pandas frame")

    return findings
