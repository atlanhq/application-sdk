"""E001–E011 and E014 — silent exception swallowing rules."""

from __future__ import annotations

import ast

from .._ast_common._sanitizers import (
    _call_uses_sanitized_local_alias,
    _is_type_projection,
    call_uses_sanitizer,
    expr_sanitizes_name,
    is_sanitizer_call,
)
from ._constants import _BROAD_EXCEPT_TYPES, _LOG_METHODS, _OPTIONAL_IMPORT_TYPES
from ._helpers import (
    RedactionScope,
    _annotated_error_return,
    _any_logging_in,
    _body_always_exits,
    _body_always_raises,
    _body_has_bypassing_exit,
    _body_is_only_loop_control_no_logging,
    _body_is_only_pass,
    _body_returns_typed_failure,
    _call_argument_for_parameter,
    _class_like,
    _dotted_name,
    _filter_body_wrapped,
    _find_filter_method,
    _function_binds_name,
    _get_name,
    _handler_rebinds_exception,
    _has_exc_info,
    _inherits_logging_filter,
    _is_empty_sentinel,
    _is_gather_call,
    _is_log_call_stmt,
    _iter_block,
    _iter_shallow,
    _raise_carries_cause,
    _return_carries_typed_failure,
    redaction_scope,
    typed_failure_scope,
    visible_helpers,
)

#: ``typing`` names that look class-like but promise no typed result object.
_TYPING_NAMES = frozenset(
    {
        "Any",
        "Optional",
        "Union",
        "Dict",
        "List",
        "Tuple",
        "Set",
        "FrozenSet",
        "Mapping",
        "MutableMapping",
        "Sequence",
        "Iterable",
        "Iterator",
        "Callable",
        "Type",
        "Awaitable",
        "Coroutine",
        "NoReturn",
        "Never",
        "Self",
    }
)

#: Verbs that make a ``*failure*`` / ``*residual*`` call a durable writer.
_RECORD_VERBS = ("write", "record", "store", "persist", "save")


