"""Shared AST helper functions used across E-series rule modules."""

from __future__ import annotations

import ast
from collections.abc import Callable, Iterator
from typing import NamedTuple

from .._ast_common._exc_info import has_exc_info_traceback
from .._ast_common._sanitizers import expr_sanitizes_name
from ._constants import _BROAD_EXCEPT_TYPES, _LOG_METHODS, BUILTIN_RAISES


def _get_name(node: ast.expr | ast.AST | None) -> str | None:
    """Extract a simple name string from a Name, Attribute, or Subscript node."""
    if node is None:
        return None
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Subscript):
        return _get_name(node.value)
    return None


def _raise_exc_name(exc: ast.expr) -> str | None:
    """Get the exception class name from a raise-expression.

    Handles both ``raise ValueError`` (Name) and ``raise ValueError(...)``
    (Call wrapping a Name or Attribute).
    """
    if isinstance(exc, ast.Call):
        return _get_name(exc.func)
    return _get_name(exc)


def _is_log_call_stmt(stmt: ast.stmt) -> bool:
    """True if *stmt* is a bare ``logger.<method>(...)`` expression."""
    if not isinstance(stmt, ast.Expr):
        return False
    call = stmt.value
    if isinstance(call, ast.Await):
        call = call.value
    if not isinstance(call, ast.Call):
        return False
    func = call.func
    return isinstance(func, ast.Attribute) and func.attr in _LOG_METHODS


def _any_logging_in(stmts: list[ast.stmt]) -> bool:
    """True if any logging call appears anywhere within *stmts*."""
    for stmt in stmts:
        if _is_log_call_stmt(stmt):
            return True
        for node in _iter_shallow(stmt):
            if isinstance(node, ast.stmt) and _is_log_call_stmt(node):
                return True
    return False


def _has_exc_info(call: ast.Call, exception_name: str | None = None) -> bool:
    """True if *call* passes an ``exc_info`` value that carries a traceback.

    Thin alias over :func:`_ast_common.has_exc_info_traceback`, shared with
    L004 — ``exc_info=<the except-as binding>`` attaches the same traceback as
    ``exc_info=True`` and must not read as a discarded stack trace here either.
    """
    return has_exc_info_traceback(call, exception_name)


def _body_is_only_pass(stmts: list[ast.stmt]) -> bool:
    """True if the body is exclusively Pass (ignoring docstring constants)."""
    real = [
        s
        for s in stmts
        if not (
            isinstance(s, ast.Expr)
            and isinstance(s.value, ast.Constant)
            and isinstance(s.value.value, str)
        )
    ]
    return len(real) == 1 and isinstance(real[0], ast.Pass)


def _body_is_only_loop_control_no_logging(stmts: list[ast.stmt]) -> bool:
    """True if body only has continue/break/pass with no logging."""
    if _any_logging_in(stmts):
        return False
    real = [
        s
        for s in stmts
        if not (
            isinstance(s, ast.Expr)
            and isinstance(s.value, ast.Constant)
            and isinstance(s.value.value, str)
        )
    ]
    return len(real) > 0 and all(
        isinstance(s, (ast.Continue, ast.Break, ast.Pass)) for s in real
    )


def _is_gather_call(call: ast.Call) -> bool:
    """True if *call* is ``asyncio.gather(...)`` or bare ``gather(...)``."""
    func = call.func
    if isinstance(func, ast.Attribute):
        return (
            func.attr == "gather"
            and isinstance(func.value, ast.Name)
            and func.value.id == "asyncio"
        )
    if isinstance(func, ast.Name):
        return func.id == "gather"
    return False


def is_broad_suppress(node: ast.Call) -> bool:
    """True if *node* is ``contextlib.suppress(Exception|BaseException)``."""
    name = _get_name(node.func)
    if name != "suppress":
        return False
    for arg in node.args:
        if _get_name(arg) in _BROAD_EXCEPT_TYPES:
            return True
    return False


def is_builtin_raise(raise_node: ast.Raise) -> bool:
    """True if this raise targets a name in BUILTIN_RAISES."""
    if raise_node.exc is None:
        return False
    return _raise_exc_name(raise_node.exc) in BUILTIN_RAISES


def _get_decorator_names(
    func: ast.FunctionDef | ast.AsyncFunctionDef,
) -> frozenset[str]:
    names: set[str] = set()
    for dec in func.decorator_list:
        # @decorator → Name; @decorator("arg") → Call whose func is Name/Attribute
        if isinstance(dec, ast.Call):
            n = _get_name(dec.func)
        else:
            n = _get_name(dec)
        if n:
            names.add(n)
    return frozenset(names)


