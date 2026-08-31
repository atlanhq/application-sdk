"""P050 NonAtomicDestinationWrite — in-place ``O_TRUNC`` write, no atomic publish.

``os.open(path, ... | os.O_TRUNC ...)`` truncates the destination at open time
and streams bytes into it in place.  A concurrent reader of the same path —
another activity materialising a shared ``FileReference.local_path``, a parser
already holding the file — observes a truncated or zero-filled file at the
artifact's real name.  That is the mechanism behind a production RCA
(CONNECT-1126): two concurrent downloads of one shared ``local_path``, both
reporting success, and a JSONL parser failing at line 1 column 1 on NUL bytes.

The sanctioned pattern is the FND-318 doctrine: write to a staging file (the
``common.atomic`` helpers, or an explicit temp inside ``PARTIAL_DIRNAME``) and
publish with ``os.replace``.

Matching
--------
A call is flagged when **both** hold:

* the callee is ``os.open`` (receiver-anchored — the builtin ``open`` returns a
  file object, not a descriptor, and is a different hazard class), and its
  argument subtree mentions ``O_TRUNC`` (``os.O_TRUNC`` or a bare imported
  ``O_TRUNC``) — directly, or through a local name assigned an
  ``O_TRUNC``-mentioning expression in an enclosing scope (the
  ``flags = ... | os.O_TRUNC`` then ``os.open(path, flags)`` split, which is
  exactly how the incident's chunked download spelled it);
* no enclosing scope — the nearest function, any outer function, or the module
  body for a top-level call — contains an ``os.replace`` / ``os.rename`` call
  **at its own level**.  A scope that publishes via rename is the
  temp-then-replace pattern this rule exists to steer toward, so it passes;
  requiring dataflow proof that the *same* path is renamed would trade a
  zero-noise heuristic for a solver.

"Own level" governs only what counts as a scope *publishing*: every construct
with a namespace of its own — nested ``def`` / ``async def`` / ``lambda``
bodies, ``class`` bodies, and the four comprehension forms — is excluded from
it, so one atomic helper elsewhere in a module (a checkpoint writer with its
own ``os.replace``) cannot clear a violating ``os.open`` in a sibling
function.
Clearance, by contrast, is inherited *inward*: a publish in any enclosing
scope clears calls in nested defs and lambdas alike.  That is the closure
allowance — workers writing through a descriptor while the outer function
publishes — and it applies uniformly, so an ``O_TRUNC`` open inside a nested
def or lambda whose outer function publishes is deliberately not flagged
(the same no-solver ceiling as above).

Ceiling: ``open(path, "wb")`` and ``Path.write_bytes`` also write in place but
are dominated by sanctioned uses (atomic-staging internals, append writers,
tooling output) — see the rule definition in ``suite.rules.atomic_publish``.

Inline suppression
------------------
``# conformance: ignore[P050] <reason>`` on the offending line, or on the
comment-only line directly above it.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

from conformance.suite.checks._ast_common import (
    _IgnoreDirective,
    _parse_directives,
    discover,
    make_cli_main,
    make_finding,
    safe_read_text,
)
from conformance.suite.schema.findings import Finding

SERIES = "P"
RULE_ID = "P050"

__all__ = ["RULE_ID", "SERIES", "discover", "main", "scan_path", "scan_text"]

_MESSAGE = (
    "os.open with O_TRUNC truncates the destination at open time and fills it "
    "in place, so a concurrent reader of the same path sees a partial file at "
    "the artifact's real name (the CONNECT-1126 corruption class — shared "
    "FileReference.local_path, JSONL parse failure at char 0). Write to a "
    "staging file (common.atomic helpers, or a temp inside PARTIAL_DIRNAME) "
    "and publish with os.replace. Suppress a reviewed single-consumer "
    "exception with '# conformance: ignore[P050] <reason>'."
)


def _is_os_call(func: ast.expr, name: str) -> bool:
    """True if *func* is the attribute ``os.<name>``."""
    return (
        isinstance(func, ast.Attribute)
        and func.attr == name
        and isinstance(func.value, ast.Name)
        and func.value.id == "os"
    )


def _expr_mentions_o_trunc(expr: ast.expr, tainted: frozenset[str]) -> bool:
    """True if *expr* references ``O_TRUNC`` — dotted, bare, or via a name in
    *tainted* (a local assigned an ``O_TRUNC``-mentioning expression)."""
    for sub in ast.walk(expr):
        if isinstance(sub, ast.Attribute) and sub.attr == "O_TRUNC":
            return True
        if isinstance(sub, ast.Name) and (sub.id == "O_TRUNC" or sub.id in tainted):
            return True
    return False


def _call_mentions_o_trunc(node: ast.Call, tainted: frozenset[str]) -> bool:
    """True if any argument of *node* references ``O_TRUNC`` (see above)."""
    return any(
        _expr_mentions_o_trunc(arg, tainted)
        for arg in [*node.args, *[kw.value for kw in node.keywords]]
    )


#: Every construct that introduces a namespace of its own in Python, and so
#: cannot count toward an enclosing scope's "own level".  Functions and
#: lambdas are the obvious members; ``class`` bodies and the four
#: comprehension forms are the easily-missed ones, and omitting them is a
#: false *negative* — an ``os.replace`` inside a nested class body or a
#: generator expression would clear a violating ``os.open`` in the enclosing
#: function, which is exactly the "atomic helper elsewhere clears a sibling"
#: leak the own-level rule exists to close.  ``ast.comprehension`` itself is
#: NOT here: it is the ``for`` clause of the four nodes below, not a scope.
_OWN_SCOPE_NODES: tuple[type[ast.AST], ...] = (
    ast.FunctionDef,
    ast.AsyncFunctionDef,
    ast.Lambda,
    ast.ClassDef,
    ast.ListComp,
    ast.SetComp,
    ast.DictComp,
    ast.GeneratorExp,
)


#: The four comprehension forms, which share an evaluation quirk the helpers
#: below all have to respect.
_COMPREHENSION_NODES: tuple[type[ast.AST], ...] = (
    ast.ListComp,
    ast.SetComp,
    ast.DictComp,
    ast.GeneratorExp,
)


def _comprehension_outer_iter(node: ast.AST) -> ast.expr | None:
    """The one sub-expression of a comprehension evaluated in the ENCLOSING scope.

    Python evaluates the first generator's iterable eagerly, in the scope that
    contains the comprehension and *before* any comprehension target is bound
    — which is why ``[x for f in g(f)]`` reads the outer ``f``.  Every later
    iterable, the ``if`` clauses and the element expression evaluate inside the
    comprehension, with the targets bound.

    So the first iterable has to be analysed with the enclosing scope's taint
    and publish state rather than the comprehension's.  Folding it into the
    comprehension lets a colliding target name shadow a genuinely tainted outer
    name, and the violation is missed.
    """
    if isinstance(node, _COMPREHENSION_NODES) and node.generators:
        return node.generators[0].iter
    return None


def _own_level_nodes(scope: ast.AST) -> list[ast.AST]:
    """Every node in *scope*'s subtree, excluding nested scopes.

    Nested ``def`` / ``async def`` / ``lambda`` / ``class`` bodies and
    comprehensions are their own scopes (:data:`_OWN_SCOPE_NODES`) — a
    checkpoint writer's ``os.replace`` elsewhere in the module must not clear
    a violating ``os.open`` in a sibling function, and the own-level invariant
    is encoded here once for both scope analyses below.
    """
    nodes: list[ast.AST] = []
    # When *scope* is itself a comprehension, its first iterable evaluates one
    # level out and is not part of this scope's own level.
    escaping = _comprehension_outer_iter(scope)
    pending = list(ast.iter_child_nodes(scope))
    while pending:
        node = pending.pop()
        if node is escaping:
            continue
        if isinstance(node, _OWN_SCOPE_NODES):
            # ...and by the same rule, a *nested* comprehension's first
            # iterable evaluates here, so it stays part of this scope even
            # though the comprehension body does not.
            inner = _comprehension_outer_iter(node)
            if inner is not None:
                pending.append(inner)
            continue
        nodes.append(node)
        pending.extend(ast.iter_child_nodes(node))
    return nodes


def _scope_publishes(scope: ast.AST) -> bool:
    """True if *scope* calls ``os.replace`` / ``os.rename`` at its own level."""
    return any(
        isinstance(node, ast.Call)
        and (_is_os_call(node.func, "replace") or _is_os_call(node.func, "rename"))
        for node in _own_level_nodes(scope)
    )


def _scope_tainted_names(scope: ast.AST) -> frozenset[str]:
    """Names assigned an ``O_TRUNC``-mentioning expression at *scope*'s level.

    Straight-line, flow-insensitive: a name is tainted if any own-level
    ``=`` / ``:=`` / ``|=`` binds it to an expression that mentions
    ``O_TRUNC``.  Reassignment to a clean value does not untaint — for a
    flag word that split is contrived, and the conservative reading errs
    toward the finding, where a suppression can record the review.
    """
    tainted: set[str] = set()
    for node in _own_level_nodes(scope):
        targets: list[ast.expr] = []
        value: ast.expr | None = None
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
            targets, value = [node.target], node.value
        elif isinstance(node, ast.NamedExpr):
            targets, value = [node.target], node.value
        if value is not None and _expr_mentions_o_trunc(value, frozenset()):
            for target in targets:
                if isinstance(target, ast.Name):
                    tainted.add(target.id)
    return frozenset(tainted)


def _scope_shadowed_names(scope: ast.AST) -> frozenset[str]:
    """Names *scope* re-binds cleanly, shedding any outer-scope taint.

    A function's parameters and its own-level bindings to expressions that do
    NOT mention ``O_TRUNC`` shadow an outer tainted name of the same spelling
    — ``def f(p, flags)`` under a module-level ``flags = ... | os.O_TRUNC``
    must not inherit the module's taint.
    """
    shadowed: set[str] = set()
    if isinstance(scope, _COMPREHENSION_NODES):
        # `[os.open(p, flags) for flags in candidates]` binds `flags` here, so
        # a module-level tainted `flags` of the same spelling does not reach in.
        # These bindings do NOT cover the first generator's iterable, which is
        # evaluated before any of them exist — the visitor walks that sub-tree
        # under the enclosing frame instead (see _comprehension_outer_iter).
        for generator in scope.generators:
            for sub in ast.walk(generator.target):
                if isinstance(sub, ast.Name):
                    shadowed.add(sub.id)
    if isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
        args = scope.args
        for arg in [
            *args.posonlyargs,
            *args.args,
            *args.kwonlyargs,
            *([args.vararg] if args.vararg else []),
            *([args.kwarg] if args.kwarg else []),
        ]:
            shadowed.add(arg.arg)
    for node in _own_level_nodes(scope):
        targets: list[ast.expr] = []
        value: ast.expr | None = None
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
            targets, value = [node.target], node.value
        elif isinstance(node, ast.NamedExpr):
            targets, value = [node.target], node.value
        if value is not None and not _expr_mentions_o_trunc(value, frozenset()):
            for target in targets:
                if isinstance(target, ast.Name):
                    shadowed.add(target.id)
    return frozenset(shadowed)


class _AtomicPublishChecker(ast.NodeVisitor):
    """Walk a module AST and emit P050 findings."""

    def __init__(self, filename: str, directives: dict[int, _IgnoreDirective]) -> None:
        self._filename = filename
        self._directives = directives
        self.findings: list[Finding] = []
        self._publish_stack: list[bool] = []
        self._tainted_stack: list[frozenset[str]] = []
        self._shadow_stack: list[frozenset[str]] = []

    def visit_Module(self, node: ast.Module) -> None:
        self._walk_scope(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._walk_scope(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._walk_scope(node)

    def visit_Lambda(self, node: ast.Lambda) -> None:
        self._walk_scope(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._walk_scope(node)

    # The four comprehension forms each evaluate in a scope of their own, so
    # they get a frame for the same reason lambdas do: a comprehension target
    # must shadow an outer tainted name of the same spelling rather than
    # inherit its taint.
    def visit_ListComp(self, node: ast.ListComp) -> None:
        self._walk_comprehension(node)

    def visit_SetComp(self, node: ast.SetComp) -> None:
        self._walk_comprehension(node)

    def visit_DictComp(self, node: ast.DictComp) -> None:
        self._walk_comprehension(node)

    def visit_GeneratorExp(self, node: ast.GeneratorExp) -> None:
        self._walk_comprehension(node)

    def _walk_comprehension(self, node: ast.AST) -> None:
        """Walk a comprehension, splitting off the sub-expression that isn't in it.

        The first generator's iterable is evaluated in the *enclosing* scope,
        before any target is bound (see :func:`_comprehension_outer_iter`), so
        it is visited with the current frame still on top. Everything else —
        later iterables, the ``if`` clauses, the element — is visited under
        the comprehension's own frame, where the targets shadow.

        Without the split, ``[x for flags in [os.open(p, flags)] for x in y]``
        under a tainted outer ``flags`` reads as shadowed and the violation is
        silently dropped.
        """
        escaping = _comprehension_outer_iter(node)
        if escaping is not None:
            self.visit(escaping)

        self._publish_stack.append(_scope_publishes(node))
        self._tainted_stack.append(_scope_tainted_names(node))
        self._shadow_stack.append(_scope_shadowed_names(node))
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.comprehension):
                for sub in ast.iter_child_nodes(child):
                    if sub is not escaping:
                        self.visit(sub)
            else:
                self.visit(child)
        self._publish_stack.pop()
        self._tainted_stack.pop()
        self._shadow_stack.pop()

    def _walk_scope(self, node: ast.AST) -> None:
        self._publish_stack.append(_scope_publishes(node))
        self._tainted_stack.append(_scope_tainted_names(node))
        self._shadow_stack.append(_scope_shadowed_names(node))
        self.generic_visit(node)
        self._publish_stack.pop()
        self._tainted_stack.pop()
        self._shadow_stack.pop()

    def _effective_taint(self) -> frozenset[str]:
        """Outer-to-inner taint with shadowing: a scope's clean re-binding of a
        name sheds outer taint; its own taint (applied after) always wins."""
        effective: set[str] = set()
        for tainted, shadowed in zip(self._tainted_stack, self._shadow_stack):
            effective -= shadowed
            effective |= tainted
        return frozenset(effective)

    def visit_Call(self, node: ast.Call) -> None:
        tainted = self._effective_taint()
        if (
            _is_os_call(node.func, "open")
            and _call_mentions_o_trunc(node, tainted)
            and not any(self._publish_stack)
        ):
            self.findings.append(
                make_finding(
                    filename=self._filename,
                    rule_id=RULE_ID,
                    node=node,
                    message=_MESSAGE,
                    directives=self._directives,
                )
            )
        self.generic_visit(node)


def scan_text(text: str, file: str) -> list[Finding]:
    """Scan a single Python source *text* for P050 findings."""
    try:
        tree = ast.parse(text, filename=file)
    except SyntaxError:
        return []
    checker = _AtomicPublishChecker(filename=file, directives=_parse_directives(text))
    checker.visit(tree)
    return checker.findings


def scan_path(path: Path, root: Path) -> list[Finding]:
    """Scan a single Python file for P050 findings."""
    text = safe_read_text(path)
    if text is None:
        return []
    try:
        rel = path.relative_to(root)
    except ValueError:
        rel = path
    return scan_text(text, str(rel))


main = make_cli_main(
    scan_text,
    description="P050: flag in-place os.open(O_TRUNC) writes with no os.replace publish.",
    discover=discover,
)


if __name__ == "__main__":
    sys.exit(main())