class SilentSwallowMixin:
    """Rule methods for E001–E011 and E014 (silent-swallow category)."""

    # ── E001 / E002 / E006 ───────────────────────────────────────────────────

    def _check_p001_p002_p006(self, node: ast.ExceptHandler) -> None:
        is_bare = node.type is None
        is_pass_only = _body_is_only_pass(node.body)

        if is_bare:
            if is_pass_only:
                self._add(
                    "E001",
                    node,
                    "Bare 'except: pass' silently discards every exception including "
                    "SystemExit and KeyboardInterrupt — the hardest class of bugs to debug. "
                    "Replace with a typed catch that at minimum logs at DEBUG.",
                )
            else:
                self._add(
                    "E006",
                    node,
                    "Bare 'except:' (no type) catches SystemExit and KeyboardInterrupt. "
                    "Use 'except Exception:' at minimum.",
                )
        elif is_pass_only:
            if isinstance(node.type, ast.Tuple):
                names = [_get_name(e) or "?" for e in node.type.elts]
                exc_type = "(" + ", ".join(names) + ")"
            else:
                exc_type = _get_name(node.type) or "Exception"
            self._add(
                "E002",
                node,
                f"'except {exc_type}: pass' silently discards the exception with no trace. "
                "Acceptable only for genuinely trivial best-effort operations — "
                "add a comment and log at DEBUG or use the suppression directive.",
            )

    # ── E003 ─────────────────────────────────────────────────────────────────

    def _check_p003(self, node: ast.Call) -> None:
        name = _get_name(node.func)
        if name != "suppress":
            return
        for arg in node.args:
            arg_name = _get_name(arg)
            if arg_name in _BROAD_EXCEPT_TYPES:
                self._add(
                    "E003",
                    node,
                    f"contextlib.suppress({arg_name}) — scope is too broad; "
                    f"suppresses every exception. Use a specific exception type "
                    f"(e.g. suppress(FileNotFoundError)).",
                )
                return

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
        from .._ast_common._sanitizers import is_sanitizer_call

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

    def _handler_stashes_for_reraise(self, node: ast.ExceptHandler) -> bool:
        """True when the handler only stashes the exception for a proven later re-raise.

        The handler body may hold nothing but the stash (``error = exc`` or
        ``holder.append(exc)``), other plain assignments, log calls and
        ``pass``.  The function that owns the stashed name — the handler's
        own, or the enclosing one for a worker-thread target that writes a
        closure list or a ``nonlocal`` — must then, after the ``try`` (or the
        nested ``def``) and in its own top-level statements, reach ``raise
        NAME`` / ``raise NAME[i]``, bare or under an ``if`` whose test reads
        NAME, with no ``return`` and no rebinding of NAME on the way.  The
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
                and any(
                    isinstance(n, ast.Name) and n.id == name
                    for n in ast.walk(stmt.test)
                )
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

    # ── E005 ─────────────────────────────────────────────────────────────────

    def _check_p005(self, node: ast.ExceptHandler) -> None:
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
                continue  # logger.exception() implies exc_info — skip
            if call_uses_sanitizer(call, handler=node):
                # Deliberate redaction boundary — exc_info would serialize the
                # raw exception past the sanitizer and can leak credentials.
                continue
            if func.attr in ("warning", "error", "critical") and not _has_exc_info(
                call, node.name
            ):
                self._add(
                    "E005",
                    n,
                    f"logger.{func.attr}() inside except block is missing exc_info=True — "
                    f"the stack trace is silently discarded. Add exc_info=True.",
                )

    # ── E007 ─────────────────────────────────────────────────────────────────

    @staticmethod
    def _eager_calls(expression: ast.expr) -> list[ast.Call]:
        """Calls evaluated unconditionally by a simple expression."""
        if isinstance(expression, ast.Call):
            if any(isinstance(arg, ast.Starred) for arg in expression.args) or any(
                keyword.arg is None for keyword in expression.keywords
            ):
                return []
            children = [
                *expression.args,
                *(keyword.value for keyword in expression.keywords),
            ]
            return [
                expression,
                *(
                    call
                    for child in children
                    for call in SilentSwallowMixin._eager_calls(child)
                ),
            ]
        if isinstance(expression, (ast.List, ast.Tuple, ast.Set)):
            return [
                call
                for element in expression.elts
                if not isinstance(element, ast.Starred)
                for call in SilentSwallowMixin._eager_calls(element)
            ]
        if isinstance(expression, ast.Dict):
            return [
                call
                for element in (*expression.keys, *expression.values)
                if element is not None
                for call in SilentSwallowMixin._eager_calls(element)
            ]
        return []

    @staticmethod
    def _supported_method_decorators(
        method: ast.FunctionDef | ast.AsyncFunctionDef,
    ) -> bool:
        """Whether a method keeps the ordinary bound-method call shape."""
        return not method.decorator_list or (
            len(method.decorator_list) == 1
            and _get_name(method.decorator_list[0]) == "classmethod"
        )

    @staticmethod
    def _class_statement_binds_name(statement: ast.stmt, name: str) -> bool:
        """Whether a class-body statement can replace a method binding."""
        if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            return statement.name == name
        nodes = [statement, *_iter_shallow(statement)]
        for node in nodes:
            if (
                isinstance(node, ast.Name)
                and node.id == name
                and isinstance(node.ctx, (ast.Store, ast.Del))
            ):
                return True
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                for alias in node.names:
                    bound = alias.asname or alias.name.split(".")[0]
                    if bound == name:
                        return True
        return False

    def _class_helper(
        self, class_node: ast.ClassDef, name: str, *, allow_static: bool = False
    ) -> ast.FunctionDef | None:
        """Resolve one unshadowed, synchronous helper declared on this class."""
        methods = [
            statement
            for statement in class_node.body
            if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef))
            and statement.name == name
        ]
        if len(methods) != 1 or not isinstance(methods[0], ast.FunctionDef):
            return None
        helper = methods[0]
        if not self._supported_method_decorators(helper) and not (
            allow_static and self._is_static_method(helper)
        ):
            return None
        if any(
            statement is not helper
            and self._class_statement_binds_name(statement, name)
            for statement in class_node.body
        ):
            return None
        # ``self._helper = other`` anywhere in the class shadows the method on
        # instances, so the lexical definition no longer proves the callee.
        if any(
            isinstance(node, ast.Attribute)
            and node.attr == name
            and isinstance(node.ctx, (ast.Store, ast.Del))
            for node in ast.walk(class_node)
        ):
            return None
        return helper

    @staticmethod
    def _bound_method_argument(
        call: ast.Call, helper: ast.FunctionDef, parameter: str
    ) -> ast.expr | None:
        """Map an explicit call argument to a bound method parameter."""
        if (
            helper.args.vararg is not None
            or helper.args.kwarg is not None
            or any(isinstance(arg, ast.Starred) for arg in call.args)
            or any(keyword.arg is None for keyword in call.keywords)
        ):
            return None
        positional = [*helper.args.posonlyargs, *helper.args.args]
        if len(positional) < 2 or len(call.args) > len(positional) - 1:
            return None
        supplied = {
            formal.arg: value
            for formal, value in zip(positional[1:], call.args, strict=False)
        }
        positional_only = {formal.arg for formal in helper.args.posonlyargs[1:]}
        keywordable = {formal.arg for formal in helper.args.args[1:]} | {
            formal.arg for formal in helper.args.kwonlyargs
        }
        for keyword in call.keywords:
            assert keyword.arg is not None
            if (
                keyword.arg in positional_only
                or keyword.arg not in keywordable
                or keyword.arg in supplied
            ):
                return None
            supplied[keyword.arg] = keyword.value
        return supplied.get(parameter)

    @staticmethod
    def _statement_rebinds_name(statement: ast.stmt, name: str) -> bool:
        """Whether a helper statement shadows or overwrites one of its inputs."""
        if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            return statement.name == name
        for node in [statement, *_iter_shallow(statement)]:
            if (
                isinstance(node, ast.Name)
                and node.id == name
                and isinstance(node.ctx, (ast.Store, ast.Del))
            ):
                return True
            if isinstance(node, ast.ExceptHandler) and node.name == name:
                return True
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                for alias in node.names:
                    bound = alias.asname or alias.name.split(".")[0]
                    if bound == name:
                        return True
            if isinstance(node, (ast.Global, ast.Nonlocal)) and name in node.names:
                return True
            if isinstance(node, (ast.MatchAs, ast.MatchStar)) and node.name == name:
                return True
            if isinstance(node, ast.MatchMapping) and node.rest == name:
                return True
        return False

    @staticmethod
    def _expression_represents_parameter(expression: ast.expr, parameter: str) -> bool:
        """Whether a log argument carries its input directly or as safe text.

        Allowlisted shapes include the bare name (``%s`` / ``exc_info=``),
        ``str()`` / ``repr()`` of it, a recognized sanitizer applied to it, an
        f-string field of it, or ``%`` formatting with it as an operand. Lossy
        derivations such as ``bool(problem)`` or ``type(problem)`` do not surface
        the exception.
        """

        def is_parameter(node: ast.AST) -> bool:
            return (
                isinstance(node, ast.Name)
                and node.id == parameter
                and isinstance(node.ctx, ast.Load)
            )

        if is_parameter(expression):
            return True
        if isinstance(expression, ast.Call):
            if (
                isinstance(expression.func, ast.Name)
                and expression.func.id in {"str", "repr"}
                and len(expression.args) == 1
                and not expression.keywords
                and is_parameter(expression.args[0])
            ):
                return True
            # The input must be the sanitizer's value: its first positional
            # argument, bare or as ``str()`` / ``repr()``. A keyword such as
            # ``max_len=problem`` or a derived value like ``problem.args[0]``
            # does not put the exception's text in the log.
            if is_sanitizer_call(expression) and expression.args:
                value = expression.args[0]
                return is_parameter(value) or (
                    isinstance(value, ast.Call)
                    and isinstance(value.func, ast.Name)
                    and value.func.id in {"str", "repr"}
                    and len(value.args) == 1
                    and not value.keywords
                    and is_parameter(value.args[0])
                )
            return False
        if isinstance(expression, ast.JoinedStr):
            return any(
                isinstance(value, ast.FormattedValue)
                and value.format_spec is None
                and is_parameter(value.value)
                for value in expression.values
            )
        if isinstance(expression, ast.BinOp) and isinstance(expression.op, ast.Mod):
            if not (
                isinstance(expression.left, ast.Constant)
                and isinstance(expression.left.value, str)
            ):
                return False
            operands = (
                expression.right.elts
                if isinstance(expression.right, ast.Tuple)
                else [expression.right]
            )
            return any(is_parameter(operand) for operand in operands)
        return False

    def _helper_logs_parameter_before_return(
        self,
        helper: ast.FunctionDef,
        parameter: str,
        *,
        call: ast.Call | None = None,
    ) -> bool:
        """Prove a helper logs its input or traceback on every path before return."""
        if any(
            isinstance(node, (ast.Yield, ast.YieldFrom))
            for node in _iter_shallow(helper)
        ) or any(
            self._statement_rebinds_name(statement, parameter)
            for statement in helper.body
        ):
            return False

        def _is_log_level(value: ast.expr | None) -> bool:
            return (
                isinstance(value, ast.Constant)
                and isinstance(value.value, str)
                and value.value in _LOG_METHODS
            )

        def _parameter_default(name: str) -> ast.expr | None:
            positional = [*helper.args.posonlyargs, *helper.args.args]
            offset = len(positional) - len(helper.args.defaults)
            for index, formal in enumerate(positional):
                if formal.arg == name and index >= offset:
                    return helper.args.defaults[index - offset]
            for formal, default in zip(
                helper.args.kwonlyargs, helper.args.kw_defaults, strict=True
            ):
                if formal.arg == name:
                    return default
            return None

        def _selector_is_log_level(selector: ast.expr) -> bool:
            if _is_log_level(selector):
                return True
            if not isinstance(selector, ast.Name) or call is None:
                return False
            selected = _call_argument_for_parameter(call, helper, selector.id)
            if selected is not None:
                return _is_log_level(selected)
            positional = [*helper.args.posonlyargs, *helper.args.args]
            explicitly_bound = selector.id in {
                formal.arg for formal in positional[: len(call.args)]
            } or any(keyword.arg == selector.id for keyword in call.keywords)
            if explicitly_bound:
                return False
            return _is_log_level(_parameter_default(selector.id))

        # The alias must be a top-level, unconditional binding to a logger's
        # recognized fallback method. It cannot be rebound before invocation.
        logger_alias_lines: dict[str, int] = {}
        formal_names = {
            formal.arg
            for formal in [
                *helper.args.posonlyargs,
                *helper.args.args,
                *helper.args.kwonlyargs,
            ]
        }
        for statement in helper.body:
            if isinstance(statement, ast.Assign):
                if len(statement.targets) != 1 or not isinstance(
                    statement.targets[0], ast.Name
                ):
                    continue
                target = statement.targets[0]
                expression = statement.value
            elif isinstance(statement, ast.AnnAssign):
                if (
                    not isinstance(statement.target, ast.Name)
                    or statement.value is None
                ):
                    continue
                target = statement.target
                expression = statement.value
            else:
                continue
            if not (
                isinstance(expression, ast.Call)
                and isinstance(expression.func, ast.Name)
                and expression.func.id == "getattr"
                and len(expression.args) == 3
                and not expression.keywords
                and not any(isinstance(arg, ast.Starred) for arg in expression.args)
            ):
                continue
            receiver, selector, fallback = expression.args
            if not (
                isinstance(receiver, ast.Name)
                and receiver.id not in formal_names
                and isinstance(fallback, ast.Attribute)
                and isinstance(fallback.value, ast.Name)
                and fallback.value.id == receiver.id
                and fallback.attr in _LOG_METHODS
                and _selector_is_log_level(selector)
                and not any(
                    self._statement_rebinds_name(item, receiver.id)
                    for item in helper.body
                )
            ):
                continue
            alias = target.id
            writes = [
                node
                for node in ast.walk(helper)
                if isinstance(node, ast.Name)
                and node.id == alias
                and isinstance(node.ctx, (ast.Store, ast.Del))
            ]
            if len(writes) != 1 or writes[0] is not target:
                continue
            if any(
                self._statement_rebinds_name(item, alias)
                for item in helper.body
                if item is not statement
            ):
                continue
            logger_alias_lines[alias] = statement.lineno

        def _extra_carries_parameter(value: ast.expr, log_line: int) -> bool:
            """Whether an ``extra=`` mapping holds the input's text on every path.

            A conditional ``exc_info`` drops the traceback on its false branch,
            so the exception must reach the record some other way. The mapping
            qualifies only as a single top-level dict literal whose entry for
            the input is never overwritten or removed, and which is not handed
            to anything that could mutate it.
            """
            if not isinstance(value, ast.Name):
                return False
            name = value.id
            bindings = [
                statement
                for statement in helper.body
                if isinstance(statement, ast.Assign)
                and len(statement.targets) == 1
                and isinstance(statement.targets[0], ast.Name)
                and statement.targets[0].id == name
            ]
            if len(bindings) != 1 or bindings[0].lineno >= log_line:
                return False
            binding = bindings[0]
            if not isinstance(binding.value, ast.Dict):
                return False
            carrying_keys = {
                key.value
                for key, entry in zip(
                    binding.value.keys, binding.value.values, strict=True
                )
                if isinstance(key, ast.Constant)
                and isinstance(key.value, str)
                and self._expression_represents_parameter(entry, parameter)
            }
            if not carrying_keys:
                return False
            loop_keys: dict[str, set[object]] = {}
            for node in ast.walk(helper):
                if (
                    isinstance(node, (ast.For, ast.AsyncFor))
                    and isinstance(node.target, ast.Name)
                    and isinstance(node.iter, (ast.Tuple, ast.List))
                    and all(isinstance(item, ast.Constant) for item in node.iter.elts)
                ):
                    loop_keys.setdefault(node.target.id, set()).update(
                        item.value
                        for item in node.iter.elts
                        if isinstance(item, ast.Constant)
                    )
            allowed: set[int] = {id(binding.targets[0]), id(value)}
            for node in ast.walk(helper):
                if not (
                    isinstance(node, ast.Subscript)
                    and isinstance(node.value, ast.Name)
                    and node.value.id == name
                ):
                    continue
                if isinstance(node.ctx, ast.Load):
                    allowed.add(id(node.value))
                    continue
                key = node.slice
                if isinstance(key, ast.Constant):
                    written = {key.value}
                elif isinstance(key, ast.Name) and key.id in loop_keys:
                    written = loop_keys[key.id]
                else:
                    return False
                if written & carrying_keys:
                    return False
                allowed.add(id(node.value))
            return all(
                id(node) in allowed
                for node in ast.walk(helper)
                if isinstance(node, ast.Name) and node.id == name
            )

        def _called_logger_alias(statement: ast.stmt) -> ast.Call | None:
            if not isinstance(statement, ast.Expr):
                return None
            expression = statement.value
            if isinstance(expression, ast.Await):
                expression = expression.value
            if not (
                isinstance(expression, ast.Call)
                and isinstance(expression.func, ast.Name)
                and expression.func.id in logger_alias_lines
                and logger_alias_lines[expression.func.id] < statement.lineno
            ):
                return None
            return expression

        def _loop_setup_is_safe(statements: list[ast.stmt]) -> bool:
            for statement in statements:
                if isinstance(
                    statement,
                    (ast.Assign, ast.AnnAssign, ast.AugAssign, ast.Expr, ast.Pass),
                ):
                    continue
                if isinstance(statement, ast.If):
                    if not _loop_setup_is_safe(
                        statement.body
                    ) or not _loop_setup_is_safe(statement.orelse):
                        return False
                    continue
                return False
            return True

        opaque_control_flow = (
            ast.Try,
            ast.TryStar,
            ast.With,
            ast.AsyncWith,
            ast.Match,
            ast.Break,
            ast.Continue,
        )

        def _logged_paths(
            body: list[ast.stmt], incoming: set[bool]
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
                    then_paths = _logged_paths(statement.body, active)
                    if then_paths is None:
                        return None
                    else_paths = (
                        _logged_paths(statement.orelse, active)
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
                if isinstance(statement, (ast.For, ast.AsyncFor, ast.While)):
                    if not _loop_setup_is_safe(
                        statement.body
                    ) or not _loop_setup_is_safe(statement.orelse):
                        return None
                    continue
                logged_call = None
                if _is_log_call_stmt(statement):
                    logged_call = statement.value
                    if isinstance(logged_call, ast.Await):
                        logged_call = logged_call.value
                else:
                    logged_call = _called_logger_alias(statement)
                if isinstance(logged_call, ast.Call):
                    carries_exception = any(
                        self._expression_represents_parameter(value, parameter)
                        for value in [
                            *logged_call.args,
                            *(keyword.value for keyword in logged_call.keywords),
                        ]
                    ) or any(
                        keyword.arg == "extra"
                        and _extra_carries_parameter(keyword.value, statement.lineno)
                        for keyword in logged_call.keywords
                    )
                    if carries_exception:
                        active = {True}
                    continue
                # Try/with blocks, match, and jumps cannot prove that all paths
                # log the input before returning. Only simple loop setup without
                # a function-exiting or bypassing statement is accepted above.
                if isinstance(statement, opaque_control_flow):
                    return None
                # Straight-line setup (assignments, metrics calls) neither logs
                # nor exits; rebinding the input was rejected up front.
            return active, exited

        paths = _logged_paths(helper.body, {False})
        if paths is None:
            return False
        all_paths = paths[0] | paths[1]
        return bool(all_paths) and all_paths == {True}

    def _module_helper_logs_caught_exception(
        self, handler: ast.ExceptHandler, statements: list[ast.stmt]
    ) -> bool:
        """True when a visible module helper logs the handler's actual exception."""
        if (
            handler.name is None
            or _handler_rebinds_exception(handler)
            or not self._function_stack
        ):
            return False
        local_helpers = visible_helpers(self._local_helpers, self._function_stack)
        for index, statement in enumerate(statements):
            # Same statement forms as the same-class path: a bare call, one
            # whose result is assigned (``result = _report(problem)``), or one
            # whose result is returned (``return _report(problem)``).
            if isinstance(statement, (ast.Expr, ast.Assign, ast.AnnAssign, ast.Return)):
                expression = statement.value
            else:
                continue
            if expression is None:
                continue
            prefix = statements[:index]
            if any(
                not isinstance(
                    previous, (ast.Assign, ast.AnnAssign, ast.Expr, ast.Pass)
                )
                and not (
                    isinstance(previous, ast.If)
                    and not previous.orelse
                    and len(previous.body) == 1
                    and isinstance(previous.body[0], ast.Raise)
                )
                for previous in prefix
            ):
                continue
            for call in self._eager_calls(expression):
                if not isinstance(call.func, ast.Name):
                    continue
                helper = local_helpers.get(call.func.id)
                if (
                    helper is None
                    or helper.decorator_list
                    or helper.args.vararg is not None
                ):
                    continue
                parameters = [
                    *helper.args.posonlyargs,
                    *helper.args.args,
                    *helper.args.kwonlyargs,
                ]
                for parameter in parameters:
                    passed = _call_argument_for_parameter(call, helper, parameter.arg)
                    if (
                        isinstance(passed, ast.Name)
                        and passed.id == handler.name
                        and self._helper_logs_parameter_before_return(
                            helper, parameter.arg, call=call
                        )
                    ):
                        return True
        return False

    def _helper_logs_caught_exception(
        self, handler: ast.ExceptHandler, statements: list[ast.stmt]
    ) -> bool:
        """True when a prior local helper logs the handler's actual exception."""
        if self._module_helper_logs_caught_exception(handler, statements):
            return True
        if (
            handler.name is None
            or _handler_rebinds_exception(handler)
            or not self._function_stack
            or not self._class_stack
        ):
            return False
        caller = self._function_stack[-1]
        class_node = self._class_stack[-1]
        caller_parameters = [*caller.args.posonlyargs, *caller.args.args]
        if (
            caller not in class_node.body
            or not caller_parameters
            or not self._supported_method_decorators(caller)
        ):
            return False
        receiver = caller_parameters[0].arg
        # A rebound receiver (``self = proxy``) no longer resolves to this class.
        if any(
            self._statement_rebinds_name(statement, receiver)
            for statement in caller.body
        ) or any(
            isinstance(node, ast.Nonlocal) and receiver in node.names
            for node in ast.walk(caller)
        ):
            return False
        caller_is_classmethod = bool(caller.decorator_list)
        for statement in statements:
            if isinstance(statement, (ast.Expr, ast.Assign, ast.AnnAssign, ast.Return)):
                expression = statement.value
            else:
                continue
            if expression is None:
                continue
            for call in self._eager_calls(expression):
                if not (
                    isinstance(call.func, ast.Attribute)
                    and isinstance(call.func.value, ast.Name)
                    and call.func.value.id == receiver
                ):
                    continue
                helper = self._class_helper(
                    class_node, call.func.attr, allow_static=True
                )
                if helper is None:
                    continue
                # ``cls.method(x)`` on a plain method is unbound: ``x`` lands in
                # ``self``, so the descriptor-binding argument shift is wrong.
                if caller_is_classmethod and not helper.decorator_list:
                    continue
                # Any parameter the binding is passed to, not only one that
                # happens to share the handler's name.
                for parameter in self._method_parameters(helper):
                    argument = self._method_argument(call, helper, parameter)
                    if (
                        isinstance(argument, ast.Name)
                        and argument.id == handler.name
                        and self._helper_logs_parameter_before_return(helper, parameter)
                    ):
                        return True
        return False

    def _check_p007(self, node: ast.ExceptHandler) -> None:
        # A return that hands the caught exception back as typed data hides
        # nothing: the failure leaves the frame for the caller to report. This
        # is the same predicate E004 uses for its typed-failure exemption, so
        # the two rules never disagree about one shape. It is applied per
        # return, because E007 judges each return on its own.
        # A prior same-class helper call also counts as logging only when its
        # straight-line body demonstrably logs the caught binding; opaque calls
        # remain unproven.
        scope = typed_failure_scope(
            node,
            local_helpers=visible_helpers(self._local_helpers, self._function_stack),
            enclosing_function=self._function_stack[-1]
            if self._function_stack
            else None,
        )
        narrow = self._is_narrow_catch(node)
        for i, stmt in enumerate(node.body):
            if not isinstance(stmt, ast.Return) or stmt.value is None:
                continue
            if scope is not None and _return_carries_typed_failure(stmt, scope):
                continue
            # Under a narrow catch the exception type is the outcome, and a
            # typed result object saying so (`return _finish(SKIPPED, ...)`)
            # is not a sentinel the caller can mistake for success.
            if narrow and self._is_typed_data_value(stmt.value):
                continue
            # Under any catch, a proven typed-result helper built from the
            # binding itself (``return self.verdict(exc)``) carries it out.
            if self._is_typed_data_from_binding(stmt.value, node.name) and not (
                _handler_rebinds_exception(node)
            ):
                continue
            # A per-item `None` after a durable failure record is the residual
            # shape: the gap is written down as data, not only logged.
            if self._has_residual_failure_record(node, stmt):
                continue
            # A CLI exit code after the failure went to stderr is surfaced,
            # not hidden.
            if self._is_stderr_exit_code(node.body[:i], stmt):
                continue
            # The returned call itself may be the helper that logs the failure
            # (``return self._failure_row(check, exc)``).
            if not (
                _any_logging_in(node.body[:i])
                or self._helper_logs_caught_exception(node, node.body[: i + 1])
            ):
                self._add(
                    "E007",
                    stmt,
                    "except block returns a value without logging — the error is hidden. "
                    "Log before returning or raise a domain-specific exception.",
                )
            elif self._is_logged_sentinel_return(node, stmt):
                self._add(
                    "E007",
                    stmt,
                    "broad except block logs and then returns an empty sentinel — the "
                    "log line is the only trace, and the caller sees a plausible empty "
                    "result with no error code. Raise a domain-specific exception, or "
                    "return the failure as typed data.",
                )

    def _is_residual_failure_record_call(self, call: ast.Call) -> bool:
        """Whether *call* invokes the imported durable residual-recording API."""
        if isinstance(call.func, ast.Name):
            origin = self._import_origins.get(call.func.id, "")
            imported = origin.split(".")[-2:] == [
                "residuals",
                "record_residual_failure",
            ]
        elif (
            isinstance(call.func, ast.Attribute)
            and call.func.attr == "record_residual_failure"
        ):
            module = _get_name(call.func.value)
            origin = self._import_origins.get(module or "", "")
            imported = origin.split(".")[-1:] == ["residuals"]
        else:
            return False
        keywords = {keyword.arg for keyword in call.keywords if keyword.arg}
        has_output_path = bool(call.args) or "output_path" in keywords
        has_category = len(call.args) >= 2 or "category" in keywords
        return imported and has_output_path and has_category

    def _has_residual_failure_record(
        self, handler: ast.ExceptHandler, stmt: ast.Return
    ) -> bool:
        """True when the handler writes a residual record before a per-item ``None``.

        An empty aggregate (``{}``, ``[]``) still reads as a genuine empty
        result to the caller, so it qualifies only when a WARNING/ERROR also
        logged the binding through a sanitizer before the return.  The record
        may read the binding through a local built from it.
        """
        none = isinstance(stmt.value, ast.Constant) and stmt.value.value is None
        if not none and not _is_empty_sentinel(stmt.value):
            return False
        # Carried locals are followed only while the binding is intact.
        track = handler.name is not None and not _handler_rebinds_exception(handler)
        carriers: set[str] = set()
        recorded = warned = False
        for previous in handler.body:
            if previous is stmt:
                return recorded and (none or warned)
            if self._is_failure_record_call(
                self._unwrapped_call(previous), handler.name, carriers
            ):
                recorded = True
            elif self._is_sanitized_failure_log(previous, handler):
                warned = True
            if track:
                assert handler.name is not None
                self._track_binding_carriers(previous, handler.name, carriers)
        return False

    def _is_stderr_exit_code(self, prefix: list[ast.stmt], stmt: ast.Return) -> bool:
        """``return <non-zero int>`` after the handler wrote to stderr.

        ``print(f"Config error: {e}", file=sys.stderr); return 3`` is a CLI
        reporting the failure on its terminal and in its exit status.  The
        write must be a top-level statement earlier in the same handler —
        ``print(..., file=sys.stderr)`` or ``sys.stderr.write(...)``.
        ``return 0``, ``return None`` and ``return True`` still fire.
        """
        value = stmt.value
        if not (
            isinstance(value, ast.Constant)
            and type(value.value) is int
            and value.value != 0
        ):
            return False
        for previous in prefix:
            call = self._unwrapped_call(previous)
            if call is None:
                continue
            if (
                isinstance(call.func, ast.Name)
                and call.func.id == "print"
                and any(
                    kw.arg == "file" and _dotted_name(kw.value) == "sys.stderr"
                    for kw in call.keywords
                )
            ):
                return True
            if ast.unparse(call.func) == "sys.stderr.write":
                return True
        return False

    @staticmethod
    def _is_app_failure_record_call(
        call: ast.Call,
        exc_name: str | None,
        carriers: frozenset[str] = frozenset(),
    ) -> bool:
        """An app-defined durable failure-record writer handed the caught exception.

        The callee's leaf name says it durably records a failure: it contains
        ``failure`` or ``residual`` *and* a write verb (``write``, ``record``,
        ``store``, ``persist``, ``save``) — ``write_failure``,
        ``record_failure``, ``store_residual``, not ``_report_failure`` — and its arguments read the binding, directly or
        inside a value built from it
        (``write_failure(dir, key, _make_failure_record(..., e))``), or a local
        in *carriers* built from it (``record = {"message": str(e)}``).  A bare
        ``record(...)`` or a call that never sees the exception does not count.
        """
        if exc_name is None:
            return False
        readers = carriers | {exc_name}
        name = _get_name(call.func)
        leaf = (name.rsplit(".", 1)[-1] if name else "").lower()
        if "failure" not in leaf and "residual" not in leaf:
            return False
        # A durable writer, not a reporter: `_report_failure(exc)` only logs.
        if not any(verb in leaf for verb in _RECORD_VERBS):
            return False
        return any(
            isinstance(inner, ast.Name) and inner.id in readers
            for argument in [*call.args, *(kw.value for kw in call.keywords)]
            for inner in ast.walk(argument)
        )

    @staticmethod
    def _track_binding_carriers(
        stmt: ast.stmt, exc_name: str, carriers: set[str]
    ) -> None:
        """Update *carriers*: single-name locals whose value reads the binding.

        ``record = {"message": str(e)}`` makes ``record`` a carrier; assigning
        a carrier something that reads no carrier drops it.
        """
        if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1:
            target, value = stmt.targets[0], stmt.value
        elif isinstance(stmt, ast.AnnAssign) and stmt.value is not None:
            target, value = stmt.target, stmt.value
        else:
            return
        if not isinstance(target, ast.Name) or target.id == exc_name:
            return
        if any(
            isinstance(inner, ast.Name) and inner.id in carriers | {exc_name}
            for inner in ast.walk(value)
        ):
            carriers.add(target.id)
        else:
            carriers.discard(target.id)

    @staticmethod
    def _only_assigns(statements: list[ast.stmt]) -> bool:
        """Assignments, ``pass``, and ``if``s (no loops) of the same, nothing else."""
        for statement in statements:
            if isinstance(
                statement, (ast.Assign, ast.AnnAssign, ast.AugAssign, ast.Pass)
            ):
                continue
            if isinstance(statement, ast.If) and (
                SilentSwallowMixin._only_assigns(statement.body)
                and SilentSwallowMixin._only_assigns(statement.orelse)
            ):
                continue
            return False
        return True

    def _is_failure_record_call(
        self, call: ast.Call | None, exc_name: str | None, carriers: set[str]
    ) -> bool:
        return call is not None and (
            self._is_residual_failure_record_call(call)
            or self._is_app_failure_record_call(call, exc_name, frozenset(carriers))
        )

    def _is_sanitized_failure_log(
        self, stmt: ast.stmt, handler: ast.ExceptHandler
    ) -> bool:
        """A WARNING/ERROR log of the binding through a sanitizer."""
        call = self._unwrapped_call(stmt)
        return (
            call is not None
            and _is_log_call_stmt(stmt)
            and isinstance(call.func, ast.Attribute)
            and call.func.attr in ("warning", "error", "critical")
            and handler.name is not None
            and call_uses_sanitizer(call, handler=handler)
            and any(
                expr_sanitizes_name(argument, handler.name)
                for argument in [*call.args, *(kw.value for kw in call.keywords)]
            )
        )

    def _record_skip_return_ok(
        self, stmt: ast.Return, *, recorded: bool, warned: bool, branch: bool
    ) -> bool:
        """Whether a ``return`` after a durable record ends the residual shape.

        ``return`` / ``return None`` always; an empty literal (``return []``)
        only after a sanitized WARNING too; inside an ``if`` branch, any other
        value as well (the branch hands back a recovered result).
        """
        if not recorded:
            return False
        value = stmt.value
        if value is None or (isinstance(value, ast.Constant) and value.value is None):
            return True
        if _is_empty_sentinel(value):
            return warned
        return branch

    def _records_then_exits(
        self,
        body: list[ast.stmt],
        node: ast.ExceptHandler,
        carriers: set[str],
        *,
        recorded: bool,
        warned: bool,
        branch: bool,
    ) -> tuple[bool, bool, bool] | None:
        """Walk *body*; None when a statement leaves the residual shape.

        Returns ``(exited, recorded, warned)`` for the path off its end.
        """
        assert node.name is not None
        for index, stmt in enumerate(body):
            last = index == len(body) - 1
            call = self._unwrapped_call(stmt)
            if self._is_failure_record_call(call, node.name, carriers):
                recorded = True
                continue
            if isinstance(stmt, (ast.Assign, ast.AnnAssign)):
                self._track_binding_carriers(stmt, node.name, carriers)
                continue
            if isinstance(stmt, ast.Pass):
                continue
            if _is_log_call_stmt(stmt):
                warned = warned or self._is_sanitized_failure_log(stmt, node)
                continue
            if (
                isinstance(stmt, (ast.For, ast.AsyncFor))
                and not stmt.orelse
                and self._only_assigns(stmt.body)
            ):
                continue
            if isinstance(stmt, ast.If):
                for arm in (stmt.body, stmt.orelse):
                    if not arm:
                        continue
                    outcome = self._records_then_exits(
                        arm,
                        node,
                        set(carriers),
                        recorded=recorded,
                        warned=warned,
                        branch=True,
                    )
                    if outcome is None or not outcome[0]:
                        return None
                if stmt.orelse:
                    return (True, recorded, warned) if last else None
                continue
            if last and recorded and isinstance(stmt, ast.Continue) and not branch:
                return True, recorded, warned
            if (
                last
                and isinstance(stmt, ast.Return)
                and self._record_skip_return_ok(
                    stmt, recorded=recorded, warned=warned, branch=branch
                )
            ):
                return True, recorded, warned
            return None
        return False, recorded, warned

    def _handler_records_failure_then_skips(self, node: ast.ExceptHandler) -> bool:
        """E004's residual shape: a durable failure record, then a per-item skip.

        Assignments, log calls and at least one failure record carrying the
        binding (imported ``record_residual_failure`` or an app writer,
        :meth:`_is_app_failure_record_call`) — directly or through a local
        built from it (``record = {... str(e) ...}``) — optionally ending in
        ``return`` / ``return None`` / ``continue``.  Also allowed:

        - a ``for`` loop whose body only assigns (``record[attr] = val``);
        - an ``if`` whose every branch records (or follows a record), logs or
          assigns, then returns any value — a recovered result is not a skip;
        - an empty literal (``return []``) after the record when a WARNING or
          ERROR also logged the binding through a sanitizer.

        The failure leaves the frame as a durable record, which is what the
        E007 prescription asks for at a per-item skip.
        """
        if node.name is None or _handler_rebinds_exception(node):
            return False
        outcome = self._records_then_exits(
            node.body, node, set(), recorded=False, warned=False, branch=False
        )
        return outcome is not None and outcome[1]

    # ── E004/E007: a failure recorded as a preflight check row ────────────────

    #: Preflight result constructors a recorded failure may be built into.
    _PREFLIGHT_ROWS = frozenset({"PreflightCheck", "PreflightOutput"})

    def _closure_helper(
        self, name: str, handler: ast.ExceptHandler
    ) -> ast.FunctionDef | None:
        """The nested ``def NAME`` an enclosing function declares, before *handler*.

        The innermost function on the stack that binds *name* must bind it only
        through one top-level, undecorated, synchronous ``def`` with no
        ``*args``/``**kwargs`` that precedes the handler.
        """
        for function in reversed(self._function_stack):
            if not _function_binds_name(function, name):
                continue
            definitions = [
                statement
                for statement in function.body
                if isinstance(statement, ast.FunctionDef) and statement.name == name
            ]
            if len(definitions) != 1:
                return None
            helper = definitions[0]
            if (
                helper.decorator_list
                or helper.args.vararg is not None
                or helper.args.kwarg is not None
                or helper.lineno >= handler.lineno
            ):
                return None
            parameters = [
                *function.args.posonlyargs,
                *function.args.args,
                *function.args.kwonlyargs,
            ]
            if any(parameter.arg == name for parameter in parameters):
                return None
            for node in _iter_shallow(function):
                if node is helper:
                    continue
                if (
                    isinstance(node, ast.Name)
                    and node.id == name
                    and isinstance(node.ctx, (ast.Store, ast.Del))
                ) or (
                    isinstance(
                        node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
                    )
                    and node.name == name
                ):
                    return None
                if isinstance(node, (ast.Nonlocal, ast.Global)) and name in node.names:
                    return None
                if isinstance(node, (ast.Import, ast.ImportFrom)) and any(
                    (alias.asname or alias.name.split(".")[0]) == name
                    for alias in node.names
                ):
                    return None
                if isinstance(node, ast.ExceptHandler) and node.name == name:
                    return None
            return helper
        return None

    def _is_typed_error_from_binding(self, value: ast.expr, exc_name: str) -> bool:
        """``self.X(exc, ...)``: a same-class method annotated ``-> SomeError``.

        The annotation must name an error class with no ``None``, and the bare
        binding must be among the arguments.
        """
        if not isinstance(value, ast.Call) or not any(
            isinstance(argument, ast.Name) and argument.id == exc_name
            for argument in [*value.args, *(kw.value for kw in value.keywords)]
        ):
            return False
        method = self._same_class_method(value, allow_static=True)
        return method is not None and _annotated_error_return(method) is False

    def _helper_appends_preflight_row(
        self, helper: ast.FunctionDef, parameter: str
    ) -> bool:
        """*helper* appends a preflight row built from *parameter* to a closure list.

        A top-level ``rows.append(PreflightCheck(..., <parameter> ...))`` where
        ``rows`` is not local to the helper, with nothing before it that could
        return or raise, and no rebinding of *parameter* anywhere.
        """
        if any(
            self._statement_rebinds_name(statement, parameter)
            for statement in helper.body
        ):
            return False
        for statement in helper.body:
            call = self._unwrapped_call(statement)
            if (
                call is not None
                and isinstance(call.func, ast.Attribute)
                and call.func.attr == "append"
                and isinstance(call.func.value, ast.Name)
                and not _function_binds_name(helper, call.func.value.id)
                and len(call.args) == 1
                and not call.keywords
                and isinstance(row := call.args[0], ast.Call)
                and (_get_name(row.func) or "").rsplit(".", 1)[-1]
                in self._PREFLIGHT_ROWS
                and any(
                    isinstance(inner, ast.Name) and inner.id == parameter
                    for argument in [*row.args, *(kw.value for kw in row.keywords)]
                    for inner in ast.walk(argument)
                )
            ):
                return True
            if any(
                isinstance(inner, (ast.Return, ast.Raise))
                for inner in [statement, *_iter_shallow(statement)]
            ):
                return False
        return False

    def _handler_records_preflight_failure(self, node: ast.ExceptHandler) -> bool:
        """The failure is recorded as a preflight check row, as typed data.

        ::

            except Exception as e:
                _record("probe", passed=False, error=self._typed_error(e, Denied))

        After optional constant assignments (``failed = True``), the handler's
        only statement calls a nested helper (:meth:`_closure_helper`), passing
        the binding wrapped in a same-class method annotated to return an error
        class (:meth:`_is_typed_error_from_binding`); the helper builds that
        argument into a ``PreflightCheck``/``PreflightOutput`` it appends to an
        enclosing list (:meth:`_helper_appends_preflight_row`).
        """
        if node.name is None or _handler_rebinds_exception(node) or not node.body:
            return False
        *setup, last = node.body
        if not all(
            isinstance(statement, (ast.Assign, ast.AnnAssign))
            and isinstance(statement.value, ast.Constant)
            for statement in setup
        ):
            return False
        call = self._unwrapped_call(last)
        if call is None or not isinstance(call.func, ast.Name):
            return False
        helper = self._closure_helper(call.func.id, node)
        if helper is None:
            return False
        parameters = [
            *helper.args.posonlyargs,
            *helper.args.args,
            *helper.args.kwonlyargs,
        ]
        return any(
            (argument := _call_argument_for_parameter(call, helper, parameter.arg))
            is not None
            and self._is_typed_error_from_binding(argument, node.name)
            and self._helper_appends_preflight_row(helper, parameter.arg)
            for parameter in parameters
        )

    @staticmethod
    def _is_narrow_catch(node: ast.ExceptHandler) -> bool:
        if node.type is None:
            return False
        types = node.type.elts if isinstance(node.type, ast.Tuple) else [node.type]
        return all(
            (name := _get_name(t)) is not None and name not in _BROAD_EXCEPT_TYPES
            for t in types
        )

    @staticmethod
    def _annotates_typed_result(helper: ast.FunctionDef) -> bool:
        """The return annotation is a bare class-like name.

        A subscripted or union annotation (``Optional[X]``, ``X | None``,
        ``Dict[str, Any]``) admits values that are not a typed result, and
        typing names are not classes.
        """
        annotation = helper.returns
        if not isinstance(annotation, (ast.Name, ast.Attribute)):
            return False
        leaf = annotation.id if isinstance(annotation, ast.Name) else annotation.attr
        return _class_like(leaf) and leaf not in _TYPING_NAMES

    def _is_typed_data_from_binding(
        self, value: ast.expr | None, exc_name: str | None
    ) -> bool:
        """A proven typed-result helper handed the caught exception itself.

        Same-class methods only (``self.X`` / ``cls.X`` / ``ClassName.X``,
        static ones included), proven by a class-like return annotation, with
        the bare binding among the call's arguments
        (``self.connectivity_verdict(exc)``): under a broad catch the failure
        must be what the typed value is built from.  Module helpers keep the
        stricter value-flow proof of :func:`_body_returns_typed_failure`.
        """
        if (
            exc_name is None
            or not isinstance(value, ast.Call)
            or not isinstance(value.func, ast.Attribute)
        ):
            return False
        if not any(
            isinstance(argument, ast.Name) and argument.id == exc_name
            for argument in [*value.args, *(kw.value for kw in value.keywords)]
        ):
            return False
        return self._is_typed_data_value(value)

    def _handler_returns_typed_data(self, node: ast.ExceptHandler) -> bool:
        """E004: straight-line setup, then ``return`` of a typed result built from it.

        The handler's only exit is its last statement,
        ``return helper(..., exc, ...)`` where the helper is proven by
        :meth:`_is_typed_data_from_binding`; everything before it is a plain
        assignment or expression statement.
        """
        if node.name is None or _handler_rebinds_exception(node) or not node.body:
            return False
        last = node.body[-1]
        if not isinstance(last, ast.Return):
            return False
        for statement in node.body[:-1]:
            if not isinstance(
                statement, (ast.Assign, ast.AnnAssign, ast.Expr, ast.Pass)
            ):
                return False
        if any(
            isinstance(inner, (ast.Return, ast.Raise, ast.Break, ast.Continue))
            for statement in node.body[:-1]
            for inner in [statement, *_iter_shallow(statement)]
        ):
            return False
        return self._is_typed_data_from_binding(last.value, node.name)

    def _is_typed_data_value(self, value: ast.expr) -> bool:
        """The result of a same-module helper proven to return a typed result object.

        The helper is a visible same-module function whose return annotation
        is a class-like name (no ``None``, no ``Optional``), or which never
        falls off its end and returns a class-like constructor on every path.
        A constructor written inline in the handler (``return Verdict()``) is
        not enough on its own: the existing E007 contract keeps asking for a
        log there.
        """
        if not isinstance(value, ast.Call):
            return False
        if isinstance(value.func, ast.Attribute):
            # ``self.X(...)`` / ``cls.X(...)`` / ``ClassName.X(...)`` on this
            # class: only the return annotation is accepted as proof.
            method = self._same_class_method(value, allow_static=True)
            return method is not None and self._annotates_typed_result(method)
        if not isinstance(value.func, ast.Name):
            return False
        helper = visible_helpers(self._local_helpers, self._function_stack).get(
            value.func.id
        )
        if helper is None:
            return False
        if self._annotates_typed_result(helper):
            return True
        returns = [
            stmt for stmt in _iter_block(helper.body) if isinstance(stmt, ast.Return)
        ]
        return (
            _body_always_exits(helper.body)
            and bool(returns)
            and all(
                isinstance(stmt.value, ast.Call)
                and _class_like(
                    (_get_name(stmt.value.func) or "").rsplit(".", 1)[-1] or None
                )
                for stmt in returns
            )
        )

    def _is_logged_sentinel_return(
        self, handler: ast.ExceptHandler, stmt: ast.Return
    ) -> bool:
        # Logging does not make a broad catch's empty return safe: the caller
        # still cannot tell the failure from a genuine empty result. A per-item
        # `None` after a durable residual record is different — the gap is
        # recorded as data, not just logged. Narrow catches stay out —
        # `except KeyError: ...; return None` is usually the lookup's contract.
        # So do procedures (`-> None`), where `return None` is a plain early
        # exit, and wholesale-deprecated modules, which are being removed
        # rather than fixed.
        if self._deprecated_module or not _is_empty_sentinel(stmt.value):
            return False
        if handler.type is None:
            broad = True
        elif isinstance(handler.type, ast.Tuple):
            broad = any(
                _get_name(element) in _BROAD_EXCEPT_TYPES
                for element in handler.type.elts
            )
        else:
            broad = _get_name(handler.type) in _BROAD_EXCEPT_TYPES
        if not broad:
            return False
        if self._has_residual_failure_record(handler, stmt):
            return False
        # Only `return None` is a procedure's early exit; `return []` from a
        # `-> None` function still hands the caller a plausible empty value.
        function = self._function_stack[-1] if self._function_stack else None
        returns = function.returns if function is not None else None
        return not (
            isinstance(returns, ast.Constant)
            and returns.value is None
            and isinstance(stmt.value, ast.Constant)
            and stmt.value.value is None
        )

    # ── E008 ─────────────────────────────────────────────────────────────────

    def _check_p008(self, node: ast.ExceptHandler) -> None:
        if node.type is None:
            return
        exc_type = _get_name(node.type)
        if exc_type not in _OPTIONAL_IMPORT_TYPES:
            return
        # An unconditional cause-preserving re-raise surfaces the missing
        # dependency instead of hiding it behind a silent fallback. An explicit
        # ``from`` replaces the implicit context, so it only counts when the
        # cause carries the caught ImportError.
        if (
            _body_always_raises(node.body)
            and not _body_has_bypassing_exit(node.body)
            and _raises_chain_caught(node)
        ):
            return
        if _any_logging_in(node.body):
            return
        # The rule's own text accepts an explanatory comment on the guard.
        end = getattr(node, "end_lineno", None) or node.lineno
        if any(node.lineno <= line <= end for line in self._comment_lines):
            return
        if any(self._is_reporter_call(stmt) for stmt in node.body):
            return
        self._add(
            "E008",
            node,
            f"'except {exc_type}' with no logging — import failures are silently hidden. "
            f"Log at DEBUG if the module is preferred but not required, or add a comment "
            f"explaining the optional dependency and use the suppression directive.",
        )

    #: Leaf-name fragments of a call that reports a problem to its user.
    _REPORTER_WORDS = ("error", "warn", "report", "fail", "fatal", "abort", "die")

    def _is_reporter_call(self, stmt: ast.stmt) -> bool:
        """A call that reports the missing module with a message, instead of a log.

        ``_error(path, "optional_codegen missing; cannot verify")``: the
        callee's leaf name says it reports a problem (error/warn/report/fail/
        fatal/abort/die) and one argument is a non-empty message string or
        f-string.  A silent fallback has neither.
        """
        call = self._unwrapped_call(stmt)
        if call is None:
            return False
        name = _get_name(call.func)
        leaf = (name.rsplit(".", 1)[-1] if name else "").lstrip("_").lower()
        if not any(word in leaf for word in self._REPORTER_WORDS):
            return False
        return any(
            (isinstance(arg, ast.Constant) and isinstance(arg.value, str) and arg.value)
            or isinstance(arg, ast.JoinedStr)
            for arg in [*call.args, *(kw.value for kw in call.keywords)]
        )

    # ── E009 ─────────────────────────────────────────────────────────────────

    def _check_p009(self, node: ast.ExceptHandler) -> None:
        real_stmts = [
            s
            for s in node.body
            if not (
                isinstance(s, ast.Expr)
                and isinstance(s.value, ast.Constant)
                and isinstance(s.value.value, str)
            )
        ]
        if not real_stmts:
            return
        if not all(
            isinstance(s, (ast.Assign, ast.AugAssign, ast.AnnAssign))
            for s in real_stmts
        ):
            return
        if _any_logging_in(node.body):
            return
        if self._handler_stashes_for_reraise(node):
            return
        self._add(
            "E009",
            node,
            "except block only assigns a variable — the exception is silently hidden. "
            "Add logger.warning(..., exc_info=True) before the assignment.",
        )

    # ── E010 ─────────────────────────────────────────────────────────────────

    def _check_p010_in_function(
        self, func_node: ast.FunctionDef | ast.AsyncFunctionDef
    ) -> None:
        """Flag asyncio.gather(return_exceptions=True) whose results are not inspected."""
        bare_gathers: list[ast.AST] = []
        gather_vars: dict[str, ast.AST] = {}

        for node in _iter_shallow(func_node):
            # Bare expression: gather result discarded entirely
            if isinstance(node, ast.Expr):
                val = node.value
                if isinstance(val, ast.Await):
                    val = val.value
                if isinstance(val, ast.Call) and _is_gather_call(val):
                    for kw in val.keywords:
                        if kw.arg == "return_exceptions":
                            v = kw.value
                            if isinstance(v, ast.Constant) and v.value is True:
                                bare_gathers.append(node)
                continue
            # Assigned: results = await asyncio.gather(..., return_exceptions=True)
            if isinstance(node, ast.Assign):
                val = node.value
                if isinstance(val, ast.Await):
                    val = val.value
                if isinstance(val, ast.Call) and _is_gather_call(val):
                    has_re = any(
                        kw.arg == "return_exceptions"
                        and isinstance(kw.value, ast.Constant)
                        and kw.value.value is True
                        for kw in val.keywords
                    )
                    if (
                        has_re
                        and len(node.targets) == 1
                        and isinstance(node.targets[0], ast.Name)
                    ):
                        # Only track single Name targets: chained assignments
                        # (a = b = ...) would emit one finding per target, and
                        # attribute targets (self.x = ...) produce false positives
                        # because the inspection-side check only matches ast.Name.
                        gather_vars[node.targets[0].id] = node

        cancelled = _cancelled_task_names(func_node)

        for node in bare_gathers:
            if _is_cancel_drain(node, cancelled):
                continue
            self._add(
                "E010",
                node,
                "asyncio.gather(return_exceptions=True) result is discarded — exception "
                "instances in the result list are silently ignored. "
                "Inspect results: 'for r in results: if isinstance(r, Exception): ...'",
            )

        local_helpers = visible_helpers(self._local_helpers, self._function_stack)
        for var_name, assign_node in gather_vars.items():
            # The results may be inspected under another name: callers routinely
            # copy them into an accumulator (`dest.extend(chunk_results)`) and
            # iterate that instead. Inspection of any such alias counts.
            names = _inspection_aliases(func_node, var_name)
            inspected = False
            for node in _iter_shallow(func_node):
                # isinstance(var, ...) direct check
                if isinstance(node, ast.Call):
                    func = node.func
                    if (
                        isinstance(func, ast.Name)
                        and func.id == "isinstance"
                        and node.args
                        and isinstance(node.args[0], ast.Name)
                        and node.args[0].id in names
                    ):
                        inspected = True
                        break
                # A comprehension checks per-item values under its bound name;
                # connect that name back to the gather result before counting it.
                if isinstance(
                    node, (ast.ListComp, ast.SetComp, ast.GeneratorExp, ast.DictComp)
                ) and _comprehension_checks_exceptions(node, names):
                    inspected = True
                    break
                # for r in var: ... — iteration counts as inspection
                if isinstance(node, ast.For):
                    # `for r in var:` — iterating the result list directly.
                    if isinstance(node.iter, ast.Name) and node.iter.id in names:
                        inspected = True
                        break
                    # `for i, r in enumerate(var):` / `for ... in zip(var, ...):` —
                    # iterating the result list through a standard combinator.
                    if isinstance(node.iter, ast.Call) and any(
                        isinstance(arg, ast.Name) and arg.id in names
                        for arg in node.iter.args
                    ):
                        inspected = True
                        break
                # `x = var[i]` / `x = var[0]` — subscripting the result list to
                # inspect elements one by one.
                if (
                    isinstance(node, ast.Subscript)
                    and isinstance(node.value, ast.Name)
                    and node.value.id in names
                ):
                    inspected = True
                    break
            if not inspected:
                for call in _iter_shallow(func_node):
                    if not isinstance(call, ast.Call) or not isinstance(
                        call.func, ast.Name
                    ):
                        continue
                    helper = local_helpers.get(call.func.id)
                    if helper is None:
                        continue
                    parameters = [
                        *helper.args.posonlyargs,
                        *helper.args.args,
                        *helper.args.kwonlyargs,
                    ]
                    if any(
                        isinstance(
                            argument := _call_argument_for_parameter(
                                call, helper, parameter.arg
                            ),
                            ast.Name,
                        )
                        and argument.id in names
                        and _helper_handles_gathered_results(helper, parameter.arg)
                        for parameter in parameters
                    ):
                        inspected = True
                        break
            if not inspected:
                self._add(
                    "E010",
                    assign_node,
                    f"asyncio.gather(return_exceptions=True) result '{var_name}' is not "
                    f"inspected for exception instances — errors vanish silently. "
                    f"Check each result: 'for r in {var_name}: if isinstance(r, Exception): ...'",
                )

    # ── E011 ─────────────────────────────────────────────────────────────────

    def _check_p011(self, cls: ast.ClassDef) -> None:
        if not _inherits_logging_filter(cls):
            return
        method = _find_filter_method(cls)
        if method is None:
            return
        if not _filter_body_wrapped(method):
            self._add(
                "E011",
                method,
                f"logging.Filter.filter() body in '{cls.name}' is not fully wrapped in "
                f"try/except — an unguarded exception crashes the logging caller "
                f"(Logger.handle() has no try/except around filters). "
                f"Wrap the entire body and return a safe fallback (True = pass-through).",
            )

    # ── E014 ─────────────────────────────────────────────────────────────────

    def _check_p014(self, node: ast.ExceptHandler) -> None:
        if not self._loop_stack:
            return
        if not _body_is_only_loop_control_no_logging(node.body):
            return
        exc_type = _get_name(node.type) if node.type else "(bare)"
        self._add(
            "E014",
            node,
            f"except {exc_type}: [continue/break/pass] inside a loop — exception is "
            f"silently swallowed. Log at DEBUG before the loop control statement.",
        )