def _inherits_logging_filter(cls: ast.ClassDef) -> bool:
    for base in cls.bases:
        n = _get_name(base)
        if n == "Filter":
            return True
    return False


def _find_filter_method(
    cls: ast.ClassDef,
) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    for item in cls.body:
        if (
            isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
            and item.name == "filter"
        ):
            return item
    return None


def _filter_body_wrapped(method: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """True if the filter() body is a single Try node (fully wrapped)."""
    real = [
        s
        for s in method.body
        if not (
            isinstance(s, ast.Expr)
            and isinstance(s.value, ast.Constant)
            and isinstance(s.value.value, str)
        )
    ]
    return len(real) == 1 and isinstance(real[0], ast.Try)


def _message_kw_has_exc_text(kw_value: ast.expr, exc_binding: str | None) -> bool:
    """True if a ``message=`` value embeds caught-exception text."""
    if exc_binding is None:
        return False
    # f-string containing the exception binding name
    if isinstance(kw_value, ast.JoinedStr):
        for part in ast.walk(kw_value):
            if isinstance(part, ast.FormattedValue):
                inner = part.value
                if isinstance(inner, ast.Name) and inner.id == exc_binding:
                    return True
                if (
                    isinstance(inner, ast.Call)
                    and _get_name(inner.func) in ("str", "repr")
                    and inner.args
                    and isinstance(inner.args[0], ast.Name)
                    and inner.args[0].id == exc_binding
                ):
                    return True
        return False
    # str(exc) / repr(exc) directly
    if isinstance(kw_value, ast.Call) and (
        _get_name(kw_value.func) in ("str", "repr")
        and kw_value.args
        and (
            isinstance(kw_value.args[0], ast.Name)
            and kw_value.args[0].id == exc_binding
        )
    ):
        return True
    # BinOp concat referencing the binding
    if isinstance(kw_value, ast.BinOp) and isinstance(kw_value.op, ast.Add):
        for node in ast.walk(kw_value):
            if isinstance(node, ast.Name) and node.id == exc_binding:
                return True
    return False


def _iter_shallow(root: ast.AST) -> Iterator[ast.AST]:
    """Yield descendants of *root* without crossing nested function/class defs."""
    queue = list(ast.iter_child_nodes(root))
    while queue:
        node = queue.pop()
        yield node
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            queue.extend(ast.iter_child_nodes(node))


def _raise_preserves_trace(stmt: ast.Raise) -> bool:
    """True when a ``raise`` keeps the original exception in the chain.

    ``raise`` (bare), ``raise X(...)`` (implicit ``__context__`` chaining), and
    ``raise X(...) from e`` (explicit ``__cause__``) all preserve the traceback.
    Only ``raise X(...) from None`` deliberately discards the context, so it is
    treated as trace-losing.
    """
    if isinstance(stmt.cause, ast.Constant) and stmt.cause.value is None:
        return False
    return True


class RedactionScope(NamedTuple):
    """Where a handler's caught exception survives in redacted form.

    ``exc_name`` is the handler's bound exception (``except Exception as exc``).
    ``redacted_at`` maps each ``raise`` statement in the handler (by node
    identity) to the local names that hold the caught exception in redacted form
    *at that statement* — see :func:`redaction_scope`.
    """

    exc_name: str
    redacted_at: dict[int, frozenset[str]]


def _references_any(expr: ast.expr, names: frozenset[str]) -> bool:
    """True when *expr* reads any of *names*."""
    if not names:
        return False
    return any(
        isinstance(node, ast.Name) and node.id in names for node in ast.walk(expr)
    )


def _rebound_names(target: ast.expr) -> set[str]:
    """Names *target* binds or unbinds — assignment, unpacking, ``del``, ``as``.

    Only ``Store``/``Del`` contexts count, so ``self`` in ``self.x = ...`` and
    ``buf`` in ``buf[0] = ...`` are left alone: those statements rebind an
    attribute or an element, not the name itself.
    """
    return {
        node.id
        for node in ast.walk(target)
        if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del))
    }


def _walrus_names(stmt: ast.stmt) -> set[str]:
    """Names bound by a ``:=`` anywhere in *stmt*, excluding nested scopes.

    A walrus can rebind a tracked local from inside an expression, where the
    statement-level dispatch below would not see it. It never *adds* one: the
    name is dropped either way, which is the conservative direction.
    """
    return {
        node.target.id
        for node in _iter_shallow(stmt)
        if isinstance(node, ast.NamedExpr) and isinstance(node.target, ast.Name)
    }


def _match_bound_names(pattern: ast.pattern) -> set[str]:
    """Names a ``match`` case pattern captures (``as`` targets, ``*rest``, ``**rest``)."""
    names: set[str] = set()
    for node in ast.walk(pattern):
        for field in ("name", "rest"):
            captured = getattr(node, field, None)
            if isinstance(captured, str):
                names.add(captured)
    return names


