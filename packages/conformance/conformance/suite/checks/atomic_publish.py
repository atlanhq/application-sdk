"""P048 NonAtomicDestinationWrite — in-place ``O_TRUNC`` write, no atomic publish.

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
  **at its own level** (nested function bodies belong to their own scope).  A
  scope that publishes via rename is the temp-then-replace pattern this rule
  exists to steer toward, so it passes; requiring dataflow proof that the
  *same* path is renamed would trade a zero-noise heuristic for a solver.

Own-level matters: counting a whole subtree would let one atomic helper
elsewhere in a module (a checkpoint writer with its own ``os.replace``) clear
every violating ``os.open`` in the file.  Counting enclosing scopes still
clears the closure pattern, where workers write through a descriptor and the
outer function publishes.

Ceiling: ``open(path, "wb")`` and ``Path.write_bytes`` also write in place but
are dominated by sanctioned uses (atomic-staging internals, append writers,
tooling output) — see the rule definition in ``suite.rules.atomic_publish``.

Inline suppression
------------------
``# conformance: ignore[P048] <reason>`` on the offending line, or on the
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
RULE_ID = "P048"

__all__ = ["RULE_ID", "SERIES", "discover", "main", "scan_path", "scan_text"]

_MESSAGE = (
    "os.open with O_TRUNC truncates the destination at open time and fills it "
    "in place, so a concurrent reader of the same path sees a partial file at "
    "the artifact's real name (the CONNECT-1126 corruption class — shared "
    "FileReference.local_path, JSONL parse failure at char 0). Write to a "
    "staging file (common.atomic helpers, or a temp inside PARTIAL_DIRNAME) "
    "and publish with os.replace. Suppress a reviewed single-consumer "
    "exception with '# conformance: ignore[P048] <reason>'."
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


def _scope_publishes(scope: ast.AST) -> bool:
    """True if *scope* calls ``os.replace`` / ``os.rename`` at its own level.

    Nested function bodies are their own scopes and are not descended into —
    a checkpoint writer's ``os.replace`` elsewhere in the module must not
    clear a violating ``os.open`` in a sibling function.
    """
    pending = list(ast.iter_child_nodes(scope))
    while pending:
        node = pending.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if isinstance(node, ast.Call) and (
            _is_os_call(node.func, "replace") or _is_os_call(node.func, "rename")
        ):
            return True
        pending.extend(ast.iter_child_nodes(node))
    return False


def _scope_tainted_names(scope: ast.AST) -> frozenset[str]:
    """Names assigned an ``O_TRUNC``-mentioning expression at *scope*'s level.

    Straight-line, flow-insensitive: a name is tainted if any own-level
    ``=`` / ``:=`` / ``|=`` binds it to an expression that mentions
    ``O_TRUNC``.  Reassignment to a clean value does not untaint — for a
    flag word that split is contrived, and the conservative reading errs
    toward the finding, where a suppression can record the review.
    """
    tainted: set[str] = set()
    pending = list(ast.iter_child_nodes(scope))
    while pending:
        node = pending.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
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
        pending.extend(ast.iter_child_nodes(node))
    return frozenset(tainted)


class _AtomicPublishChecker(ast.NodeVisitor):
    """Walk a module AST and emit P048 findings."""

    def __init__(self, filename: str, directives: dict[int, _IgnoreDirective]) -> None:
        self._filename = filename
        self._directives = directives
        self.findings: list[Finding] = []
        self._publish_stack: list[bool] = []
        self._tainted_stack: list[frozenset[str]] = []

    def visit_Module(self, node: ast.Module) -> None:
        self._walk_scope(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._walk_scope(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._walk_scope(node)

    def _walk_scope(self, node: ast.AST) -> None:
        self._publish_stack.append(_scope_publishes(node))
        self._tainted_stack.append(_scope_tainted_names(node))
        self.generic_visit(node)
        self._publish_stack.pop()
        self._tainted_stack.pop()

    def visit_Call(self, node: ast.Call) -> None:
        tainted = (
            frozenset().union(*self._tainted_stack)
            if self._tainted_stack
            else frozenset()
        )
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
    """Scan a single Python source *text* for P048 findings."""
    try:
        tree = ast.parse(text, filename=file)
    except SyntaxError:
        return []
    checker = _AtomicPublishChecker(filename=file, directives=_parse_directives(text))
    checker.visit(tree)
    return checker.findings


def scan_path(path: Path, root: Path) -> list[Finding]:
    """Scan a single Python file for P048 findings."""
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
    description="P048: flag in-place os.open(O_TRUNC) writes with no os.replace publish.",
    discover=discover,
)


if __name__ == "__main__":
    sys.exit(main())