def _raises_chain_caught(handler: ast.ExceptHandler) -> bool:
    """True when every ``raise ... from <cause>`` in *handler* chains the caught error.

    A bare ``raise`` or ``raise X(...)`` keeps the ImportError as implicit
    context. An explicit ``from`` replaces that context, so the cause must read
    the handler's bound name; an unbound handler has nothing to chain. Nested
    ``def``/``class`` bodies are skipped — their raises are not handler exits.
    """
    caught = frozenset({handler.name}) if handler.name else frozenset()
    for stmt in handler.body:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        for node in (stmt, *_iter_shallow(stmt)):
            if not isinstance(node, ast.Raise) or node.cause is None:
                continue
            if not any(
                isinstance(n, ast.Name) and n.id in caught for n in ast.walk(node.cause)
            ):
                return False
    return True


def _inspection_aliases(
    func_node: ast.FunctionDef | ast.AsyncFunctionDef, var_name: str
) -> set[str]:
    """Names that the gather results of *var_name* also flow into.

    A caller frequently copies the gather results into an accumulator and
    inspects *that* list::

        chunk_results = await asyncio.gather(*coros, return_exceptions=True)
        dest.extend(chunk_results)
        for i, result in enumerate(dest):
            if isinstance(result, Exception):
                ...

    Only whole-list copies are tracked -- ``dest.extend(var)``,
    ``dest.append(var)``, ``dest += var`` and ``dest = var`` -- and
    transitively, so a chain of copies resolves too. Element-wise copies are deliberately excluded: those
    require the caller to have touched each element already.
    """
    names = {var_name}
    changed = True
    while changed:
        changed = False
        for node in _iter_shallow(func_node):
            src: str | None = None
            dest: str | None = None
            # `dest.extend(var)` / `dest.append(var)`
            if isinstance(node, ast.Call):
                func = node.func
                if (
                    isinstance(func, ast.Attribute)
                    and func.attr in ("extend", "append")
                    and isinstance(func.value, ast.Name)
                    and len(node.args) == 1
                    and isinstance(node.args[0], ast.Name)
                ):
                    dest = func.value.id
                    src = node.args[0].id
            # `dest += var`
            elif (
                isinstance(node, ast.AugAssign)
                and isinstance(node.op, ast.Add)
                and isinstance(node.target, ast.Name)
                and isinstance(node.value, ast.Name)
            ):
                dest = node.target.id
                src = node.value.id
            # `dest = var` / `dest: list = var` — a plain rebinding
            elif (
                isinstance(node, (ast.Assign, ast.AnnAssign))
                and isinstance(node.value, ast.Name)
                and len(targets := _assign_targets(node)) == 1
                and isinstance(targets[0], ast.Name)
            ):
                dest = targets[0].id
                src = node.value.id
            if src in names and dest is not None and dest not in names:
                names.add(dest)
                changed = True
    return names