def _derives_from_cause(value: ast.expr, exc_name: str, live: set[str]) -> bool:
    """True when *value* carries the caught exception out in redacted form."""
    return expr_sanitizes_name(value, exc_name) or _references_any(
        value, frozenset(live)
    )


def _track_carried(
    stmts: list[ast.stmt],
    exc_name: str,
    live: set[str],
    out: dict[int, frozenset[str]],
    derives: Callable[[ast.expr, str, set[str]], bool],
) -> set[str]:
    """Propagate cause-carrying locals through *stmts*, recording them at exits.

    *derives* decides what "carrying" means for the caller — redacted form for
    :func:`redaction_scope`, typed form for :func:`typed_failure_scope`.  The
    flow analysis is the same either way, which is the point: both exemptions
    ask where the caught exception is still reachable at the statement that
    leaves the handler, and only the leaf test differs.

    Walks in source order and returns the names still carrying the caught
    exception once *stmts* has run.  A name is added when it is assigned from an
    expression that carries the caught exception — directly, or by reading a name
    that is live at that point — and dropped the moment it is reassigned,
    augmented, deleted, unpacked, or rebound by a ``for`` target, ``with ... as``,
    ``:=``, import, ``def``/``class`` or ``match`` capture.

    ``out`` records the live set at each ``raise`` and ``return``, keyed by node
    identity, so a caller can ask what a specific exit carries.

    Branch joins keep a name only when *every* path keeps it, and a loop body is
    iterated to a fixpoint, so a name that survives one pass but not the next is
    dropped.  Nested ``def``/``class`` bodies are not entered: assignments there
    belong to another scope.
    """
    live = set(live)
    for stmt in stmts:
        live -= _walrus_names(stmt)

        if isinstance(stmt, (ast.Raise, ast.Return)):
            # An exit: record what is carried at it, then stop — statements
            # after it in the same block are unreachable.
            out[id(stmt)] = frozenset(live)
            continue

        if isinstance(stmt, (ast.Assign, ast.AnnAssign)):
            # The value is evaluated before the targets are bound, so `live` here
            # is still the pre-assignment set — `detail = sanitize(detail)` reads
            # the old `detail`.
            targets = stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]
            # `detail: str` with no value is a bare annotation: it binds nothing.
            carries = stmt.value is not None and derives(stmt.value, exc_name, live)
            for target in targets:
                rebound = _rebound_names(target)
                # Only a plain `name = <expr>` carries the value across whole.
                # Unpacking (`a, b = ...`) hands each name a *part* of it, which
                # is not something the sanitizer's output can be assumed to be.
                if carries and isinstance(target, ast.Name):
                    live |= rebound
                else:
                    live -= rebound
            continue

        if isinstance(stmt, ast.AugAssign):
            # `detail += extra` folds in text that was never redacted.
            live -= _rebound_names(stmt.target)
            continue

        if isinstance(stmt, ast.Delete):
            for target in stmt.targets:
                live -= _rebound_names(target)
            continue

        if isinstance(stmt, ast.If):
            taken = _track_carried(stmt.body, exc_name, live, out, derives)
            skipped = _track_carried(stmt.orelse, exc_name, live, out, derives)
            live = taken & skipped
            continue

        if isinstance(stmt, (ast.For, ast.AsyncFor, ast.While)):
            entry = set(live)
            if isinstance(stmt, (ast.For, ast.AsyncFor)):
                entry -= _rebound_names(stmt.target)
            # Shrink to a fixpoint: a name killed on a later iteration must not
            # survive because the first pass happened to keep it.
            while True:
                after = _track_carried(stmt.body, exc_name, entry, out, derives)
                narrowed = entry & after
                if narrowed == entry:
                    break
                entry = narrowed
            # Zero iterations leaves `live` untouched, so only names that hold on
            # both paths survive; `else` then runs on normal completion.
            live = _track_carried(stmt.orelse, exc_name, entry & live, out, derives)
            continue

        if isinstance(stmt, (ast.With, ast.AsyncWith)):
            for item in stmt.items:
                if item.optional_vars is not None:
                    live -= _rebound_names(item.optional_vars)
            live = _track_carried(stmt.body, exc_name, live, out, derives)
            continue

        if isinstance(stmt, (ast.Try, ast.TryStar)):
            body_live = _track_carried(stmt.body, exc_name, live, out, derives)
            paths = [
                body_live,
                _track_carried(stmt.orelse, exc_name, body_live, out, derives),
            ]
            for handler in stmt.handlers:
                # A handler can start anywhere in the body, so it inherits the
                # entry set, minus its own bound exception name.
                entry = live - ({handler.name} if handler.name else set())
                paths.append(
                    _track_carried(handler.body, exc_name, entry, out, derives)
                )
            live = _track_carried(
                stmt.finalbody, exc_name, set.intersection(*paths), out, derives
            )
            continue

        if isinstance(stmt, ast.Match):
            paths = [set(live)]  # no case matched
            for case in stmt.cases:
                entry = live - _match_bound_names(case.pattern)
                paths.append(_track_carried(case.body, exc_name, entry, out, derives))
            live = set.intersection(*paths)
            continue

        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            live.discard(stmt.name)  # binds the name; its body is another scope
            continue

        if isinstance(stmt, (ast.Import, ast.ImportFrom)):
            for alias in stmt.names:
                live.discard(alias.asname or alias.name.split(".")[0])
            continue

        if isinstance(stmt, (ast.Global, ast.Nonlocal)):
            live -= set(stmt.names)
            continue

    return live


