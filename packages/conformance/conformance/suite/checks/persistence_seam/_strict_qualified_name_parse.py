"""P049 StrictConnectionQualifiedNameParse — app raises where the SDK warns.

The companion to P048.  P048 catches an app that builds the SDK-owned
``persistent-artifacts`` layout itself; this one catches the *behavioural* half of
the same fork, which is what actually broke production in CONNECT-1136.

``extract_epoch_id_from_qualified_name`` deliberately **warns and proceeds** when
a connection qualified name's last segment is not an epoch::

    if not connection_id.isdigit():
        logger.warning("Connection ID %s is not purely numeric ...")
    return connection_id

An app that parses the same value itself and *raises* on the same input is
strictly more brittle than the SDK for no stated reason.  The two agree on every
qualified name the app's fixtures contain (all epoch-based) and disagree on the
ones they do not — so the app crawls fine and mines not at all, in whichever
tenant provisions connections programmatically, long after review.

Detection shape
---------------
A function that

* takes a ``connection_qualified_name`` parameter (that exact suffix — a table,
  column or asset qualified name has a different owner and different segment
  semantics),
* calls ``.split(...)`` on a value that traces back to that parameter, and
* can ``raise`` out of its own body,

**and does not delegate that parse to the SDK.**

Why the gate is per-function, not per-module
--------------------------------------------
An earlier cut skipped any module importing ``application_sdk.common.incremental``
wholesale.  That reads as delegation today, when almost no app module imports the
seam — but the whole point of P048 and the published seam is that they all
should.  A module-level gate therefore goes blind exactly as adoption succeeds,
and "module imports the seam *and* one function still hand-rolls a strict parse"
is the most likely shape of the next recurrence.  For a permanent BLOCK-tier
recurrence guard that is a hole, not a nit.

So delegation is judged per function: a function that calls one of the two seam
symbols owning *this* parse is deriving the value through the SDK and stays
silent — including the correct post-fix shape, which catches the SDK's typed
error and re-raises its own.  A sibling function in the same module that parses
by hand is still caught.

Which seam symbols count as delegation
--------------------------------------
Only ``get_persistent_s3_prefix`` and ``extract_epoch_id_from_qualified_name``.
Touching the seam anywhere is not the same as delegating this decision:
``fetch_marker_from_storage``, ``persist_marker_to_storage``,
``create_next_marker`` and ``process_marker_timestamp`` all take an
already-derived prefix or marker and say nothing about which segment of the
qualified name is the connection id.  Treating any call beneath the seam as
delegation would let the exact CONNECT-1136 shape through unremarked the moment
the offending function also read its marker via the SDK — which is precisely
what a half-migrated app looks like, and a hole in a BLOCK-tier recurrence guard.

``raise`` statements inside nested function definitions are not attributed to the
enclosing function; a nested helper is scanned in its own right if it takes the
parameter itself.
"""

from __future__ import annotations

import ast

from conformance.suite.checks._ast_common import (
    _IgnoreDirective,
    collect_import_origins,
    make_finding,
    qualify_chained_attr_call,
)
from conformance.suite.schema.findings import Finding

# Parameter suffix identifying the value whose parsing the SDK owns.
#
# Deliberately ``connection_qualified_name`` and not the looser
# ``qualified_name``: the SDK helper governs the *connection* qualified name
# specifically.  Matching the looser form produced a false positive on a mapper
# that splits an asset's qualifiedName and raises about an unrelated missing
# field.
_PARAM_SUFFIX = "connection_qualified_name"

# The package that owns this parse.
_SEAM_MODULE = "application_sdk.common.incremental"

# The two symbols beneath the seam that answer *this* question — "where does
# this connection's state live?" and "which segment is its id?".  A function
# calling one of them has handed the parse to the SDK.
#
# Deliberately not "anything under the seam": the marker helpers
# (``fetch_marker_from_storage``, ``persist_marker_to_storage``,
# ``create_next_marker``, ``process_marker_timestamp``) consume an
# already-derived prefix and leave the parse entirely to their caller, so a
# function that calls one and still splits the qualified name apart itself is
# a finding, not a delegation.
_PARSE_SEAM_SYMBOLS = frozenset(
    {
        "extract_epoch_id_from_qualified_name",
        "get_persistent_s3_prefix",
    }
)

_FUNC_DEFS = (ast.FunctionDef, ast.AsyncFunctionDef)


def _is_parse_seam_origin(origin: str) -> bool:
    """True if *origin* resolves to a seam symbol that owns this parse.

    Both the package re-export
    (``application_sdk.common.incremental.get_persistent_s3_prefix``) and the
    defining submodule (``…incremental.helpers.get_persistent_s3_prefix``)
    resolve here, since only the trailing symbol name is matched beneath the
    seam.  An ``as`` alias binds a different *name* but the same origin, so it
    resolves too.
    """
    if not origin.startswith(_SEAM_MODULE + "."):
        return False
    return origin.rsplit(".", 1)[-1] in _PARSE_SEAM_SYMBOLS


def _takes_qualified_name(func: ast.FunctionDef | ast.AsyncFunctionDef) -> str | None:
    """Return the first ``…connection_qualified_name`` parameter, else ``None``."""
    a = func.args
    for arg in (*a.posonlyargs, *a.args, *a.kwonlyargs):
        if arg.arg.endswith(_PARAM_SUFFIX):
            return arg.arg
    return None