def _assign_targets(node: ast.Assign | ast.AnnAssign) -> list[ast.expr]:
    """Targets of an assignment; chained ``a = b = x`` yields several."""
    return node.targets if isinstance(node, ast.Assign) else [node.target]


def _is_exception_class_name(name: str) -> bool:
    """Whether *name* reads as an exception class (``AppError``, ``FooException``).

    Types cannot be resolved from the AST, so this leans on the PEP 8
    convention that exception class names end in ``Error`` or ``Exception``.
    """
    return name == "BaseException" or name.endswith(("Error", "Exception"))


def _is_exception_type(node: ast.AST) -> bool:
    """Whether an isinstance type expression names an exception class."""
    if isinstance(node, ast.Name):
        return _is_exception_class_name(node.id)
    if isinstance(node, ast.Attribute):
        # `builtins.Exception`, `errors.AppError`, `pkg.errors.AppError`
        return _is_exception_class_name(node.attr)
    if isinstance(node, ast.Tuple):
        return any(_is_exception_type(element) for element in node.elts)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        return _is_exception_type(node.left) or _is_exception_type(node.right)
    return False


def _condition_checks_exception(node: ast.AST, item_names: set[str]) -> bool:
    """Whether *node* positively tests a bound comprehension item as an exception.

    Negations (``not isinstance(r, Exception)``) and comparisons
    (``isinstance(r, Exception) == False``) are deliberately not followed: they
    select the successes and drop the failures, which is the silent swallow
    E010 exists to catch.
    """
    if isinstance(node, ast.Call):
        return (
            isinstance(node.func, ast.Name)
            and node.func.id == "isinstance"
            and len(node.args) >= 2
            and isinstance(node.args[0], ast.Name)
            and node.args[0].id in item_names
            and _is_exception_type(node.args[1])
        )
    if isinstance(node, ast.BoolOp):
        operands = _reachable_operands(node)
        # `isinstance(r, Exception) and False` never selects anything, so the
        # test it contains never takes effect.
        if isinstance(node.op, ast.And) and any(
            isinstance(value, ast.Constant) and not value.value for value in operands
        ):
            return False
        return any(_condition_checks_exception(value, item_names) for value in operands)
    if isinstance(node, ast.IfExp):
        return _condition_checks_exception(node.test, item_names)
    return False