def redaction_scope(handler: ast.ExceptHandler) -> RedactionScope | None:
    """Find where *handler*'s caught exception survives in redacted form.

    ``None`` when the handler does not bind the exception (``except Exception:``)
    — nothing is named, so nothing can be carried out in redacted form.

    The redacted text is often built up over several statements before the raise
    references it, so the locals are tracked rather than the raise expression
    alone::

        except Exception as exc:
            detail = sanitize_cause_repr(exc)
            message = f"credential rejected: {detail}"
            raise CredentialError(message) from None

    Tracking is flow-sensitive (see :func:`_track_carried`): a local that is
    later overwritten, augmented or only redacted on one branch is not live at
    the raise that follows.
    """
    if handler.name is None:
        return None
    redacted_at: dict[int, frozenset[str]] = {}
    _track_carried(handler.body, handler.name, set(), redacted_at, _derives_from_cause)
    return RedactionScope(handler.name, redacted_at)


def _raise_redacts_cause(stmt: ast.Raise, scope: RedactionScope) -> bool:
    """True when a severed ``raise X(...) from None`` still carries its cause.

    ``from None`` exists precisely so a raw traceback cannot reach the sink —
    the frame may hold a resolved credential, and the SDK's loguru sinks format
    tracebacks with ``diagnose`` enabled.  Severing and *dropping* the cause is
    trace-loss; severing while the raised error's own arguments carry the cause
    through a sanitizer is not: the failure survives in redacted form, which is
    the property E004 is about.
    """
    if stmt.exc is None:
        return False
    if expr_sanitizes_name(stmt.exc, scope.exc_name):
        return True
    return _references_any(stmt.exc, scope.redacted_at.get(id(stmt), frozenset()))


def _raise_carries_cause(stmt: ast.Raise, scope: RedactionScope | None) -> bool:
    """True when *stmt* re-raises without losing the original failure."""
    if _raise_preserves_trace(stmt):
        return True
    return scope is not None and _raise_redacts_cause(stmt, scope)


def _body_always_raises(
    body: list[ast.stmt], scope: RedactionScope | None = None
) -> bool:
    """True when *body* is guaranteed to re-raise on every path (cause intact).

    A top-level ``raise`` makes everything after it unreachable, so the block
    always raises; an ``if``/``else`` whose branches both always-raise does too.
    A ``raise`` that only appears inside a conditional without a matching ``else``
    is not counted — the other path could still swallow. A ``raise ... from None``
    on the guaranteeing path disqualifies the body unless *scope* shows it carries
    the caught exception out in redacted form (see :func:`redaction_scope`).
    """
    for stmt in body:
        if isinstance(stmt, ast.Raise):
            return _raise_carries_cause(stmt, scope)
        if (
            isinstance(stmt, ast.If)
            and stmt.orelse
            and _body_always_raises(stmt.body, scope)
            and _body_always_raises(stmt.orelse, scope)
        ):
            return True
    return False


