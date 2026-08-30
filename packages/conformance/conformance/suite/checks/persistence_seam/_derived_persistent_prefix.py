"""P048 AppDerivedPersistentArtifactPrefix — app builds an SDK-owned path itself.

Fires when a module spells the ``persistent-artifacts`` path segment out in a
string *and* imports nothing from ``application_sdk.common.incremental``.  See
``rules/persistence_seam.py`` for why this shape (and not "re-implements a
helper", which is not statically decidable) is the thing worth detecting.

Both signals are needed.  The literal alone fires on every app that legitimately
writes under the prefix; the missing import alone fires on most of the fleet.
Together they describe an app that reached the layout without going through the
function that owns it.
"""

from __future__ import annotations

import ast

from conformance.suite.checks._ast_common import _IgnoreDirective, make_finding
from conformance.suite.schema.findings import Finding

# The SDK-owned object-store root for connection-scoped cross-run state.
# Matched as a whole path segment, so "persistent-artifacts-backup" is not a hit.
_PREFIX_SEGMENT = "persistent-artifacts"

# The seam that owns the layout. Importing anything beneath it means the module
# is delegating, so it is not the target of this rule.
_SEAM_MODULE = "application_sdk.common.incremental"


def _imports_seam(tree: ast.AST) -> bool:
    """True if *tree* imports anything from the SDK's incremental seam.

    Covers ``import application_sdk.common.incremental.helpers``,
    ``from application_sdk.common.incremental import marker``, and
    ``from application_sdk.common.incremental.helpers import
    get_persistent_s3_prefix``.  Relative imports are ignored: an app cannot
    reach the SDK package relatively.
    """
    for node in ast.walk(tree):
        if isinstance(node, ast.Import) and any(
            _is_seam_module(alias.name) for alias in node.names
        ):
            return True
        if (
            isinstance(node, ast.ImportFrom)
            and node.level == 0
            and _is_seam_module(node.module or "")
        ):
            return True
    return False


def _is_seam_module(name: str) -> bool:
    """True if dotted module *name* is the seam or a module beneath it."""
    return name == _SEAM_MODULE or name.startswith(_SEAM_MODULE + ".")


def _literal_text(expr: ast.expr) -> str:
    """Return the *static* string content of *expr*, or ``""``.

    A plain ``str`` constant yields its value; an f-string yields its literal
    segments joined (runtime ``FormattedValue`` parts contribute nothing, which
    is what lets ``f"persistent-artifacts/{cqn}"`` still match).  Anything else
    yields the empty string.

    Deliberately narrower than the client-seam equivalent: ``+`` concatenation is
    not walked, because a path assembled that way still puts the segment in one
    of its own operands, and each operand is visited separately by the walk.
    """
    if isinstance(expr, ast.Constant):
        return expr.value if isinstance(expr.value, str) else ""
    if isinstance(expr, ast.JoinedStr):
        return "".join(
            part.value
            for part in expr.values
            if isinstance(part, ast.Constant) and isinstance(part.value, str)
        )
    return ""


def _has_prefix_segment(text: str) -> bool:
    """True if *text* contains ``persistent-artifacts`` as a whole path segment."""
    return _PREFIX_SEGMENT in text.split("/")


def _skipped_strings(tree: ast.AST) -> set[int]:
    """Node ids of string nodes the walk must not report on its own.

    Two sources:

    * **Statement strings** — a module/class/function docstring, or any bare
      string statement, is documentation rather than a path being built.  The
      pre-fix module in CONNECT-1136 documented the layout in its docstring
      *and* built it in code; only the second is a violation.  Comments never
      reach the AST at all, so the fleet's many ``# persistent-artifacts/...``
      explanatory notes cost nothing here.
    * **F-string segments** — ``ast.walk`` yields a ``JoinedStr`` *and* each
      literal ``Constant`` inside it.  The ``JoinedStr`` is the path expression;
      reporting its parts as well would double-count one path.
    """
    skipped: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Expr) and isinstance(
            node.value, ast.Constant | ast.JoinedStr
        ):
            skipped.add(id(node.value))
        elif isinstance(node, ast.JoinedStr):
            skipped.update(
                id(part) for part in node.values if isinstance(part, ast.Constant)
            )
    return skipped


def check_p048(
    tree: ast.AST,
    filename: str,
    directives: dict[int, _IgnoreDirective],
) -> list[Finding]:
    """Emit P048 for app-built ``persistent-artifacts`` paths outside the SDK seam."""
    if _imports_seam(tree):
        # The module goes through the SDK helper — accepted false-negative for a
        # file that both delegates and hand-rolls a second path (see the rule doc).
        return []

    skipped = _skipped_strings(tree)
    findings: list[Finding] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant | ast.JoinedStr):
            continue
        if id(node) in skipped:
            continue
        if not _has_prefix_segment(_literal_text(node)):
            continue
        findings.append(
            make_finding(
                filename=filename,
                rule_id="P048",
                node=node,
                message=(
                    "'persistent-artifacts' object-store path built in app code. This "
                    "layout is owned by the SDK: derive it from "
                    "application_sdk.common.incremental.helpers.get_persistent_s3_prefix"
                    "(connection_qualified_name, app_name), and read/write incremental "
                    "markers with fetch_marker_from_storage / persist_marker_to_storage "
                    "(application_sdk.common.incremental.marker). A local copy drifts "
                    "from the SDK on inputs your fixtures do not cover — see "
                    "CONNECT-1136, where an app raised on connection qualified names "
                    "the SDK warns and proceeds on. If this path is genuinely outside "
                    "what the SDK models (e.g. Argo-layout compatibility), justify it "
                    "with '# conformance: ignore[P048] <reason>'."
                ),
                directives=directives,
            )
        )
    return findings