def _reachable_operands(node: ast.BoolOp) -> list[ast.expr]:
    """Operands of *node* up to a literal that short-circuits the rest.

    ``False and isinstance(r, Exception)`` never evaluates the test, so it must
    not count as one. Only literal constants are folded; any other operand may
    take either value at runtime.
    """
    stop_on = isinstance(node.op, ast.Or)
    operands: list[ast.expr] = []
    for value in node.values:
        operands.append(value)
        if isinstance(value, ast.Constant) and bool(value.value) is stop_on:
            break
    return operands


def _result_item_names(
    iterable: ast.expr, target: ast.expr, result_names: set[str]
) -> set[str]:
    """Names in a comprehension *target* that bind one gathered result each.

    ``for r in rs`` binds ``r``. Through ``enumerate`` and ``zip`` only the
    target position fed by the result list counts, so the index in
    ``for i, r in enumerate(rs)`` or the partner in ``zip(keys, rs)`` is never
    mistaken for a result. Other calls may ignore their argument and are not
    followed.
    """
    if isinstance(iterable, ast.Name):
        if iterable.id in result_names and isinstance(target, ast.Name):
            return {target.id}
        return set()
    if not (
        isinstance(iterable, ast.Call)
        and isinstance(iterable.func, ast.Name)
        and iterable.func.id in {"enumerate", "zip"}
        and (
            not iterable.keywords
            # `zip(..., strict=True)` only; `strict=False` silently drops the
            # results past the shorter partner.
            or (
                iterable.func.id == "zip"
                and all(
                    keyword.arg == "strict"
                    and isinstance(keyword.value, ast.Constant)
                    and keyword.value.value is True
                    for keyword in iterable.keywords
                )
            )
        )
        and isinstance(target, (ast.Tuple, ast.List))
        # `zip(*cols, rs)` / `for *a, r in ...` hide the width, so positions
        # cannot be lined up.
        and not any(isinstance(arg, ast.Starred) for arg in iterable.args)
        and not any(isinstance(element, ast.Starred) for element in target.elts)
    ):
        return set()
    if iterable.func.id == "enumerate":
        # enumerate(rs) / enumerate(rs, start) yields (index, result)
        positions = [1] if _is_result_name(iterable.args[:1], result_names) else []
        width = 2
    else:
        positions = [
            index
            for index, arg in enumerate(iterable.args)
            if _is_result_name([arg], result_names)
        ]
        width = len(iterable.args)
    if len(target.elts) != width:
        return set()
    return {
        element.id
        for position in positions
        if isinstance(element := target.elts[position], ast.Name)
    }