def _body_has_bypassing_exit(
    body: list[ast.stmt],
    *,
    loop_depth: int = 0,
    scope: RedactionScope | None = None,
) -> bool:
    """True when some path through *body* leaves the handler without a
    cause-preserving re-raise.

    ``_body_always_raises`` only proves that *a* guaranteeing raise is reached;
    it does not see a path that bypasses it. Bypasses:

    * ``return`` — exits the enclosing function, swallowing the exception.
    * ``raise ... from None`` — reaches a raise but discards the cause, unless
      *scope* shows it re-raises with the cause redacted rather than dropped.
    * ``break`` / ``continue`` — only when they belong to a loop *outside* the
      handler (``loop_depth == 0``): they escape to that outer loop and skip the
      trailing raise, so the exception is swallowed. A ``break``/``continue``
      controlling a loop *nested inside* the handler is ordinary loop control —
      the handler's trailing raise still runs — and must not disqualify.

    Recurses through compound statements (tracking loop nesting) but not into
    nested ``def``/``class`` bodies, where a ``return`` belongs to the inner
    scope. Conservative by design: over-firing merely re-introduces a
    suppression, whereas under-firing hides a real swallow (WARN tier).
    """
    for stmt in body:
        if isinstance(stmt, ast.Return):
            return True
        if isinstance(stmt, ast.Raise) and not _raise_carries_cause(stmt, scope):
            return True
        if isinstance(stmt, (ast.Break, ast.Continue)):
            if loop_depth == 0:
                return True
            continue  # loop control for a loop nested in the handler
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue  # different scope — its return/raise is not a handler exit
        if isinstance(stmt, (ast.For, ast.AsyncFor, ast.While)):
            # break/continue inside the loop body target this loop, not the
            # handler; the loop's ``else`` runs after the loop, so it stays at
            # the handler's depth.
            if _body_has_bypassing_exit(
                stmt.body, loop_depth=loop_depth + 1, scope=scope
            ):
                return True
            if _body_has_bypassing_exit(
                stmt.orelse, loop_depth=loop_depth, scope=scope
            ):
                return True
            continue
        for block in _child_stmt_blocks(stmt):
            if _body_has_bypassing_exit(block, loop_depth=loop_depth, scope=scope):
                return True
    return False


def _child_stmt_blocks(stmt: ast.stmt) -> list[list[ast.stmt]]:
    """Statement blocks of a non-loop compound statement (``if``/``with``/``try``/
    ``match``), for depth-preserving recursion. Loops are handled separately so
    their nesting can be tracked."""
    blocks: list[list[ast.stmt]] = []
    for field in ("body", "orelse", "finalbody"):
        value = getattr(stmt, field, None)
        if isinstance(value, list):
            blocks.append(value)
    # ast.TryStar (``except*``, PEP 654) is a separate node, not an ast.Try
    # subclass — include its handlers too, or a swallow inside an except* group
    # is invisible.
    if isinstance(stmt, (ast.Try, ast.TryStar)):
        blocks.extend(handler.body for handler in stmt.handlers)
    if isinstance(stmt, ast.Match):
        blocks.extend(case.body for case in stmt.cases)
    return blocks


class TypedFailureScope(NamedTuple):
    """Where a handler's caught exception survives as typed data.

    ``exc_name`` is the handler's bound exception (``except Exception as exc``).
    ``carried_at`` maps each ``return``/``raise`` in the handler (by node
    identity) to the local names holding a typed value built from the caught
    exception *at that statement*; ``live_at_end`` is the same set for the
    fall-through path off the end of the handler body.

    ``typed_binding`` is true when every type the handler catches is a specific
    one (nothing in ``_BROAD_EXCEPT_TYPES``): the binding is then already a typed
    domain error, so handing it to a helper carries it out as typed data too —
    see :func:`_expr_types_cause`.
    """

    exc_name: str
    carried_at: dict[int, frozenset[str]]
    live_at_end: frozenset[str]
    typed_binding: bool = False


# Calls that turn the caught exception into text.  A string is the failure
# laundered into a plain value — its type and cause are gone — so passing the
# binding to one of these never counts as carrying it out as typed data.
_STRINGIFIERS = frozenset({"str", "repr", "ascii", "format"})


def _handler_binds_typed_error(handler: ast.ExceptHandler) -> bool:
    """True when *handler* catches only specific (non-broad) exception types."""
    if handler.type is None:
        return False
    types = handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
    names = [_get_name(t) for t in types]
    return bool(names) and all(
        name is not None and name not in _BROAD_EXCEPT_TYPES for name in names
    )


