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

**Canonical fix.** Resolve the token where the value is built: an f-string or
``.format(app_name=...)`` when the app name is already in scope, or the shared
substitution helper (``application_sdk.common.task_queue`` —
``derive_task_queue`` applies the naming rule, ``resolve_manifest_tokens``
reconciles a served manifest against the queue the worker actually polls) once
the SDK release that ships FND-195 is available. Prefer those over
re-implementing the substitution — the helper this rule's earliest revisions
described as missing has been independently hand-rolled at least four times
across separate codebases (Heracles in Go, native-migration-app,
atlan-local-marketplace-app per CONNECT-191, atlan-hightouch-app per
ARUN-1039), and one of those shipped a double prefix (DISTR-834). This check
still flags the *unresolved shape* directly rather than the absence of that
import, because the writers that most need catching are hand-authored templates
outside the SDK, where nothing is imported at all.

Detection anchors on the token **reaching a value**, not merely appearing in the
source. A ``{app_name}`` that only ever appears in prose or in a diagnostic
cannot freeze into an identifier, and flagging it trains people to suppress the
rule. Concretely, a string ``ast.Constant`` containing ``{app_name}`` is flagged
unless it is:

* part of an ``ast.JoinedStr`` whose **runtime value cannot carry the token** —
  an f-string interpolates at parse time, so ``f"...{app_name}..."`` is never
  flagged. ``f"...{{app_name}}..."`` is: the escaped braces evaluate to the
  literal runtime text ``{app_name}``, which freezes into the identifier
  exactly like a plain literal;
* the receiver of a ``.format(...)`` call whose keywords include ``app_name=``
  (a proper, already-resolving substitution site);
* **documentation** — the value of any bare string expression statement. This
  covers module, class and function docstrings *and* PEP 257 attribute
  docstrings (a bare string after a field annotation), which are not the first
  statement of their class body and so were previously flagged;
* **diagnostic text** — inside the arguments of a logging call
  (``logger.error(...)`` and friends), ``warnings.warn(...)``, or a ``raise``.
  Code that reports on the token necessarily quotes it, and the message is
  never dispatched as an identifier;
* **a declared token sentinel or message constant** — bound to an ``ALL_CAPS``
  name where either the literal is exactly the token (i.e. the definition of
  the token itself, such as ``APP_NAME_TOKEN = "{app_name}"``) or the name
  reads as prose — one delimited segment *is* a prose word
  (``START_MESSAGE``, ``VALIDATION_RATIONALE``). An ``ALL_CAPS`` constant
  holding a genuine queue *template* (``TASK_QUEUE = "atlan-{app_name}-prod"``)
  is still flagged, and so is one whose name merely *contains* a prose fragment
  without a delimited boundary (``MESSAGE_QUEUE``, ``HELP_QUEUE`` — queue
  templates, not message text).