def _is_result_name(args: list[ast.expr], result_names: set[str]) -> bool:
    """Whether *args* is exactly one plain reference to the gather results."""
    return (
        len(args) == 1 and isinstance(args[0], ast.Name) and args[0].id in result_names
    )


def _comprehension_checks_exceptions(
    node: ast.ListComp | ast.SetComp | ast.GeneratorExp | ast.DictComp,
    result_names: set[str],
) -> bool:
    """Whether a comprehension over the gather results tests each item as an exception.

    The test may sit in a filter (``[r for r in rs if isinstance(r, Exception)]``)
    or in the element itself (``any(isinstance(r, Exception) for r in rs)``).
    """
    item_names: set[str] = set()
    for generator in node.generators:
        item_names |= _result_item_names(generator.iter, generator.target, result_names)
    if not item_names:
        return False
    if isinstance(node, ast.DictComp):
        elements: list[ast.expr] = [node.key, node.value]
    else:
        elements = [node.elt]
    conditions = [c for generator in node.generators for c in generator.ifs]
    return any(
        _condition_checks_exception(expression, item_names)
        for expression in (*conditions, *elements)
    )


def _helper_handles_gathered_results(helper: ast.FunctionDef, parameter: str) -> bool:
    """Whether a local helper handles exception values from its results parameter.

    Only a direct per-item exception test followed by a raise or log is a proven
    handling boundary. Merely passing the results to an opaque helper, counting
    them, or continuing past a detected exception does not satisfy E010.
    """
    if any(
        isinstance(node, ast.Name)
        and node.id == parameter
        and isinstance(node.ctx, (ast.Store, ast.Del))
        for node in _iter_shallow(helper)
    ):
        return False

    for loop in _iter_shallow(helper):
        if not isinstance(loop, ast.For):
            continue
        item_names = _result_item_names(loop.iter, loop.target, {parameter})
        if not item_names:
            continue
        # The body only: `orelse` runs once after the loop, not per item.
        body = [
            *loop.body,
            *(node for statement in loop.body for node in _iter_shallow(statement)),
        ]
        # A nested loop or assignment that rebinds the item name means a later
        # test may be looking at something other than the gathered result.
        if any(
            isinstance(node, ast.Name)
            and node.id in item_names
            and isinstance(node.ctx, (ast.Store, ast.Del))
            for node in body
        ):
            continue
        for candidate in body:
            if not isinstance(candidate, ast.If):
                continue
            if not _condition_checks_exception(candidate.test, item_names):
                continue
            if any(
                isinstance(statement, ast.Raise) or _is_log_call_stmt(statement)
                for statement in candidate.body
            ):
                return True
    return False