def _expr_types_cause(
    expr: ast.expr, exc_name: str, *, typed_binding: bool = False
) -> bool:
    """True when *expr* builds a typed value *from* the caught exception.

    The marker is a call to a class-like target — a ``Name`` or ``Attribute``
    whose leaf identifier is capitalised — that receives the caught binding among
    its arguments, at any nesting depth::

        SourceUnavailableError(message="…", cause=exc)
        self._failed_check(name, SourceUnavailableError(cause=exc), start)

    Recognition is by naming convention, the same trade the sanitizer detector
    makes (see ``_ast_common/_sanitizers.py``): the error leaves live in each
    app, so the checker cannot resolve them.  Capitalisation is what separates
    constructing an object from ``str(exc)`` or a bare hand-off to a helper —
    ``_failed_check(name, exc, start)`` proves nothing about what the value
    becomes, and under a broad catch nothing about ``exc`` is known either.

    Under a narrow catch (*typed_binding*) something is known: the binding is
    itself an instance of the specific types caught, so a call that receives it
    directly as an argument — ``self._failed(name, start, exc)`` — hands the
    typed error on.  Stringifiers (``str(exc)``, ``repr(exc)``,
    ``"{}".format(exc)``) never count: a string is not typed data.
    """
    for node in ast.walk(expr):
        if not isinstance(node, ast.Call):
            continue
        target = _get_name(node.func)
        if target is None:
            continue
        if typed_binding and target not in _STRINGIFIERS:
            args = [*node.args, *[kw.value for kw in node.keywords]]
            if any(isinstance(a, ast.Name) and a.id == exc_name for a in args):
                return True
        if not target[:1].isupper():
            continue
        for arg in [*node.args, *[kw.value for kw in node.keywords]]:
            for inner in ast.walk(arg):
                if isinstance(inner, ast.Name) and inner.id == exc_name:
                    return True
    return False


def _derives_typed_failure(
    value: ast.expr, exc_name: str, live: set[str], *, typed_binding: bool = False
) -> bool:
    """True when *value* carries the caught exception out as typed data."""
    return _expr_types_cause(
        value, exc_name, typed_binding=typed_binding
    ) or _references_any(value, frozenset(live))


def _typed_failure_deriver(
    typed_binding: bool,
) -> Callable[[ast.expr, str, set[str]], bool]:
    """:func:`_derives_typed_failure` bound to a handler's ``typed_binding``."""

    def derives(value: ast.expr, exc_name: str, live: set[str]) -> bool:
        return _derives_typed_failure(
            value, exc_name, live, typed_binding=typed_binding
        )

    return derives


def typed_failure_scope(handler: ast.ExceptHandler) -> TypedFailureScope | None:
    """Find where *handler*'s caught exception survives as typed data.

    ``None`` when the handler does not bind the exception (``except Exception:``)
    — nothing is named, so nothing can be carried out.

    Like :func:`redaction_scope`, the value is often staged across several
    statements before the ``return`` hands it back, so the locals are tracked to
    a fixpoint rather than the return expression alone::

        except Exception as exc:
            failure = SourceUnavailableError(cause=exc)
            row = self._failed_check(name, failure, start)
            return row
    """
    if handler.name is None:
        return None
    typed_binding = _handler_binds_typed_error(handler)
    carried_at: dict[int, frozenset[str]] = {}
    live_at_end = _track_carried(
        handler.body,
        handler.name,
        set(),
        carried_at,
        _typed_failure_deriver(typed_binding),
    )
    return TypedFailureScope(
        handler.name, carried_at, frozenset(live_at_end), typed_binding
    )


def _return_carries_typed_failure(stmt: ast.Return, scope: TypedFailureScope) -> bool:
    """True when *stmt* hands back a typed value built from the caught exception."""
    if stmt.value is None:
        return False
    return _derives_typed_failure(
        stmt.value,
        scope.exc_name,
        set(scope.carried_at.get(id(stmt), frozenset())),
        typed_binding=scope.typed_binding,
    )


def _body_always_exits(body: list[ast.stmt]) -> bool:
    """True when *body* cannot fall off its end — every path returns or raises.

    The mirror of :func:`_body_always_raises` for the typed-failure exemption,
    which accepts either kind of exit.  Conservative in the same direction: a
    body whose exits are proven only through a construct this does not model
    reads as falling through, and a fall-through has to prove itself separately.
    """
    for stmt in body:
        if isinstance(stmt, (ast.Return, ast.Raise)):
            return True
        if (
            isinstance(stmt, ast.If)
            and stmt.orelse
            and _body_always_exits(stmt.body)
            and _body_always_exits(stmt.orelse)
        ):
            return True
    return False


