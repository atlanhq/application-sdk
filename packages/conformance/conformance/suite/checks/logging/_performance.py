"""L008, L009 — log-performance and log-noise rules."""

from __future__ import annotations

import ast
from collections import deque

from conformance.suite.checks._ast_common import _IgnoreDirective, make_finding
from conformance.suite.schema.findings import Finding

from ._base import _MixinBase
from ._constants import EXPENSIVE_CALL_ATTRS, EXPENSIVE_CALL_NAMES
from ._helpers import get_logger_method

# ---------------------------------------------------------------------------
# L008 helpers
# ---------------------------------------------------------------------------


def _contains_expensive_call(node: ast.AST) -> bool:
    """True if *node* (or any non-scope descendant) is a call to an expensive function.

    Prunes Lambda / FunctionDef / AsyncFunctionDef so that a lambda *value*
    passed as a logger argument does not trigger L008 — the lambda body is not
    evaluated at the call site.
    """
    queue: deque[ast.AST] = deque([node])
    while queue:
        current = queue.popleft()
        if isinstance(current, ast.Call):
            func = current.func
            if isinstance(func, ast.Name) and func.id in EXPENSIVE_CALL_NAMES:
                return True
            if isinstance(func, ast.Attribute) and func.attr in EXPENSIVE_CALL_ATTRS:
                return True
        if not isinstance(current, (ast.Lambda, ast.FunctionDef, ast.AsyncFunctionDef)):
            queue.extend(ast.iter_child_nodes(current))
    return False


class PerformanceMixin(_MixinBase):
    """Rule methods for L008 (log-performance) and L009 (log-noise)."""

    # ── L008 UnguardedExpensiveDebug ──────────────────────────────────────────

    def _check_l008(self, node: ast.Call) -> None:
        """Flag expensive serialisation calls in ``logger.debug()`` arguments.

        Log-call arguments are evaluated eagerly regardless of whether the level
        is enabled.  An unguarded expensive serialisation inside logger.debug()
        runs on every call in production — invisible CPU/memory overhead that
        compounds on hot paths.

        Suppress with ``# conformance: ignore[L008] reason`` when the call is
        already inside an ``if logger.isEnabledFor(logging.DEBUG):`` guard (the
        checker cannot detect the guard reliably without parent tracking).
        """
        if get_logger_method(node, self._logging_module_names) != "debug":
            return
        for arg in node.args[1:]:  # skip format string (args[0])
            if _contains_expensive_call(arg):
                self._add(
                    "L008",
                    node,
                    "Expensive computation in logger.debug() argument evaluates eagerly. "
                    "Guard with: if logger.isEnabledFor(logging.DEBUG): ...",
                )
                return
        for kw in node.keywords:
            if kw.arg == "exc_info":
                continue
            if _contains_expensive_call(kw.value):
                self._add(
                    "L008",
                    node,
                    "Expensive computation in logger.debug() argument evaluates eagerly. "
                    "Guard with: if logger.isEnabledFor(logging.DEBUG): ...",
                )
                return


# ---------------------------------------------------------------------------
# L009 — standalone visitor (needs statement-list context)
#
# Use WarnThenRaiseVisitor (not a Checker mixin) for any rule that requires
# comparing *adjacent statements* in a block; use a mixin when a single Call
# node carries enough information to emit a finding.
# ---------------------------------------------------------------------------


def _is_warn_or_error_log_stmt(
    stmt: ast.stmt,
    module_names: frozenset[str] = frozenset({"logging"}),
) -> ast.Call | None:
    """Return the Call node if *stmt* is a ``logger.warning/error(...)`` expression."""
    if not isinstance(stmt, ast.Expr):
        return None
    call = stmt.value
    if isinstance(call, ast.Await):
        call = call.value
    if not isinstance(call, ast.Call):
        return None
    method = get_logger_method(call, module_names)
    if method in ("warning", "error"):
        return call
    return None


# Keyword arguments of a log call that control the record, not its message.
_NON_CONTEXT_KWARGS: frozenset[str] = frozenset(
    {"exc_info", "stack_info", "stacklevel"}
)


def _ref_chain(node: ast.expr) -> str | None:
    """Return the dotted chain of a name or attribute access (``self.host``)."""
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return None
    parts.append(node.id)
    return ".".join(reversed(parts))


