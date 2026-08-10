"""O005 UnresolvedAppNamePlaceholder — flag a hardcoded, unsubstituted ``{app_name}`` token.

The AE-DAG-write path builds identifiers (task-queue names, workflow-input
fields, manifest values) that need ``app_name`` interpolated at write time.  A
plain string literal containing the single-brace token ``{app_name}`` — one
that is neither an f-string nor the receiver of a ``.format(app_name=...)``
call — freezes the literal token into whatever it's assigned to instead of the
real app name, e.g.::

    task_queue = "atlan-{app_name}-production"   # never interpolated

That exact shape shipped a queue no worker polls, hanging every child workflow
routed through it until the 24h heartbeat backstop killed it (CONNECT-183).
The substitution helper needed to fix it (``substitute_app_name_placeholder``)
has been independently hand-rolled at least four times across separate
codebases — Heracles (Go), native-migration-app, atlan-local-marketplace-app
(CONNECT-191, ``atlan-local-marketplace-app#539``), and atlan-hightouch-app
(ARUN-1039) — because no shared, obviously-discoverable utility exists yet.
This check doesn't require that utility to exist: it flags the *unresolved*
shape directly, so the next hand-authored template that skips substitution is
caught before it ships, rather than after a workflow hangs to its timeout.

Detection is shape-anchored, not name-anchored — no import or call target is
required, since the SDK provides nothing canonical to import yet:

* A string ``ast.Constant`` containing the literal substring ``{app_name}``.
* Not part of an ``ast.JoinedStr`` (an f-string already interpolates at parse
  time, so ``f"...{app_name}..."`` is never flagged).
* Not the receiver of a ``.format(...)`` call whose keywords include
  ``app_name=`` (a proper, already-resolving substitution site).
* Not a module/class/function docstring (the first statement of a
  ``Module``/``ClassDef``/``FunctionDef`` body, when it is a bare string
  expression) — documentation and doctest examples are out of scope.
"""

from __future__ import annotations

import ast

from conformance.suite.checks._ast_common import _IgnoreDirective, make_finding
from conformance.suite.schema.findings import Finding

_TOKEN = "{app_name}"
_MESSAGE = (
    "Hardcoded '{app_name}' left unsubstituted in a plain string literal — this "
    "freezes the literal token into whatever it's assigned to instead of the real "
    "app name (the exact shape that hung dbt:process on a dead task queue for 24h, "
    "CONNECT-183). Use an f-string or .format(app_name=...) to resolve it before "
    "the value is written/dispatched."
)


def _is_docstring_node(const: ast.Constant, tree: ast.AST) -> bool:
    """True if *const* is the bare string literal of a Module/ClassDef/FunctionDef docstring."""
    for node in ast.walk(tree):
        if not isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            continue
        body = node.body
        if not body:
            continue
        first = body[0]
        if (
            isinstance(first, ast.Expr)
            and first.value is const
            and isinstance(const.value, str)
        ):
            return True
    return False


def _resolving_format_receivers(tree: ast.AST) -> set[ast.Constant]:
    """String-literal receivers of a ``.format(...)`` call that keyword-binds ``app_name``."""
    receivers: set[ast.Constant] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr == "format"):
            continue
        if not any(kw.arg == "app_name" for kw in node.keywords):
            continue
        receiver = func.value
        if isinstance(receiver, ast.Constant) and isinstance(receiver.value, str):
            receivers.add(receiver)
    return receivers


def _joined_str_children(tree: ast.AST) -> set[ast.Constant]:
    """Constant nodes that are pieces of an f-string — never independently flagged."""
    pieces: set[ast.Constant] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr):
            for value in node.values:
                if isinstance(value, ast.Constant):
                    pieces.add(value)
    return pieces


def check_o005(
    tree: ast.AST,
    filename: str,
    directives: dict[int, _IgnoreDirective],
) -> list[Finding]:
    """Emit O005 for a plain string literal that still carries an unresolved '{app_name}' token."""
    findings: list[Finding] = []
    resolving_receivers = _resolving_format_receivers(tree)
    fstring_pieces = _joined_str_children(tree)

    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
            continue
        if _TOKEN not in node.value:
            continue
        if node in resolving_receivers or node in fstring_pieces:
            continue
        if _is_docstring_node(node, tree):
            continue
        findings.append(
            make_finding(
                filename=filename,
                rule_id="O005",
                node=node,
                message=_MESSAGE,
                directives=directives,
            )
        )
    return findings