def _has_swallowing_exit(
    body: list[ast.stmt],
    scope: TypedFailureScope,
    *,
    loop_depth: int = 0,
    redaction: RedactionScope | None = None,
) -> bool:
    """True when some path through *body* leaves the handler carrying nothing.

    The counterpart of :func:`_body_has_bypassing_exit` for the typed-failure
    exemption.  A ``return`` whose value does not carry the caught exception as
    typed data swallows it — ``return None``, a bare sentinel, a row the tracker
    cannot reach.  A ``raise`` that drops the cause swallows it too, and a
    ``break``/``continue`` belonging to a loop *outside* the handler escapes past
    the exit that would have carried it.

    Recurses through compound statements (tracking loop nesting) but not into
    nested ``def``/``class`` bodies, whose exits belong to another scope.
    """
    for stmt in body:
        if isinstance(stmt, ast.Return):
            if not _return_carries_typed_failure(stmt, scope):
                return True
            continue
        if isinstance(stmt, ast.Raise):
            if not _raise_carries_cause(stmt, redaction):
                return True
            continue
        if isinstance(stmt, (ast.Break, ast.Continue)):
            if loop_depth == 0:
                return True
            continue  # loop control for a loop nested in the handler
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue  # different scope — its return/raise is not a handler exit
        if isinstance(stmt, (ast.For, ast.AsyncFor, ast.While)):
            if _has_swallowing_exit(
                stmt.body, scope, loop_depth=loop_depth + 1, redaction=redaction
            ):
                return True
            if _has_swallowing_exit(
                stmt.orelse, scope, loop_depth=loop_depth, redaction=redaction
            ):
                return True
            continue
        for block in _child_stmt_blocks(stmt):
            if _has_swallowing_exit(
                block, scope, loop_depth=loop_depth, redaction=redaction
            ):
                return True
    return False


def _iter_block(stmts: list[ast.stmt]) -> Iterator[ast.stmt]:
    """Yield *stmts* and their nested statements, not crossing ``def``/``class``."""
    for stmt in stmts:
        yield stmt
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        for node in _iter_shallow(stmt):
            if isinstance(node, ast.stmt):
                yield node


def _stmt_blocks_of(node: ast.AST) -> Iterator[tuple[str, list[ast.stmt]]]:
    """Yield *node*'s ``(field, block)`` pairs that are lists of statements."""
    for field, value in ast.iter_fields(node):
        if (
            isinstance(value, list)
            and value
            and all(isinstance(item, ast.stmt) for item in value)
        ):
            yield field, value


#: Blocks whose statements run in sequence after the construct below them, so
#: the walk in :func:`_trailing_statements` may step outward through one.  A
#: loop body is deliberately absent — it re-enters rather than continuing past —
#: and so are a ``finally`` body and an ``except`` body, which are reached by
#: paths the walk has not established.
_SEQUENTIAL_OWNER_FIELDS: dict[type, frozenset[str]] = {
    ast.If: frozenset({"body", "orelse"}),
    ast.Try: frozenset({"body", "orelse"}),
    ast.TryStar: frozenset({"body", "orelse"}),
    ast.With: frozenset({"body"}),
    ast.AsyncWith: frozenset({"body"}),
    ast.match_case: frozenset({"body"}),
}


def _escapes_to_outer_loop(stmts: list[ast.stmt], *, loop_depth: int = 0) -> bool:
    """True when some path through *stmts* breaks or continues out of them.

    Loop control belonging to a loop *nested inside* *stmts* is ordinary and
    does not count; one at depth zero targets a loop enclosing the whole chain
    and skips whatever came next, which is what makes the chain unprovable.
    """
    for stmt in stmts:
        if isinstance(stmt, (ast.Break, ast.Continue)):
            if loop_depth == 0:
                return True
            continue
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        if isinstance(stmt, (ast.For, ast.AsyncFor, ast.While)):
            if _escapes_to_outer_loop(stmt.body, loop_depth=loop_depth + 1):
                return True
            if _escapes_to_outer_loop(stmt.orelse, loop_depth=loop_depth):
                return True
            continue
        for block in _child_stmt_blocks(stmt):
            if _escapes_to_outer_loop(block, loop_depth=loop_depth):
                return True
    return False


