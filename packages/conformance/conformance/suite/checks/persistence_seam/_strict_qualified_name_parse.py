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

* takes a ``…qualified_name`` parameter,
* splits it apart itself (any ``.split(...)`` in the body), and
* can ``raise`` out of that body,

while the module imports nothing from ``application_sdk.common.incremental``.

The seam-import gate is what keeps the *fixed* shape silent: the post-fix module
in CONNECT-1136 still raises its own typed error, but only after delegating the
parse to the SDK and catching the SDK's error — which is correct, and is exactly
what the gate encodes.

``raise`` statements inside nested function definitions are not attributed to the
enclosing function; a nested helper is scanned in its own right if it takes the
parameter itself.
"""

from __future__ import annotations

import ast

from conformance.suite.checks._ast_common import _IgnoreDirective, make_finding
from conformance.suite.schema.findings import Finding

from ._derived_persistent_prefix import _imports_seam

# Parameter suffix identifying the value whose parsing the SDK owns.
#
# Deliberately ``connection_qualified_name`` and not the looser
# ``qualified_name``: the SDK helper governs the *connection* qualified name
# specifically.  A table/column/asset qualified name has a different owner and
# different segment semantics, and matching those produced a false positive on
# a mapper that splits an asset's qualifiedName and raises about an unrelated
# missing field.
_PARAM_SUFFIX = "connection_qualified_name"

_FUNC_DEFS = (ast.FunctionDef, ast.AsyncFunctionDef)


def _takes_qualified_name(func: ast.FunctionDef | ast.AsyncFunctionDef) -> str | None:
    """Return the first ``…qualified_name`` parameter of *func*, else ``None``."""
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
    if isinstance(expr, ast.Attribute):
        return _traces_to_param(expr.value, param)
    if isinstance(expr, ast.Subscript):
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
    if _imports_seam(tree):
        # Delegating to the SDK — a typed re-raise around the SDK's own error is
        # the correct shape, not a divergence.
        return []

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
                    "(application_sdk.common.incremental.helpers) warns and proceeds "
                    "when the last segment is not an epoch, so an app that raises on "
                    "the same input is more brittle than the SDK for connections whose "
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