The last three exclusions exist because without them the rule fires on the very
module that implements the correct behaviour: ``common/task_queue.py`` defines
the token it substitutes, documents its resolution fields as attribute
docstrings, and ``handler/service.py`` logs at WARNING/ERROR naming the token it
could not resolve. A rule that flags the fix is a rule people turn off.
"""

from __future__ import annotations

import ast

from conformance.suite.checks._ast_common import _IgnoreDirective, make_finding
from conformance.suite.schema.findings import Finding

_TOKEN = "{app_name}"

# Built from _TOKEN rather than spelled inline: a literal "{app_name}" here
# would make this module trip its own rule (the string is neither a docstring
# nor a diagnostic argument, and _MESSAGE's exclusion should not have to carry
# the checker's own source).
#
# The runtime SARIF message leads with fixes that work on any SDK version and
# names the shared helper as upcoming: application_sdk.common.task_queue ships
# with FND-195, which is not yet on main, so the message must not send users to
# an import that does not exist in their release.
_MESSAGE = (
    f"Hardcoded '{_TOKEN}' left unsubstituted in a plain string literal — this "
    "freezes the literal token into whatever it's assigned to instead of the real "
    "app name (the exact shape that hung dbt:process on a dead task queue for 24h, "
    "CONNECT-183). Resolve it with an f-string or .format(app_name=...) if the "
    "value is already in scope; a shared substitution helper "
    "(application_sdk.common.task_queue — derive_task_queue / "
    "resolve_manifest_tokens) lands in the SDK release that ships FND-195 and is "
    "the canonical target once available."
)

#: Attribute names that mean "this call is reporting, not dispatching".
_LOG_METHODS = frozenset(
    {"debug", "info", "warning", "warn", "error", "critical", "exception", "log"}
)

#: Trailing identifier segments that make an ``ALL_CAPS`` name read as prose. A
#: name is message-shaped only when it *ends* in one of these segments
#: (``_MESSAGE``, ``START_MESSAGE``, ``VALIDATION_RATIONALE``), so a queue
#: template whose name merely contains a prose fragment
#: (``MESSAGE_QUEUE``, ``HELP_QUEUE``, ``DOC_QUEUE``) stays flagged.
_PROSE_NAME_SUFFIXES = (
    "MESSAGE",
    "MSG",
    "RATIONALE",
    "DESCRIPTION",
    "DOC",
    "HELP",
    "HINT",
    "REMEDIATION",
    "EXPLANATION",
)


def _documentation_constants(tree: ast.AST) -> set[int]:
    """Constants that are the value of a bare string expression statement.

    Generalises the original module/class/function-docstring exclusion to any
    bare string expression, which is what a PEP 257 attribute docstring is. A
    string that is never bound to anything cannot be dispatched as an
    identifier, so it is documentation by construction.
    """
    found: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Expr):
            continue
        value = node.value
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            found.add(id(value))
    return found


def _is_diagnostic_call(node: ast.Call) -> bool:
    """True for a logging call or ``warnings.warn`` — a reporting sink."""
    func = node.func
    if isinstance(func, ast.Attribute):
        if func.attr in _LOG_METHODS:
            return True
        # warnings.warn(...)
        if func.attr == "warn":
            return True
    if isinstance(func, ast.Name) and func.id in {"warn", "print"}:
        return True
    return False


def _diagnostic_constants(tree: ast.AST) -> set[int]:
    """String constants inside a logging/warning call, or inside a ``raise``.

    Code that reports an unresolved token has to quote it; that text is never
    dispatched as an identifier. Without this, the SDK's own WARNING/ERROR logs
    naming the token they could not resolve are flagged. An f-string *inside*
    one of these sinks still needs its pieces exempted here — the line-based
    waiver in ``check_o005`` would otherwise re-flag its escaped braces.
    """
    found: set[int] = set()
    subtrees: list[ast.AST] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and _is_diagnostic_call(node):
            subtrees.extend(node.args)
            subtrees.extend(kw.value for kw in node.keywords)
        elif isinstance(node, ast.Raise):
            subtrees.append(node)
    for subtree in subtrees:
        for inner in ast.walk(subtree):
            if isinstance(inner, ast.Constant) and isinstance(inner.value, str):
                found.add(id(inner))
    return found


def _diagnostic_joined_str_lines(tree: ast.AST) -> set[int]:
    """Lines hosting a ``JoinedStr`` inside a logging/warning call or ``raise``.

    Code that reports an unresolved token has to quote it; that text is never
    dispatched as an identifier — and that holds for a *resolving* f-string
    (``logger.info(f"queue atlan-{app_name}-prod")``) just as much as for a
    plain literal. Without this, ``_unresolved_joined_str_lines`` would flag
    every interpolated f-string that merely mentions the token in a log
    message.

    Line-keyed (not node-id-keyed) for the same reason as
    ``_unresolved_joined_str_lines``: the SARIF emitter re-parses the source,
    so only positions survive.
    """
    lines: set[int] = set()
    subtrees: list[ast.AST] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and _is_diagnostic_call(node):
            subtrees.extend(node.args)
            subtrees.extend(kw.value for kw in node.keywords)
        elif isinstance(node, ast.Raise):
            subtrees.append(node)
    for subtree in subtrees:
        for inner in ast.walk(subtree):
            if isinstance(inner, ast.JoinedStr):
                lines.add(inner.lineno)
    return lines


def _unresolved_joined_str_lines(tree: ast.AST) -> set[int]:
    """Lines hosting a ``JoinedStr`` whose runtime value still carries the token.

    Python parses ``f"atlan-{{app_name}}-production"`` as a ``JoinedStr``, but
    the escaped braces are *not* interpolated — the runtime string is the
    literal ``atlan-{app_name}-production``, the exact frozen-token shape this
    rule exists to catch (a real f-string, ``f"...{app_name}..."``, contains no
    ``{app_name}`` substring anywhere in its ``ast.Constant`` pieces — the token
    lives in the ``ast.FormattedValue`` — so it never lands here).

    A ``JoinedStr`` containing an escaped-brace token inside a diagnostic call
    or a docstring stays exempt (``check_o005`` subtracts both line-sets):
    reporting on the token still is not dispatching it, and double-brace-
    quoting it is a normal way to make the literal render inside an f-string.
    """
    lines: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.JoinedStr):
            continue
        if any(
            isinstance(value, ast.Constant)
            and isinstance(value.value, str)
            and _TOKEN in value.value
            for value in node.values
        ):
            lines.add(node.lineno)
    return lines


def _sentinel_or_prose_constants(tree: ast.AST) -> set[int]:
    """Token definitions and message constants bound to an ``ALL_CAPS`` name.

    Two shapes, both non-dispatching:

    * the literal is *exactly* the token — this is the declaration of the token
      being hunted (``APP_NAME_TOKEN = "{app_name}"``), not a frozen identifier;
    * the name reads as prose (``_MESSAGE``, ``RATIONALE``) — the string is
      human-facing text that happens to quote the token. Prose is matched on
      the *trailing* delimited segment, not substrings: ``START_MESSAGE`` is
      prose, while ``MESSAGE_QUEUE`` is a queue template that merely contains
      the fragment.

    Deliberately narrow: ``TASK_QUEUE = "atlan-{app_name}-prod"`` is an
    ``ALL_CAPS`` name that is *not* prose-shaped and whose literal is *not* bare,
    so it stays flagged.
    """
    found: set[int] = set()
    for node in ast.walk(tree):
        targets: list[ast.expr] = []
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
            targets = [node.target]
        else:
            continue
        value = node.value
        if not (isinstance(value, ast.Constant) and isinstance(value.value, str)):
            continue
        names = [t.id for t in targets if isinstance(t, ast.Name)]
        if not names:
            continue
        for name in names:
            bare = name.lstrip("_")
            if not (bare.isupper() and bare):
                continue
            if value.value.strip() == _TOKEN:
                found.add(id(value))
            elif any(bare.split("_")[-1] == part for part in _PROSE_NAME_SUFFIXES):
                found.add(id(value))
    return found


def _resolving_format_receivers(tree: ast.AST) -> set[int]:
    """String-literal receivers of a ``.format(...)`` call that keyword-binds ``app_name``."""
    receivers: set[int] = set()
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
            receivers.add(id(receiver))
    return receivers


def _joined_str_children(tree: ast.AST) -> set[int]:
    """Constant nodes that are pieces of an f-string — never independently flagged."""
    pieces: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr):
            for value in node.values:
                if isinstance(value, ast.Constant):
                    pieces.add(id(value))
    return pieces


def check_o005(
    tree: ast.AST,
    filename: str,
    directives: dict[int, _IgnoreDirective],
) -> list[Finding]:
    """Emit O005 for a plain string literal that still carries an unresolved '{app_name}' token."""
    findings: list[Finding] = []
    exempt = (
        _resolving_format_receivers(tree)
        | _joined_str_children(tree)
        | _documentation_constants(tree)
        | _diagnostic_constants(tree)
        | _sentinel_or_prose_constants(tree)
    )
    # An f-string piece still carrying the token is the escaped-brace shape:
    # ``f"{{app_name}}"`` is *not* interpolated — the runtime value freezes the
    # token exactly like a plain literal, so exempting every JoinedStr child
    # (above) must not cover it. The waiver is line-keyed (a JoinedStr and its
    # Constant children share the f-string's line, and positions are the only
    # handle that survives the SARIF emitter's re-parse); a docstring or a
    # diagnostic f-string on that line stays exempt.
    # A bare string *or f-string* expression statement is documentation — a
    # docstring quoting the token with escaped braces (``f"...{{app_name}}..."``)
    # parses as ``Expr(JoinedStr)``, not ``Expr(Constant)``, so both shapes
    # contribute their line here.
    doc_lines = {
        node.value.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Expr)
        and isinstance(node.value, (ast.Constant, ast.JoinedStr))
    }
    flagged_fstring_lines = (
        _unresolved_joined_str_lines(tree)
        - _diagnostic_joined_str_lines(tree)
        - doc_lines
    )

    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
            continue
        if _TOKEN not in node.value:
            continue
        if id(node) in exempt and node.lineno not in flagged_fstring_lines:
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