def _trailing_statements(
    function: ast.FunctionDef | ast.AsyncFunctionDef, handler: ast.ExceptHandler
) -> list[ast.stmt] | None:
    """The statements that run after *handler*'s ``try``, flattened in order.

    A handler that falls off its end hands control to whatever follows the
    ``try``, and possibly to whatever follows the ``if`` that ``try`` sits in,
    so the segments are collected outwards until one of them is guaranteed to
    exit.  Concatenating them models a path where every segment runs, which is
    the conservative reading: a name rebound in an earlier segment is dropped
    for the later ones, and every ``return`` collected has to carry the failure.

    ``None`` when the fall-through reaches something this does not model.  The
    walk steps outward only through blocks that run in sequence after the one
    below them — the function body, an ``if`` arm, a ``try``/``else`` body, a
    ``with`` body, a ``match`` case.  A loop body is not one of them: the next
    iteration re-enters instead of continuing past, so what follows the loop is
    not "after" the handler.  A ``break``/``continue`` in a collected segment
    means the same thing from the other direction — it leaves the chain for a
    loop enclosing all of it, skipping whatever exit came next — and also gives
    up.  So does falling off the end of the function, which is an implicit
    ``return None`` and swallows.
    """
    owners: dict[int, tuple[ast.AST, str, list[ast.stmt]]] = {}
    for node in (function, *_iter_shallow(function)):
        if node is not function and isinstance(
            node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
        ):
            continue
        for field, block in _stmt_blocks_of(node):
            for item in block:
                owners[id(item)] = (node, field, block)

    # Typed as AST, not stmt: the walk can step to an owner that is not a
    # statement (a `match` case), where `owners` has no entry and it stops.
    current: ast.AST | None = None
    for node in _iter_shallow(function):
        if isinstance(node, (ast.Try, ast.TryStar)) and any(
            h is handler for h in node.handlers
        ):
            current = node
            break
    if current is None:
        return None

    segments: list[ast.stmt] = []
    while True:
        entry = owners.get(id(current))
        if entry is None:
            return None
        owner, field, block = entry
        # Reject an owner the walk cannot step through *before* looking at the
        # suffix, or a loop-body sibling `return` appended after a segment
        # ending in `continue` would read as "always exits" when the `continue`
        # skips that return outright.
        if owner is function:
            if field != "body":
                return None
        elif field not in _SEQUENTIAL_OWNER_FIELDS.get(type(owner), frozenset()):
            return None
        index = next(i for i, item in enumerate(block) if item is current)
        segments.extend(block[index + 1 :])
        if _escapes_to_outer_loop(segments):
            return None
        if _body_always_exits(segments):
            return segments
        # Nothing below guarantees an exit, so control reaches the enclosing
        # block too — unless there is none left, where falling off the end of
        # the function is an implicit `return None`.
        if owner is function:
            return None
        current = owner


def _staged_row_is_returned(
    function: ast.FunctionDef | ast.AsyncFunctionDef,
    handler: ast.ExceptHandler,
    scope: TypedFailureScope,
) -> bool:
    """True when a row staged in *handler* is what the function hands back.

    The last-resort arm of a probe often assigns the typed row and lets the
    function's trailing ``return`` hand it back, so cleanup that must run on
    every path stays in one place::

        except Exception as exc:
            check = self._failed_check(name, SourceUnavailableError(cause=exc), start)
        if client is not None:
            await client.close()
        return check, None

    The statements after the ``try`` are tracked with the same flow analysis the
    handler body uses, seeded with what the handler staged, so a name reassigned
    or deleted below it stops counting.  *Every* ``return`` reached from there
    must carry the failure: one arm returning the row while another returns
    ``None`` swallows on that path, and a fall-through that reaches the end of
    the function swallows too.
    """
    if not scope.live_at_end:
        return False
    trailing = _trailing_statements(function, handler)
    if not trailing:
        return False
    carried: dict[int, frozenset[str]] = {}
    _track_carried(
        trailing,
        scope.exc_name,
        set(scope.live_at_end),
        carried,
        _typed_failure_deriver(scope.typed_binding),
    )
    below = TypedFailureScope(scope.exc_name, carried, frozenset(), scope.typed_binding)
    returns = [stmt for stmt in _iter_block(trailing) if isinstance(stmt, ast.Return)]
    if not returns:
        return False
    return all(_return_carries_typed_failure(stmt, below) for stmt in returns)


def _body_returns_typed_failure(
    handler: ast.ExceptHandler,
    *,
    function: ast.FunctionDef | ast.AsyncFunctionDef | None = None,
    redaction: RedactionScope | None = None,
) -> bool:
    """True when the caught exception leaves *handler* as typed data on every path.

    A broad catch that converts the exception into a typed value and hands it
    back — a failed check row, a typed result object — is not swallowing it: the
    failure leaves the frame in inspectable form and whatever consumes the value
    decides how it is reported, which is the property a cause-preserving re-raise
    has too.  The log level at such a site is therefore irrelevant to whether the
    failure is visible, so requiring one (E004's ``exc_info`` exemption, whose
    accepted levels are exactly the ones F005 forbids inside a preflight gate)
    grades the wrong thing.

    Every exit must carry it.  A ``return None``, a bare sentinel, a ``raise``
    that drops the cause, or a ``break``/``continue`` escaping to an outer loop
    all swallow on that path.  So does ``except Exception:`` with no ``as``
    binding — nothing is named, so nothing can be carried out.

    A handler that falls off its end carries the failure only if the typed row is
    still live in a local that the enclosing function returns below the ``try``
    (:func:`_staged_row_is_returned`); without *function* that shape cannot be
    proven and does not pass.
    """
    scope = typed_failure_scope(handler)
    if scope is None:
        return False
    if _has_swallowing_exit(handler.body, scope, redaction=redaction):
        return False
    if _body_always_exits(handler.body):
        return True
    return function is not None and _staged_row_is_returned(function, handler, scope)