def _own_body(func: ast.FunctionDef | ast.AsyncFunctionDef) -> list[ast.AST]:
    """Nodes belonging to *func* itself, not to a function nested inside it.

    A nested ``def`` is its own scope: its ``raise`` is not this function's
    control flow, and it is visited separately by the top-level walk if it takes
    the parameter in its own right.
    """
    own: list[ast.AST] = []
    stack: list[ast.AST] = list(func.body)
    while stack:
        node = stack.pop()
        if isinstance(node, (*_FUNC_DEFS, ast.Lambda)):
            continue
        own.append(node)
        stack.extend(ast.iter_child_nodes(node))
    return own


def _delegates_the_parse(body: list[ast.AST], origins: dict[str, str]) -> bool:
    """True if any call in *body* hands this parse to the SDK seam.

    Resolves the three call spellings ``collect_import_origins`` covers: a bare
    name (``get_persistent_s3_prefix(...)``), a single-level attribute on an
    imported module (``marker.fetch_marker_from_storage(...)``), and a bare
    dotted chain (``application_sdk.common.incremental.helpers.f(...)``).
    """
    for node in body:
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name):
            origin = origins.get(func.id)
        elif isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
            # ``helpers.get_persistent_s3_prefix()`` — resolve the receiver
            # through its import origin. qualify_chained_attr_call returns the
            # *as-written* path, which for an aliased module is not the real
            # one, so the single-level case is resolved here instead.
            base = origins.get(func.value.id)
            origin = f"{base}.{func.attr}" if base else None
        elif isinstance(func, ast.Attribute):
            # Bare dotted chain: ``application_sdk.common.incremental.helpers.f()``
            origin = qualify_chained_attr_call(func, origins)
        else:
            continue
        if origin and _is_parse_seam_origin(origin):
            return True
    return False


def _traces_to_param(expr: ast.expr, param: str) -> bool:
    """True if *expr* is built from *param* through call/attribute wrapping.

    Follows the shapes a qualified name is normalised through on its way to a
    ``.split`` — ``str(cqn)``, ``cqn.strip("/")``, ``str(cqn).strip("/")``,
    ``cqn[1:]`` — by walking down callees, call arguments, attribute receivers
    and subscript targets until it reaches a bare name.

    This is intentionally *not* a dataflow analysis: a name rebound through an
    intermediate local (``normalised = tidy(cqn)``; ``normalised.split("/")``)
    is not followed and is an accepted false-negative.  Requiring the receiver
    to trace syntactically to the parameter is what keeps the rule off functions
    that merely happen to split some *other* string.
    """
    if isinstance(expr, ast.Name):
        return expr.id == param
    if isinstance(expr, ast.Call):
        return _traces_to_param(expr.func, param) or any(
            _traces_to_param(a, param) for a in expr.args
        )
    if isinstance(expr, ast.Attribute | ast.Subscript):
        return _traces_to_param(expr.value, param)
    return False


def _splits_the_name(body: list[ast.AST], param: str) -> bool:
    """True if *body* calls ``.split(...)`` on something derived from *param*."""
    return any(
        isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == "split"
        and _traces_to_param(n.func.value, param)
        for n in body
    )


def check_p049(
    tree: ast.AST,
    filename: str,
    directives: dict[int, _IgnoreDirective],
) -> list[Finding]:
    """Emit P049 where an app parses a qualified name itself and raises on it."""
    origins = collect_import_origins(tree)
    findings: list[Finding] = []

    for func in ast.walk(tree):
        if not isinstance(func, _FUNC_DEFS):
            continue
        param = _takes_qualified_name(func)
        if param is None:
            continue
        body = _own_body(func)
        if not _splits_the_name(body, param):
            continue
        raises = [n for n in body if isinstance(n, ast.Raise)]
        if not raises:
            continue
        if _delegates_the_parse(body, origins):
            # This function derives the value through the SDK; a typed re-raise
            # around the SDK's own error is the correct shape, not a divergence.
            continue
        # Anchor at the earliest raise in source order: ``_own_body`` walks with a
        # stack, so its order is not the file's, and a SARIF fingerprint that
        # moved between runs would defeat suppression and de-duplication.
        anchor = min(raises, key=lambda n: (n.lineno, n.col_offset))
        findings.append(
            make_finding(
                filename=filename,
                rule_id="P049",
                node=anchor,
                message=(
                    f"'{func.name}' parses {param} itself and raises on it. The SDK's "
                    "extract_epoch_id_from_qualified_name "
                    "(application_sdk.common.incremental) warns and proceeds when the "
                    "last segment is not an epoch, so an app that raises on the same "
                    "input is more brittle than the SDK for connections whose "
                    "qualified name ends in a name rather than a timestamp — the "
                    "CONNECT-1136 failure. Derive the value through the SDK "
                    "(get_persistent_s3_prefix / extract_epoch_id_from_qualified_name) "
                    "and let it decide what is fatal. If this stricter contract is "
                    "deliberate, justify it with "
                    "'# conformance: ignore[P049] <reason>'."
                ),
                directives=directives,
            )
        )
    return findings
