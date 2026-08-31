"""P048 AppDerivedPersistentArtifactPrefix — app builds an SDK-owned path itself.

Fires when app code assembles the **connection-scoped** persistent-artifacts
layout — ``persistent-artifacts/apps/<app>/connection/…`` — rather than deriving
it from ``get_persistent_s3_prefix``.  See ``rules/persistence_seam.py`` for why
this shape (and not "re-implements a helper", which is not statically decidable)
is the thing worth detecting.

Matching the *layout*, not the root segment
-------------------------------------------
An earlier cut of this check matched the bare ``persistent-artifacts`` root
segment.  Measured across the fleet that fired 65 times, of which **one** was the
connection layout; the rest were paths the SDK helper cannot produce and does not
own — ``apps/<app>/state/…``, ``apps/<app>/workflows/<id>/config.json``, and the
Argo-compatibility ``{cqn}/parquet/markers/<phase>``.  For those the finding's
prescribed remedy is inapplicable, so the only available action was a suppression
in someone else's repo.  A rule whose remedy does not apply teaches people to
reach for ``# conformance: ignore`` reflexively, which is what makes the next
real finding easy to wave through.

So the match is the segment *sequence* the helper actually produces, which drops
all four other shapes at their fourth segment.

Assembling the path
-------------------
The path is matched across the whole expression, not within a single literal.
The CONNECT-1136 defect built its key as::

    "/".join(["persistent-artifacts", "apps", resolved_app, "connection", ...])

where no individual string carries the layout — the only literal in that file
containing both ``apps`` and ``connection`` is its *docstring*.  A check that
tested one literal at a time would miss the very defect it exists for, so
``_assemble`` reconstructs the path across ``str.join``, f-strings and ``+``
concatenation, with runtime pieces standing in as a wildcard segment.

No delegation gate at all
-------------------------
Unlike ``P049``, which exempts a function that derives the value through
``get_persistent_s3_prefix`` / ``extract_epoch_id_from_qualified_name``, this
check has no such exemption — and in particular does not skip modules that
import ``application_sdk.common.incremental``.  An earlier seam-import gate
existed to compensate for the loose root-segment match; with the layout matched
precisely it would only create a blind spot, because a module that imports the
seam *and* still hand-rolls the connection layout is exactly a finding worth
making.  ``test_p048_fires_even_when_module_imports_the_seam`` is the contract.
"""

from __future__ import annotations

import ast

from conformance.suite.checks._ast_common import _IgnoreDirective, make_finding
from conformance.suite.schema.findings import Finding

# The SDK-owned object-store layout for connection-scoped cross-run state:
# ``persistent-artifacts/apps/{application_name}/connection/{connection_id}``.
# Matched as whole path segments, so ``persistent-artifacts-backup`` is a
# different directory and not a hit.
_ROOT_SEGMENT = "persistent-artifacts"
_APPS_SEGMENT = "apps"
_CONNECTION_SEGMENT = "connection"

# Stands in for a path piece whose value is only known at runtime, so an
# f-string or a joined variable still contributes one segment to the shape.
_WILDCARD = "\x00"


def _assemble(expr: ast.expr) -> str | None:
    """Reconstruct the static path text of *expr*, or ``None`` if it is not one.

    Runtime pieces become :data:`_WILDCARD` so they still occupy a segment:
    ``f"persistent-artifacts/apps/{app}/connection"`` keeps its four-segment
    shape.  Handles the shapes apps actually build paths with — a literal, an
    f-string, ``+`` concatenation, and ``<sep>.join([...])``.

    Not a dataflow analysis: a path split across statements
    (``base = "persistent-artifacts/apps"``; ``f"{base}/x/connection"``) is not
    followed and is an accepted false-negative.
    """
    if isinstance(expr, ast.Constant):
        return expr.value if isinstance(expr.value, str) else None

    if isinstance(expr, ast.JoinedStr):
        return "".join(
            part.value
            if isinstance(part, ast.Constant) and isinstance(part.value, str)
            else _WILDCARD
            for part in expr.values
        )

    if isinstance(expr, ast.BinOp) and isinstance(expr.op, ast.Add):
        left, right = _assemble(expr.left), _assemble(expr.right)
        if left is None and right is None:
            return None
        return (left or _WILDCARD) + (right or _WILDCARD)

    if isinstance(expr, ast.Call):
        return _assemble_join(expr)

    return None


def _assemble_join(node: ast.Call) -> str | None:
    """Reconstruct ``<separator>.join([...])`` when the separator is a literal."""
    func = node.func
    if not isinstance(func, ast.Attribute) or func.attr != "join":
        return None
    sep = func.value
    if not (isinstance(sep, ast.Constant) and isinstance(sep.value, str)):
        return None
    if len(node.args) != 1 or not isinstance(node.args[0], ast.List | ast.Tuple):
        return None
    return sep.value.join(
        _assemble(element) or _WILDCARD for element in node.args[0].elts
    )


def _is_connection_layout(text: str) -> bool:
    """True if *text* carries the SDK's connection-scoped segment sequence.

    ``persistent-artifacts / apps / <any> / connection`` — the application-name
    segment may be anything, including a runtime wildcard.  Anything that
    diverges at the fourth segment (``state``, ``workflows``, ``skills``) or at
    the second (the Argo ``{cqn}/parquet/...`` layout) is a path the SDK helper
    cannot produce and does not own.
    """
    segments = text.split("/")
    return any(
        segments[i] == _ROOT_SEGMENT
        and segments[i + 1] == _APPS_SEGMENT
        and segments[i + 3] == _CONNECTION_SEGMENT
        for i in range(len(segments) - 3)
    )


def _statement_strings(tree: ast.AST) -> set[int]:
    """Node ids of strings that are whole statements — docstrings and no-ops.

    A module/class/function docstring documents the layout rather than building
    it.  The pre-fix module in CONNECT-1136 did both, and only the second is a
    violation.  Comments never reach the AST at all, so the fleet's explanatory
    ``# persistent-artifacts/...`` notes cost nothing here.
    """
    return {
        id(node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Constant | ast.JoinedStr)
    }


def check_p048(
    tree: ast.AST,
    filename: str,
    directives: dict[int, _IgnoreDirective],
) -> list[Finding]:
    """Emit P048 where app code assembles the SDK's connection-scoped layout."""
    docstrings = _statement_strings(tree)
    findings: list[Finding] = []

    # Explicit walk rather than ast.walk: once a node assembles into a path, its
    # children are pieces of that same path and must not be reported again.
    stack: list[ast.AST] = [tree]
    while stack:
        node = stack.pop()
        assembled = (
            _assemble(node)
            if isinstance(node, ast.expr) and id(node) not in docstrings
            else None
        )
        if assembled is None:
            stack.extend(ast.iter_child_nodes(node))
            continue
        if _is_connection_layout(assembled):
            findings.append(
                make_finding(
                    filename=filename,
                    rule_id="P048",
                    node=node,
                    message=(
                        "The connection-scoped persistent-artifacts layout "
                        "('persistent-artifacts/apps/<app>/connection/...') is built "
                        "here instead of derived from the SDK. Use "
                        "application_sdk.common.incremental.get_persistent_s3_prefix"
                        "(connection_qualified_name, app_name) — the same call the "
                        "crawler's marker goes through — so both watermarks for a "
                        "connection land in one directory and cannot drift apart. A "
                        "local copy diverges on inputs your fixtures do not cover; see "
                        "CONNECT-1136. If this path is deliberately outside what the "
                        "SDK models, justify it with "
                        "'# conformance: ignore[P048] <reason>'."
                    ),
                    directives=directives,
                )
            )
    return findings
