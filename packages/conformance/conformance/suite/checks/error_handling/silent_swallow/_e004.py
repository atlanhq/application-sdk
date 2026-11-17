"""E004 — a broad ``except`` that neither logs nor re-raises.

Also holds the E004 exemptions (stashed re-raises, best-effort cleanup,
predicate-gated skips, scripts, preflight) that E007/E009 reuse through ``self``."""

from __future__ import annotations

import ast

from ..._ast_common._sanitizers import (
    _call_uses_sanitized_local_alias,
    _is_type_projection,
    call_uses_sanitizer,
    expr_sanitizes_name,
    is_sanitizer_call,
)
from .._constants import _BROAD_EXCEPT_TYPES
from .._helpers import (
    RedactionScope,
    _body_always_raises,
    _body_has_bypassing_exit,
    _body_returns_typed_failure,
    _call_argument_for_parameter,
    _get_name,
    _handler_rebinds_exception,
    _has_exc_info,
    _is_empty_sentinel,
    _is_log_call_stmt,
    _iter_shallow,
    _raise_carries_cause,
    redaction_scope,
    visible_helpers,
)


class E004Mixin:
    """Rule methods for E004."""

    # ── E004 ─────────────────────────────────────────────────────────────────

    def _check_p004(self, node: ast.ExceptHandler) -> None:
        if node.type is None:
            return  # bare except handled by E006
        if isinstance(node.type, ast.Tuple):
            broad = {_get_name(e) for e in node.type.elts} & _BROAD_EXCEPT_TYPES
        else:
            name = _get_name(node.type)
            broad = {name} & _BROAD_EXCEPT_TYPES if name else set()
        if not broad:
            return
        exc_type = next(iter(broad))

        local_helpers = visible_helpers(self._local_helpers, self._function_stack)

        # Pass only if the handler re-raises with the cause preserved on *every*
        # path (bare `raise`, `raise X(...)`, or `raise X(...) from e`): a broad
        # catch that translates-and-chains swallows nothing. A guaranteeing raise
        # alone is not enough — a preceding `return`/`break`/`continue` (swallow)
        # or a `raise ... from None` that drops the cause bypasses it, so those
        # disqualify. A severed raise that carries the caught exception through a
        # redaction helper does not: it preserves the cause in the only form that
        # is safe to emit from a frame holding a resolved credential.
        scope = self._with_converted_raises(
            node, redaction_scope(node, sdk_errors=self._sdk_errors)
        )
        if _body_always_raises(node.body, scope) and not _body_has_bypassing_exit(
            node.body, scope=scope
        ):
            return
        # Same principle on the other arm: a handler that converts the caught
        # exception into a typed value and hands it back swallows nothing — the
        # failure leaves the frame as data for the caller to report, so no log
        # level decides whether it is visible. Every exit must carry it, and an
        # unbound `except Exception:` never can.
        if _body_returns_typed_failure(
            node,
            function=self._function_stack[-1] if self._function_stack else None,
            redaction=scope,
            local_helpers=local_helpers,
        ):
            return

        def _call_redacts_parameter(call: ast.Call, parameter: str) -> bool:
            # `exc_info` is a traceback switch, not message text: any value not
            # provably falsy — the parameter, `True`, a sanitized string —
            # attaches the raw active traceback past the sanitizer, so the call
            # is no redaction boundary.
            if any(
                keyword.arg == "exc_info"
                and not (
                    isinstance(keyword.value, ast.Constant) and not keyword.value.value
                )
                for keyword in call.keywords
            ):
                return False
            sanitized = False
            raw = False
            pending: list[ast.AST] = [
                *call.args,
                *[keyword.value for keyword in call.keywords],
            ]
            while pending:
                current = pending.pop()
                # ``type(exc).__name__`` names the class, never the message.
                if _is_type_projection(current, parameter):
                    continue
                if isinstance(current, ast.Call) and is_sanitizer_call(current):
                    sanitizer_args = [
                        *current.args,
                        *[keyword.value for keyword in current.keywords],
                    ]
                    if any(
                        isinstance(inner, ast.Name)
                        and inner.id == parameter
                        and isinstance(inner.ctx, ast.Load)
                        for argument in sanitizer_args
                        for inner in ast.walk(argument)
                    ):
                        sanitized = True
                        continue
                if (
                    isinstance(current, ast.Name)
                    and current.id == parameter
                    and isinstance(current.ctx, ast.Load)
                ):
                    raw = True
                pending.extend(ast.iter_child_nodes(current))
            return sanitized and not raw

        def _logged_paths(
            body: list[ast.stmt], incoming: set[bool], parameter: str
        ) -> tuple[set[bool], set[bool]] | None:
            active = set(incoming)
            exited: set[bool] = set()
            for statement in body:
                if not active or active == {True}:
                    break
                if (
                    isinstance(statement, ast.Expr)
                    and isinstance(statement.value, ast.Constant)
                    and isinstance(statement.value.value, str)
                ):
                    continue  # helper docstring
                if isinstance(statement, ast.If):
                    then_paths = _logged_paths(statement.body, active, parameter)
                    if then_paths is None:
                        return None
                    else_paths = (
                        _logged_paths(statement.orelse, active, parameter)
                        if statement.orelse
                        else (set(active), set())
                    )
                    if else_paths is None:
                        return None
                    active = then_paths[0] | else_paths[0]
                    exited.update(then_paths[1] | else_paths[1])
                    continue
                if isinstance(statement, (ast.Return, ast.Raise)):
                    exited.update(active)
                    active.clear()
                    continue
                if isinstance(statement, ast.Pass):
                    continue
                # A plain assignment neither exits nor skips the log after it
                # (a write to the parameter itself was rejected up front).
                if isinstance(statement, (ast.Assign, ast.AnnAssign)):
                    continue
                if isinstance(statement, ast.Expr) and isinstance(
                    statement.value, ast.Call
                ):
                    call = statement.value
                    if (
                        isinstance(call.func, ast.Attribute)
                        and call.func.attr in ("warning", "error", "critical")
                        and _call_redacts_parameter(call, parameter)
                    ):
                        active = {True}
                        continue
                # Do not summarize loops or opaque calls before a redacted log:
                # their effect on whether the failure reaches it is not proven
                # by this deliberately small flow analysis.
                return None
            return active, exited

        def _helper_logs_parameter(helper: ast.FunctionDef, parameter: str) -> bool:
            # A write to the parameter anywhere — an `if (p := None):` test, a
            # walrus inside the log call's own arguments — can replace the
            # failure before the sanitizer sees it; the flow below does not
            # track values, so any rebinding leaves the proof incomplete.
            if any(
                (
                    isinstance(descendant, ast.Name)
                    and descendant.id == parameter
                    and isinstance(descendant.ctx, (ast.Store, ast.Del))
                )
                or (
                    isinstance(descendant, (ast.Global, ast.Nonlocal))
                    and parameter in descendant.names
                )
                for statement in helper.body
                for descendant in ast.walk(statement)
            ):
                return False
            paths = _logged_paths(helper.body, {False}, parameter)
            if paths is None:
                return False
            all_paths = paths[0] | paths[1]
            return bool(all_paths) and all_paths == {True}

        def _call_logs_binding(call: ast.Call) -> bool:
            """A proven helper receives the binding and logs it through a sanitizer."""
            assert node.name is not None
            method = self._same_class_method(call, allow_static=True)
            if method is not None:
                return any(
                    isinstance(
                        passed := self._method_argument(call, method, parameter),
                        ast.Name,
                    )
                    and passed.id == node.name
                    and _helper_logs_parameter(method, parameter)
                    for parameter in self._method_parameters(method)
                )
            if not isinstance(call.func, ast.Name):
                return False
            helper = local_helpers.get(call.func.id)
            if (
                helper is None
                or helper.decorator_list
                or helper.args.vararg is not None
                or helper.args.kwarg is not None
            ):
                return False
            parameters = [
                *helper.args.posonlyargs,
                *helper.args.args,
                *helper.args.kwonlyargs,
            ]
            return any(
                isinstance(
                    passed := _call_argument_for_parameter(call, helper, parameter.arg),
                    ast.Name,
                )
                and passed.id == node.name
                and _helper_logs_parameter(helper, parameter.arg)
                for parameter in parameters
            )

        def _is_raising_guard(previous: ast.stmt) -> bool:
            # `if <cond>: raise <cause-carrying>` with no else: the path it
            # takes re-raises, the other one reaches the helper.  A severed
            # `raise helper(..., exc) from None` qualifies when the helper is
            # proven to log the binding through a sanitizer first.
            if not (
                isinstance(previous, ast.If)
                and not previous.orelse
                and len(previous.body) == 1
                and isinstance(previous.body[0], ast.Raise)
            ):
                return False
            raised = previous.body[0]
            if _raise_carries_cause(raised, scope):
                return True
            return (
                node.name is not None
                and isinstance(raised.exc, ast.Call)
                and _call_logs_binding(raised.exc)
            )

        def _returned_calls(statement: ast.stmt) -> list[ast.Call]:
            """Calls whose results a statement hands back or makes bare.

            ``helper(...)`` / ``await helper(...)`` as a statement, or the value
            of ``return helper(...)`` / ``return None, helper(...)``.
            """
            call = self._unwrapped_call(statement)
            if call is not None:
                return [call]
            if not isinstance(statement, ast.Return) or statement.value is None:
                return []
            values = (
                statement.value.elts
                if isinstance(statement.value, ast.Tuple)
                else [statement.value]
            )
            calls: list[ast.Call] = []
            for value in values:
                if isinstance(value, ast.Await):
                    value = value.value
                if isinstance(value, ast.Call):
                    calls.append(value)
            return calls

        def _handler_logs_through_helper() -> bool:
            if node.name is None:
                return False
            for index, statement in enumerate(node.body):
                calls = _returned_calls(statement)
                if not calls:
                    continue
                prefix = node.body[:index]
                if any(
                    not isinstance(
                        previous, (ast.Assign, ast.AnnAssign, ast.Expr, ast.Pass)
                    )
                    and not _is_raising_guard(previous)
                    for previous in prefix
                ):
                    continue
                if any(
                    isinstance(descendant, ast.Name)
                    and descendant.id == node.name
                    and isinstance(descendant.ctx, (ast.Store, ast.Del))
                    for previous in prefix
                    for descendant in ast.walk(previous)
                ):
                    continue
                if any(_call_logs_binding(call) for call in calls):
                    return True
            return False

        # A stash that the owning function provably re-raises after cleanup
        # (or after a worker thread is joined) defers the failure, not swallows it.
        if self._handler_stashes_for_reraise(node):
            return

        # Best-effort cleanup logged at DEBUG with its traceback, and a skip
        # gated on a predicate of the exception that re-raises everything else.
        if self._is_quiet_cleanup(node) or self._is_predicate_gated_skip(node, scope):
            return

        # A durable failure record handed the exception, then a per-item skip.
        if self._handler_records_failure_then_skips(node):
            return

        # A proven typed-result helper built from the binding is returned, or
        # the typed failure is recorded as a preflight check row.
        if self._handler_returns_typed_data(
            node
        ) or self._handler_records_preflight_failure(node):
            return

        # A sanitized DEBUG/INFO note before a computed fallback value: the
        # failure is traced and the caller still gets a real result.
        if self._is_sanitized_log_before_fallback(node):
            return

        # A standalone script reports to its terminal: printing the caught
        # exception is the failure record, the same carve-out L005 makes.
        if self._script_file and self._handler_prints_binding(node):
            return

        # Inside F005's preflight scope WARNING is barred, so a DEBUG/INFO log
        # that carries the exception is the most the handler can do there.
        in_preflight = bool(self._function_stack) and (
            self._function_stack[-1].lineno in self._preflight_lines
        )

        # Follow only an unconditional direct call to a proven same-module helper.
        # A preceding branch can bypass the helper and leaves this proof incomplete.
        if _handler_logs_through_helper():
            return

        # Pass if body has logger.exception() or any log call with exc_info=True
        for n in _iter_shallow(node):
            if not isinstance(n, ast.Expr):
                continue
            call = n.value
            if isinstance(call, ast.Await):
                call = call.value
            if not isinstance(call, ast.Call):
                continue
            func = call.func
            if not isinstance(func, ast.Attribute):
                continue
            if func.attr == "exception":
                return
            if func.attr in ("warning", "error", "critical") and _has_exc_info(
                call, node.name
            ):
                return
            # A log call that formats the exception through a redaction helper
            # marks a deliberate no-traceback boundary (see _sanitizers.py) —
            # the failure IS logged; exc_info there would leak past the
            # sanitizer, so its absence must not flag the handler.
            if func.attr in ("warning", "error", "critical") and call_uses_sanitizer(
                call, handler=node
            ):
                return
            if (
                in_preflight
                and func.attr in ("debug", "info")
                and self._log_carries_binding(call, node.name, node)
            ):
                return
        self._add(
            "E004",
            node,
            f"'except {exc_type}' catches everything. Acceptable only at top-level handlers "
            f"(worker loops, HTTP handlers) when logged with exc_info=True. "
            f"Narrow the exception type or add exc_info=True logging.",
        )

    # ── E004: a converted error re-raised with its cause dropped ──────────────

    @staticmethod
    def _with_converted_raises(
        node: ast.ExceptHandler, scope: RedactionScope | None
    ) -> RedactionScope | None:
        """Credit ``raise NAME from None`` when NAME was converted from the binding.

        ::

            typed = _to_typed_fail(exc)
            if typed.cause is None:
                raise typed from None
            raise typed from exc

        NAME is bound exactly once in the handler, by a top-level
        ``NAME = f(..., exc, ...)`` that receives the bare binding and precedes
        the raise; the raise is exactly ``raise NAME from None``.  The converted
        error is built from the failure, so severing the chain does not lose it.
        """
        if scope is None or _handler_rebinds_exception(node):
            return scope
        converted: dict[str, int] = {}
        for stmt in node.body:
            if (
                isinstance(stmt, ast.Assign)
                and len(stmt.targets) == 1
                and isinstance(stmt.targets[0], ast.Name)
                and isinstance(stmt.value, ast.Call)
                and any(
                    isinstance(argument, ast.Name) and argument.id == scope.exc_name
                    for argument in [
                        *stmt.value.args,
                        *(kw.value for kw in stmt.value.keywords),
                    ]
                )
            ):
                converted[stmt.targets[0].id] = stmt.lineno
        if not converted:
            return scope
        stores: dict[str, int] = {}
        for stmt in node.body:
            for inner in [stmt, *_iter_shallow(stmt)]:
                if isinstance(inner, ast.Name) and isinstance(
                    inner.ctx, (ast.Store, ast.Del)
                ):
                    stores[inner.id] = stores.get(inner.id, 0) + 1
        redacted_at = dict(scope.redacted_at)
        for stmt in node.body:
            for inner in [stmt, *_iter_shallow(stmt)]:
                if not (
                    isinstance(inner, ast.Raise)
                    and isinstance(inner.exc, ast.Name)
                    and isinstance(inner.cause, ast.Constant)
                    and inner.cause.value is None
                ):
                    continue
                name = inner.exc.id
                if (
                    name in converted
                    and stores.get(name) == 1
                    and converted[name] < inner.lineno
                ):
                    redacted_at[id(inner)] = redacted_at.get(id(inner), frozenset()) | {
                        name
                    }
        return scope._replace(redacted_at=redacted_at)

    # ── E004 / E009: stashed for a later re-raise ─────────────────────────────

    @staticmethod
    def _stash_target(stmt: ast.stmt, exc_name: str) -> str | None:
        """The name *stmt* stores the caught exception in, or None.

        ``error = exc`` or ``holder.append(exc)`` — nothing else.
        """
        if (
            isinstance(stmt, ast.Assign)
            and len(stmt.targets) == 1
            and isinstance(stmt.targets[0], ast.Name)
            and isinstance(stmt.value, ast.Name)
            and stmt.value.id == exc_name
        ):
            return stmt.targets[0].id
        if (
            isinstance(stmt, ast.Expr)
            and isinstance(stmt.value, ast.Call)
            and isinstance(stmt.value.func, ast.Attribute)
            and stmt.value.func.attr == "append"
            and isinstance(stmt.value.func.value, ast.Name)
            and len(stmt.value.args) == 1
            and not stmt.value.keywords
            and isinstance(stmt.value.args[0], ast.Name)
            and stmt.value.args[0].id == exc_name
        ):
            return stmt.value.func.value.id
        return None

    @staticmethod
    def _binds_locally(
        function: ast.FunctionDef | ast.AsyncFunctionDef, name: str
    ) -> bool | None:
        """Whether *name* is local to *function*: True, False, or None when it is
        declared ``nonlocal``/``global`` there (it then belongs to another scope)."""
        parameters = [
            *function.args.posonlyargs,
            *function.args.args,
            *function.args.kwonlyargs,
        ]
        local = any(parameter.arg == name for parameter in parameters)
        for node in _iter_shallow(function):
            if isinstance(node, (ast.Nonlocal, ast.Global)) and name in node.names:
                return None
            if (
                isinstance(node, ast.Name)
                and node.id == name
                and isinstance(node.ctx, (ast.Store, ast.Del))
            ):
                local = True
        return local

    @staticmethod
    def _raises_name(stmt: ast.stmt, name: str) -> bool:
        """``raise NAME`` / ``raise NAME[...]``, with or without ``from``."""
        if not isinstance(stmt, ast.Raise) or stmt.exc is None:
            return False
        raised = stmt.exc
        if isinstance(raised, ast.Subscript):
            raised = raised.value
        return isinstance(raised, ast.Name) and raised.id == name

    @staticmethod
    def _tests_stash_present(test: ast.expr, name: str) -> bool:
        """Whether *test* is true exactly when the stash *name* holds a failure.

        ``NAME``, ``NAME is not None``, ``len(NAME)`` and ``len(NAME) > 0`` /
        ``!= 0`` / ``>= 1``: the false branch then proves nothing was stashed.
        A compound test (``NAME is not None and should_reraise``) is refused —
        its false branch can still drop a stashed failure.
        """

        def is_name(node: ast.expr) -> bool:
            return isinstance(node, ast.Name) and node.id == name

        def is_len(node: ast.expr) -> bool:
            return (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "len"
                and len(node.args) == 1
                and not node.keywords
                and is_name(node.args[0])
            )

        if is_name(test) or is_len(test):
            return True
        if not (isinstance(test, ast.Compare) and len(test.ops) == 1):
            return False
        op, right = test.ops[0], test.comparators[0]
        if is_name(test.left):
            return (
                isinstance(op, ast.IsNot)
                and isinstance(right, ast.Constant)
                and right.value is None
            )
        if is_len(test.left) and isinstance(right, ast.Constant):
            bound = right.value
            return (isinstance(op, (ast.Gt, ast.NotEq)) and bound == 0) or (
                isinstance(op, ast.GtE) and bound == 1
            )
        return False

    def _handler_stashes_for_reraise(self, node: ast.ExceptHandler) -> bool:
        """True when the handler only stashes the exception for a proven later re-raise.

        The handler body may hold nothing but the stash (``error = exc`` or
        ``holder.append(exc)``), other plain assignments, log calls and
        ``pass``.  The function that owns the stashed name — the handler's
        own, or the enclosing one for a worker-thread target that writes a
        closure list or a ``nonlocal`` — must then, after the ``try`` (or the
        nested ``def``) and in its own top-level statements, reach ``raise
        NAME`` / ``raise NAME[i]``, bare or under an ``if`` whose test is
        only a presence check on NAME (:meth:`_tests_stash_present`), with no ``return`` and no rebinding of NAME on the way.  The
        failure is deferred, not swallowed: it leaves the frame with its
        traceback once the cleanup or the join has run.
        """
        if (
            node.name is None
            or _handler_rebinds_exception(node)
            or not self._function_stack
        ):
            return False
        stashed: set[str] = set()
        for stmt in node.body:
            target = self._stash_target(stmt, node.name)
            if target is not None:
                stashed.add(target)
                continue
            if isinstance(stmt, (ast.Assign, ast.AnnAssign, ast.Pass)):
                continue
            if _is_log_call_stmt(stmt) or (
                isinstance(stmt, ast.Expr)
                and isinstance(stmt.value, ast.Constant)
                and isinstance(stmt.value.value, str)
            ):
                continue
            return False
        if not stashed:
            return False
        handler_try = next(
            (
                candidate
                for candidate in _iter_shallow(self._function_stack[-1])
                if isinstance(candidate, (ast.Try, ast.TryStar))
                and any(h is node for h in candidate.handlers)
            ),
            None,
        )
        if handler_try is None:
            return False
        return all(self._owner_reraises(name, handler_try) for name in sorted(stashed))

    def _owner_reraises(self, name: str, handler_try: ast.Try | ast.TryStar) -> bool:
        # Find the function owning *name*: innermost first, outward through
        # closures. *anchor* is the statement in the owner after which the
        # re-raise must come: the ``try`` itself, or the nested ``def``.
        anchor: ast.AST = handler_try
        owner = None
        for depth in range(len(self._function_stack) - 1, -1, -1):
            function = self._function_stack[depth]
            local = self._binds_locally(function, name)
            if local:
                owner = function
                break
            anchor = function
        if owner is None:
            return False
        anchor_end = getattr(anchor, "end_lineno", None) or anchor.lineno
        for stmt in owner.body:
            if stmt.lineno <= anchor_end:
                continue
            if self._raises_name(stmt, name):
                return True
            if (
                isinstance(stmt, ast.If)
                and self._tests_stash_present(stmt.test, name)
                and any(self._raises_name(inner, name) for inner in stmt.body)
            ):
                return True
            if isinstance(stmt, ast.Return) or any(
                isinstance(n, ast.Return) for n in _iter_shallow(stmt)
            ):
                return False
            if any(
                isinstance(n, ast.Name)
                and n.id == name
                and isinstance(n.ctx, (ast.Store, ast.Del))
                for n in [stmt, *_iter_shallow(stmt)]
            ):
                return False
        return False

    def _same_class_method(
        self, call: ast.Call, *, allow_static: bool = False
    ) -> ast.FunctionDef | None:
        """The method ``self.<name>(...)`` resolves to on the enclosing class, or None.

        The caller must be a plain (or classmethod) method directly in the
        class body whose receiver is never rebound; the method must be an
        unshadowed synchronous one (:meth:`_class_helper`), and a classmethod
        caller cannot reach a plain method through ``cls``.

        With *allow_static*, a ``@staticmethod`` resolves too, and a static or
        class method may also be reached as ``ClassName.<name>(...)`` when the
        caller does not rebind the class name.  Map its arguments with
        :meth:`_method_argument`, which knows a static method binds no receiver.
        """
        if not (
            isinstance(call.func, ast.Attribute)
            and isinstance(call.func.value, ast.Name)
            and self._function_stack
            and self._class_stack
        ):
            return None
        caller = self._function_stack[-1]
        class_node = self._class_stack[-1]
        positional = [*caller.args.posonlyargs, *caller.args.args]
        if (
            caller not in class_node.body
            or not positional
            or not self._supported_method_decorators(caller)
        ):
            return None
        receiver = positional[0].arg
        owner = call.func.value.id
        via_class = (
            allow_static
            and owner == class_node.name
            and owner != receiver
            and owner not in self._rebound_names
            and not any(
                self._statement_rebinds_name(statement, owner)
                for statement in caller.body
            )
        )
        if not via_class and (
            owner != receiver
            or any(
                self._statement_rebinds_name(statement, receiver)
                for statement in caller.body
            )
        ):
            return None
        helper = self._class_helper(
            class_node, call.func.attr, allow_static=allow_static
        )
        if helper is None:
            return None
        if self._is_static_method(helper):
            return helper
        if via_class and not helper.decorator_list:
            return None  # ``ClassName.method(x)`` is unbound: ``x`` lands in self
        if bool(caller.decorator_list) and not helper.decorator_list:
            return None
        return helper

    @staticmethod
    def _is_static_method(helper: ast.FunctionDef) -> bool:
        return (
            len(helper.decorator_list) == 1
            and _get_name(helper.decorator_list[0]) == "staticmethod"
        )

    def _method_parameters(self, helper: ast.FunctionDef) -> list[str]:
        """Parameters a call supplies: all of them, less the receiver unless static."""
        positional = [*helper.args.posonlyargs, *helper.args.args]
        if not self._is_static_method(helper):
            positional = positional[1:]
        return [formal.arg for formal in [*positional, *helper.args.kwonlyargs]]

    def _method_argument(
        self, call: ast.Call, helper: ast.FunctionDef, parameter: str
    ) -> ast.expr | None:
        """Map an explicit call argument to a method parameter, receiver aware."""
        if self._is_static_method(helper):
            if helper.args.vararg is not None or helper.args.kwarg is not None:
                return None
            return _call_argument_for_parameter(call, helper, parameter)
        return self._bound_method_argument(call, helper, parameter)

    # ── E004: best-effort cleanup, predicate-gated skips, scripts, preflight ──

    #: Leading verb of a call that releases a resource (``client.close()``,
    #: ``engine.dispose()``, ``self._close_session()``).
    _CLEANUP_VERBS = frozenset(
        {
            "close",
            "aclose",
            "dispose",
            "shutdown",
            "disconnect",
            "cleanup",
            "release",
            "terminate",
            "stop",
            "kill",
            "unlink",
            "rmtree",
        }
    )

    def _log_carries_binding(
        self,
        call: ast.Call,
        exc_name: str | None,
        handler: ast.ExceptHandler | None = None,
    ) -> bool:
        """The log call attaches the traceback or formats the caught exception.

        With *handler*, a bare local the handler assigned earlier from a
        sanitizer applied to the binding (``sanitized = sanitize_cause_repr(exc)``)
        and never rebound before the call counts too.
        """
        if _has_exc_info(call, exc_name):
            return True
        if exc_name is None:
            return False
        if any(
            self._expression_represents_parameter(value, exc_name)
            for value in [*call.args, *(keyword.value for keyword in call.keywords)]
        ):
            return True
        return handler is not None and _call_uses_sanitized_local_alias(call, handler)

    @staticmethod
    def _unwrapped_call(stmt: ast.stmt) -> ast.Call | None:
        """The call a bare expression statement makes (``await`` unwrapped)."""
        if not isinstance(stmt, ast.Expr):
            return None
        value = stmt.value
        if isinstance(value, ast.Await):
            value = value.value
        return value if isinstance(value, ast.Call) else None

    def _is_debug_traceback_only(
        self, body: list[ast.stmt], exc_name: str | None
    ) -> bool:
        """*body* is only log calls (and ``pass``), one at DEBUG/INFO with a traceback."""
        logged = False
        for stmt in body:
            if isinstance(stmt, ast.Pass):
                continue
            if not _is_log_call_stmt(stmt):
                return False
            call = self._unwrapped_call(stmt)
            assert call is not None and isinstance(call.func, ast.Attribute)
            if call.func.attr in ("debug", "info") and _has_exc_info(call, exc_name):
                logged = True
        return logged

    def _owning_try(self, node: ast.ExceptHandler) -> ast.Try | ast.TryStar | None:
        scope: ast.AST | None = (
            self._function_stack[-1] if self._function_stack else self._module
        )
        if scope is None:
            return None
        for candidate in _iter_shallow(scope):
            if isinstance(candidate, (ast.Try, ast.TryStar)) and any(
                handler is node for handler in candidate.handlers
            ):
                return candidate
        return None

    def _inside_finally(self, target: ast.AST) -> bool:
        """Whether *target* sits in a ``finally`` block of the same function."""
        scope: ast.AST | None = (
            self._function_stack[-1] if self._function_stack else self._module
        )
        if scope is None:
            return False
        for candidate in _iter_shallow(scope):
            if not isinstance(candidate, (ast.Try, ast.TryStar)):
                continue
            for stmt in candidate.finalbody:
                if stmt is target or any(
                    inner is target for inner in _iter_shallow(stmt)
                ):
                    return True
        return False

    def _is_cleanup_call(self, call: ast.Call) -> bool:
        name = _get_name(call.func)
        leaf = name.rsplit(".", 1)[-1] if name else ""
        return leaf.lstrip("_").split("_")[0].lower() in self._CLEANUP_VERBS

    @staticmethod
    def _is_telemetry_call(call: ast.Call) -> bool:
        """A metric emit: a ``record_metric`` leaf, or a ``get_metrics()`` receiver."""
        name = _get_name(call.func)
        if (name.rsplit(".", 1)[-1] if name else "") == "record_metric":
            return True
        func = call.func
        return (
            isinstance(func, ast.Attribute)
            and isinstance(func.value, ast.Call)
            and (_get_name(func.value.func) or "").rsplit(".", 1)[-1] == "get_metrics"
        )

    def _is_best_effort_call(self, call: ast.Call) -> bool:
        return self._is_cleanup_call(call) or self._is_telemetry_call(call)

    @staticmethod
    def _is_lookup(value: ast.expr) -> bool:
        """A getter call (``get_app_state(...)``, ``getattr(...)``) or attribute read."""
        if isinstance(value, ast.Call):
            name = _get_name(value.func)
            leaf = (name.rsplit(".", 1)[-1] if name else "").lstrip("_").lower()
            return leaf.startswith("get")
        node: ast.expr = value
        while isinstance(node, ast.Attribute):
            node = node.value
        return isinstance(node, ast.Name)

    @staticmethod
    def _guarded_name(test: ast.expr) -> str | None:
        """``if NAME:`` / ``if NAME is not None:`` — the name, else None."""
        if isinstance(test, ast.Name):
            return test.id
        if (
            isinstance(test, ast.Compare)
            and isinstance(test.left, ast.Name)
            and len(test.ops) == 1
            and isinstance(test.ops[0], ast.IsNot)
            and isinstance(test.comparators[0], ast.Constant)
            and test.comparators[0].value is None
        ):
            return test.left.id
        return None

    @staticmethod
    def _awaits_if_awaitable(stmt: ast.If, names: set[str]) -> bool:
        """``if hasattr(result, "__await__"): await result`` for a tracked name."""
        test = stmt.test
        return (
            not stmt.orelse
            and isinstance(test, ast.Call)
            and isinstance(test.func, ast.Name)
            and test.func.id == "hasattr"
            and len(test.args) == 2
            and not test.keywords
            and isinstance(test.args[0], ast.Name)
            and test.args[0].id in names
            and isinstance(test.args[1], ast.Constant)
            and test.args[1].value == "__await__"
            and len(stmt.body) == 1
            and isinstance(stmt.body[0], ast.Expr)
            and isinstance(stmt.body[0].value, ast.Await)
            and isinstance(stmt.body[0].value.value, ast.Name)
            and stmt.body[0].value.value.id == test.args[0].id
        )

    def _is_quiet_release_body(self, body: list[ast.stmt]) -> bool:
        """A ``try`` body that only looks a resource up and releases it.

        Statements are best-effort calls (releases or metric emits), single-name
        assignments from a lookup or from a release call, ``if NAME`` /
        ``if NAME is not None`` guards (no ``else``) around release calls on a
        looked-up name, and ``if hasattr(result, "__await__"): await result``
        on a release call's result.  At least one release or emit must run, and
        every bound name must feed one of those guards.
        """
        looked_up: set[str] = set()
        released: set[str] = set()
        consumed: set[str] = set()
        emits = False
        for stmt in body:
            call = self._unwrapped_call(stmt)
            if call is not None:
                if not self._is_best_effort_call(call):
                    return False
                emits = True
                continue
            if (
                isinstance(stmt, ast.Assign)
                and len(stmt.targets) == 1
                and isinstance(stmt.targets[0], ast.Name)
            ):
                target = stmt.targets[0].id
                value = stmt.value
                if isinstance(value, ast.Await):
                    value = value.value
                if isinstance(value, ast.Call) and self._is_cleanup_call(value):
                    released.add(target)
                    emits = True
                    continue
                if self._is_lookup(value):
                    looked_up.add(target)
                    continue
                return False
            if isinstance(stmt, ast.If) and not stmt.orelse:
                if self._awaits_if_awaitable(stmt, released):
                    assert isinstance(stmt.test, ast.Call)
                    awaited = stmt.test.args[0]
                    assert isinstance(awaited, ast.Name)
                    consumed.add(awaited.id)
                    continue
                guarded = self._guarded_name(stmt.test)
                if (
                    guarded is not None
                    and guarded in looked_up
                    and all(
                        (inner := self._unwrapped_call(statement)) is not None
                        and self._is_cleanup_call(inner)
                        for statement in stmt.body
                    )
                ):
                    consumed.add(guarded)
                    emits = True
                    continue
            return False
        # Every name bound here only feeds a guard: none is a computed result
        # that a failure could leave unset for the code after the ``try``.
        return emits and (looked_up | released) <= consumed

    def _is_quiet_cleanup(self, node: ast.ExceptHandler) -> bool:
        """A best-effort release whose failure is logged at DEBUG with its traceback.

        The handler body is only log calls, one of them DEBUG/INFO with a
        truthy ``exc_info``, and the ``try`` body is:

        - only call statements, every one a release (``close``/``aclose``/
          ``dispose``/``shutdown``/``disconnect``/``cleanup``/``release``/
          ``terminate``/``stop``/``kill``/``unlink``/``rmtree`` as the call's
          leading verb) or a metric emit (a ``record_metric`` leaf, or a
          ``get_metrics()`` receiver); or
        - a lookup and release (:meth:`_is_quiet_release_body`):
          ``state = app.get_app_state(...)`` then
          ``if state is not None: await state.close()``, or
          ``result = close()`` then
          ``if hasattr(result, "__await__"): await result``; or
        - any calls at all, when the ``try`` sits in a ``finally`` block.

        A WARNING there would land beside the error actually being reported,
        and narrowing the catch would let a cleanup failure replace it.
        Nothing is returned or computed under the catch, so there is no result
        the failure could corrupt.
        """
        if not self._is_debug_traceback_only(node.body, node.name):
            return False
        owner = self._owning_try(node)
        if owner is None or not owner.body:
            return False
        if self._is_quiet_release_body(owner.body):
            return True
        calls = [self._unwrapped_call(stmt) for stmt in owner.body]
        if any(call is None for call in calls):
            return False
        return self._inside_finally(owner)

    def _is_predicate_gated_skip(
        self, node: ast.ExceptHandler, scope: RedactionScope | None
    ) -> bool:
        """A skip for failures a predicate on the exception picks out; the rest re-raise.

        ::

            except Exception as exc:
                if _is_no_objectstore(exc):
                    logger.debug("no object store configured", exc_info=True)
                    return None
                raise

        The handler is one or more ``if`` statements with no ``else``, whose
        test reads the binding and whose body is only log calls — one with a
        truthy ``exc_info`` — ending in ``return``/``continue``/``pass``,
        optionally after leading aliases that only read the binding
        (``msg = str(exc)``, ``kind = type(exc).__name__``; see
        :meth:`_binding_alias`) which the test may read instead,
        followed by a cause-preserving re-raise on every remaining path.  The
        predicate is the narrowing, written as code instead of an ``except``
        clause; everything it does not pick out still propagates.
        """
        if node.name is None or _handler_rebinds_exception(node):
            return False
        # Leading ``msg = str(exc)`` / ``kind = type(exc).__name__``: aliases
        # that only read the binding, which the predicate may test instead.
        readers = {node.name}
        aliases = 0
        for stmt in node.body:
            alias = self._binding_alias(stmt, node.name)
            if alias is None or alias in readers:
                break
            readers.add(alias)
            aliases += 1
        if any(
            self._statement_rebinds_name(stmt, alias)
            for stmt in node.body[aliases:]
            for alias in readers - {node.name}
        ):
            return False
        index = aliases
        for index, stmt in enumerate(node.body[aliases:], start=aliases):
            if not isinstance(stmt, ast.If):
                break
            if stmt.orelse or not any(
                isinstance(n, ast.Name) and n.id in readers for n in ast.walk(stmt.test)
            ):
                return False
            body = stmt.body
            exit_stmt = body[-1] if body else None
            if not isinstance(exit_stmt, (ast.Return, ast.Continue, ast.Pass)):
                return False
            if not self._is_debug_traceback_only(body[:-1], node.name):
                return False
        else:
            return False
        if index == aliases:
            return False
        tail = node.body[index:]
        return _body_always_raises(tail, scope) and not _body_has_bypassing_exit(
            tail, scope=scope
        )

    #: Builtins an alias of the binding may call without reading anything else.
    _ALIAS_BUILTINS = frozenset({"str", "repr", "type"})

    def _binding_alias(self, stmt: ast.stmt, exc_name: str) -> str | None:
        """``NAME = <expr reading only the binding>`` — NAME, else None.

        The value may read the binding and call ``str``/``repr``/``type``
        (``str(exc)``, ``type(exc).__name__``); no other name.
        """
        if not (
            isinstance(stmt, ast.Assign)
            and len(stmt.targets) == 1
            and isinstance(stmt.targets[0], ast.Name)
            and stmt.targets[0].id != exc_name
        ):
            return None
        names = [n for n in ast.walk(stmt.value) if isinstance(n, ast.Name)]
        if not any(n.id == exc_name for n in names):
            return None
        if any(
            isinstance(n, (ast.NamedExpr, ast.Lambda, ast.Await))
            or isinstance(n, ast.comprehension)
            for n in ast.walk(stmt.value)
        ):
            return None
        if not all(n.id == exc_name or n.id in self._ALIAS_BUILTINS for n in names):
            return None
        return stmt.targets[0].id

    @staticmethod
    def _next_statement(stmt: ast.stmt, parents: dict[int, ast.AST]) -> ast.stmt | None:
        """The statement after *stmt*, climbing out of ``if`` blocks it ends."""
        current: ast.stmt = stmt
        while True:
            parent = parents.get(id(current))
            if parent is None:
                return None
            for _, values in ast.iter_fields(parent):
                if isinstance(values, list) and any(v is current for v in values):
                    index = next(i for i, v in enumerate(values) if v is current)
                    if index + 1 < len(values):
                        following = values[index + 1]
                        return following if isinstance(following, ast.stmt) else None
                    break
            if not isinstance(parent, ast.If):
                return None
            current = parent

    def _is_sanitized_log_before_fallback(self, node: ast.ExceptHandler) -> bool:
        """A sanitized DEBUG/INFO log, then a computed fallback after the ``try``.

        ::

            try:
                names = [read_label(i) for i in range(count)]
                if names:
                    return names
            except Exception as exc:
                logger.debug("labels unreadable; falling back: %s", safe_traceback(exc))
            return [str(d[0]).lower() for d in cursor.description]

        The handler is only log calls, one at DEBUG/INFO formatting the binding
        through a recognised sanitizer (and never raw).  It falls through, and
        the next statement after the ``try`` (climbing out of ``if`` blocks the
        ``try`` ends) returns a computed value: a call or comprehension that is
        not an empty sentinel and reads nothing the ``try`` body assigned.  A
        ``return None``, a pre-set result variable or an empty literal would
        hide the failure, so they still fire.
        """
        if node.name is None or _handler_rebinds_exception(node):
            return False
        logged = False
        for stmt in node.body:
            if isinstance(stmt, ast.Pass):
                continue
            if not _is_log_call_stmt(stmt):
                return False
            call = self._unwrapped_call(stmt)
            assert call is not None and isinstance(call.func, ast.Attribute)
            if (
                call.func.attr in ("debug", "info")
                and call_uses_sanitizer(call, handler=node)
                and any(
                    expr_sanitizes_name(argument, node.name)
                    for argument in [*call.args, *(kw.value for kw in call.keywords)]
                )
            ):
                logged = True
        if not logged:
            return False
        owner = self._owning_try(node)
        scope: ast.AST | None = (
            self._function_stack[-1] if self._function_stack else None
        )
        if owner is None or scope is None:
            return False
        parents = {
            id(child): parent
            for parent in [scope, *_iter_shallow(scope)]
            for child in ast.iter_child_nodes(parent)
        }
        following = self._next_statement(owner, parents)
        if not isinstance(following, ast.Return) or following.value is None:
            return False
        value = following.value
        if not isinstance(
            value, (ast.Call, ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)
        ) or _is_empty_sentinel(value):
            return False
        assigned = {
            inner.id
            for stmt in owner.body
            for inner in [stmt, *_iter_shallow(stmt)]
            if isinstance(inner, ast.Name)
            and isinstance(inner.ctx, (ast.Store, ast.Del))
        }
        return not any(
            isinstance(inner, ast.Name) and inner.id in assigned
            for inner in ast.walk(value)
        )

    def _handler_prints_binding(self, node: ast.ExceptHandler) -> bool:
        """A ``print(...)`` in the handler body formats the caught exception."""
        if node.name is None or _handler_rebinds_exception(node):
            return False
        for stmt in node.body:
            call = self._unwrapped_call(stmt)
            if (
                call is not None
                and isinstance(call.func, ast.Name)
                and call.func.id == "print"
                and any(
                    self._expression_represents_parameter(arg, node.name)
                    for arg in [*call.args, *(kw.value for kw in call.keywords)]
                )
            ):
                return True
        return False