def _cancelled_task_names(
    func_node: ast.FunctionDef | ast.AsyncFunctionDef,
) -> dict[str, int]:
    """Variables `.cancel()`ed in *func_node*, mapped to their earliest line.

    Covers both `for t in tasks: t.cancel()` and bare `task.cancel()` shapes.
    The line number is what makes the cancel-drain idiom distinguishable from
    a discarded gather: the documented idiom cancels *first* and then drains,
    so only a cancel that precedes the gather excuses it.
    """
    names: dict[str, int] = {}

    def _record(mapping: dict[str, int], name: str, lineno: int) -> None:
        if lineno < mapping.get(name, lineno + 1):
            mapping[name] = lineno

    for node in _iter_shallow(func_node):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr == "cancel"):
            continue
        target = func.value
        if isinstance(target, ast.Name):
            _record(names, target.id, node.lineno)
    # Resolve loop variables: `for t in tasks: ... t.cancel()` — if a
    # cancelled name is the loop variable of a `for ... in <name>:`, treat the
    # collection as cancelled too, from the line the loop starts on.
    resolved: dict[str, int] = dict(names)
    for node in _iter_shallow(func_node):
        if not isinstance(node, ast.For):
            continue
        if not isinstance(node.iter, ast.Name):
            continue
        target = node.target
        if isinstance(target, ast.Name) and target.id in names:
            _record(resolved, node.iter.id, node.lineno)
    return resolved


def _is_cancel_drain(gather_stmt: ast.AST, cancelled: dict[str, int]) -> bool:
    """True if *gather_stmt* is a bare ``await asyncio.gather(*tasks, ...)``
    whose positional iterable(s) were all cancelled *earlier* in the same
    function — the standard asyncio cancellation-drain idiom, where the
    results are by construction CancelledError instances and there is nothing
    to inspect. A cancel that comes after the gather is not a drain: it does
    not make the results the gather already discarded any less real.
    """
    if not isinstance(gather_stmt, ast.Expr):
        return False
    val = gather_stmt.value
    if isinstance(val, ast.Await):
        val = val.value
    if not (isinstance(val, ast.Call) and _is_gather_call(val)):
        return False
    positional = list(val.args)
    if not positional:
        return False
    for arg in positional:
        # `*tasks` (starred Name) or a bare `tasks` Name
        target = arg.value if isinstance(arg, ast.Starred) else arg
        if not isinstance(target, ast.Name):
            # e.g. a list literal or comprehension — cannot prove drain
            return False
        cancel_line = cancelled.get(target.id)
        if cancel_line is None or cancel_line >= gather_stmt.lineno:
            return False
    return True