def _data_refs(node: ast.AST) -> set[str]:
    """Return the data references *node* reads, as dotted chains.

    A call contributes its receiver (``connection`` for
    ``connection.keys()``) and its arguments, never the function name itself,
    so builtins such as ``list`` and ``len`` do not count as shared data.
    """
    refs: set[str] = set()
    pending: list[ast.AST] = [node]
    while pending:
        current = pending.pop()
        if isinstance(current, ast.Call):
            func = current.func
            if isinstance(func, ast.Attribute):
                pending.append(func.value)
            pending.extend(current.args)
            pending.extend(kw.value for kw in current.keywords)
            continue
        if isinstance(current, (ast.Name, ast.Attribute)):
            chain = _ref_chain(current)
            if chain is not None:
                refs.add(chain)
                continue
        if isinstance(current, (ast.Lambda, ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        pending.extend(ast.iter_child_nodes(current))
    return refs


def _overlaps(a: set[str], b: set[str]) -> bool:
    """True when a chain in *a* and one in *b* are equal or one prefixes the other."""
    return any(
        x == y or x.startswith(y + ".") or y.startswith(x + ".") for x in a for y in b
    )


def _log_context_values(call: ast.Call) -> list[ast.expr]:
    """Return the value arguments of a log call: positional args after the
    format string, plus keyword values (and ``extra={...}`` dict values) other
    than the record-control keywords."""
    values: list[ast.expr] = list(call.args[1:])
    for kw in call.keywords:
        if kw.arg in _NON_CONTEXT_KWARGS:
            continue
        if kw.arg == "extra" and isinstance(kw.value, ast.Dict):
            values.extend(kw.value.values)
        else:
            values.append(kw.value)
    return values


def _log_adds_context(call: ast.Call, raise_stmt: ast.Raise) -> bool:
    """True when the pre-raise log carries data the raised error does not.

    L009's own carve-out ("adding context not available to the caller"),
    narrowed to what the AST can prove.  The caller only asks outside every
    ``except`` handler of the current function (see ``WarnThenRaiseVisitor``);
    within it:

    * the ``raise`` constructs a new exception (``raise Err(...)``) — a bare
      ``raise`` or ``raise exc`` re-raises an error whose content the log
      cannot be compared with, so it keeps firing;
    * the log passes at least one non-constant value argument;
    * no value argument shares a data reference with the raise's constructor
      arguments or its ``from`` cause — the error already carries it.
    """
    exc = raise_stmt.exc
    if not isinstance(exc, ast.Call):
        return False
    values = [v for v in _log_context_values(call) if not isinstance(v, ast.Constant)]
    if not values:
        return False
    raise_refs: set[str] = set()
    for arg in [*exc.args, *(kw.value for kw in exc.keywords), raise_stmt.cause]:
        if arg is not None:
            raise_refs |= _data_refs(arg)
    for value in values:
        refs = _data_refs(value)
        if not refs:
            return False
        if _overlaps(refs, raise_refs):
            return False
    return True


# Web frameworks whose ``HTTPException`` is turned into a response without
# being logged server-side.
_HTTP_EXCEPTION_MODULES: frozenset[str] = frozenset({"fastapi", "starlette"})


def _http_exception_names(tree: ast.AST) -> tuple[frozenset[str], frozenset[str]]:
    """Return (local names bound to ``HTTPException``, names bound to its modules).

    The bare name ``HTTPException`` always counts; aliases come from
    ``from fastapi|starlette... import HTTPException as X`` and module
    aliases from ``import fastapi|starlette... as m``.
    """
    names = {"HTTPException"}
    modules = set(_HTTP_EXCEPTION_MODULES)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if (node.module or "").split(".")[0] not in _HTTP_EXCEPTION_MODULES:
                continue
            for alias in node.names:
                if alias.name == "HTTPException":
                    names.add(alias.asname or alias.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] in _HTTP_EXCEPTION_MODULES:
                    modules.add(alias.asname or alias.name.split(".")[0])
    return frozenset(names), frozenset(modules)


def _raises_http_exception(
    raise_stmt: ast.Raise, names: frozenset[str], modules: frozenset[str]
) -> bool:
    """True for ``raise HTTPException(...)`` (fastapi/starlette, or the bare name).

    The framework answers the request from it and never logs it, so a log
    before it is the only server-side record, not a duplicate.
    """
    exc = raise_stmt.exc
    if isinstance(exc, ast.Call):
        exc = exc.func
    if isinstance(exc, ast.Name):
        return exc.id in names
    if isinstance(exc, ast.Attribute) and exc.attr == "HTTPException":
        chain = _ref_chain(exc)
        return chain is not None and chain.split(".")[0] in modules
    return False


class WarnThenRaiseVisitor(ast.NodeVisitor):
    """Walk all statement blocks and emit L009 findings.

    L009 fires when a ``logger.warning/error(...)`` call appears within the
    three statements immediately preceding a ``raise`` in the same block.
    This creates duplicate log records (raise site + caller's handler),
    inflating error counts in dashboards.

    Acceptable: logging immediately before re-raise when the call *adds context*
    not available to the caller.  The provable form of that is exempt (see
    :func:`_log_adds_context`): outside any ``except`` handler,
    ``raise Err(...)`` whose constructor arguments share no data with the
    log's non-constant value arguments.  A ``raise HTTPException(...)``
    (fastapi/starlette) is exempt anywhere: the framework turns it into a
    response without logging it, so the log is the only server-side record.
    Any other case needs
    ``# conformance: ignore[L009] reason`` with justification.
    """

    def __init__(
        self,
        filename: str,
        directives: dict[int, _IgnoreDirective],
        logging_module_names: frozenset[str] = frozenset({"logging"}),
    ) -> None:
        self._filename = filename
        self._directives = directives
        self._logging_module_names = logging_module_names
        self._findings: list[Finding] = []
        # ``except`` handlers enclosing the current statement in the current
        # function.  The context exemption applies only at depth 0: inside a
        # handler the same line is an L004/E005 site, whose fix would add the
        # traceback L009 exists to keep from being duplicated.
        self._handler_depth = 0
        self._http_names: frozenset[str] = frozenset({"HTTPException"})
        self._http_modules: frozenset[str] = _HTTP_EXCEPTION_MODULES

    def _scan_stmts(self, stmts: list[ast.stmt]) -> None:
        for i, stmt in enumerate(stmts):
            if not isinstance(stmt, ast.Raise):
                continue
            if _raises_http_exception(stmt, self._http_names, self._http_modules):
                continue  # the framework never logs it; the log is the record
            # Look back up to 3 statements for a warning/error log call
            for prev in reversed(stmts[max(0, i - 3) : i]):
                call = _is_warn_or_error_log_stmt(prev, self._logging_module_names)
                if call is None:
                    continue
                if self._handler_depth == 0 and _log_adds_context(call, stmt):
                    continue  # this log carries context the raise does not
                method = (
                    call.func.attr
                    if isinstance(call.func, ast.Attribute)
                    else "warning"
                )  # type: ignore[union-attr]
                self._findings.append(
                    make_finding(
                        filename=self._filename,
                        rule_id="L009",
                        node=prev,
                        message=(
                            f"logger.{method}() immediately before raise — creates duplicate "
                            "log records (raise site + caller's handler), inflating error "
                            "counts. Acceptable only when adding context not available to "
                            "the caller; otherwise just re-raise."
                        ),
                        directives=self._directives,
                    )
                )
                break  # one finding per raise

    def visit_Module(self, node: ast.Module) -> None:
        self._http_names, self._http_modules = _http_exception_names(node)
        self._scan_stmts(node.body)
        self.generic_visit(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_function(node)

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        # A nested function body is not inside the enclosing handler.
        saved = self._handler_depth
        self._handler_depth = 0
        self._scan_stmts(node.body)
        self.generic_visit(node)
        self._handler_depth = saved

    def visit_If(self, node: ast.If) -> None:
        self._scan_stmts(node.body)
        self._scan_stmts(node.orelse)
        self.generic_visit(node)

    def visit_For(self, node: ast.For) -> None:
        self._scan_stmts(node.body)
        self.generic_visit(node)

    def visit_AsyncFor(self, node: ast.AsyncFor) -> None:
        self._scan_stmts(node.body)
        self.generic_visit(node)

    def visit_While(self, node: ast.While) -> None:
        self._scan_stmts(node.body)
        self.generic_visit(node)

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        self._handler_depth += 1
        self._scan_stmts(node.body)
        self.generic_visit(node)
        self._handler_depth -= 1

    def visit_With(self, node: ast.With) -> None:
        self._scan_stmts(node.body)
        self.generic_visit(node)

    def visit_AsyncWith(self, node: ast.AsyncWith) -> None:
        self._scan_stmts(node.body)
        self.generic_visit(node)

    def visit_Try(self, node: ast.Try) -> None:
        self._scan_stmts(node.body)
        self._scan_stmts(node.orelse)
        self._scan_stmts(node.finalbody)
        self.generic_visit(node)

    # Python 3.11+ try/except* blocks have the same structure as Try.
    # The type: ignore is needed because TryStar and Try are sibling AST nodes.
    visit_TryStar = visit_Try  # type: ignore[assignment]
