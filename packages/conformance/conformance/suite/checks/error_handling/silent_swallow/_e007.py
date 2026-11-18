"""E007 — a broad ``except`` that returns an empty sentinel.

Also holds the typed-result and failure-record exemptions, including the
E004/E007 preflight-row exemption, which E004 reuses through ``self``."""

from __future__ import annotations

import ast

from ..._ast_common._sanitizers import (
    call_uses_sanitizer,
    expr_sanitizes_name,
    is_sanitizer_call,
)
from .._constants import _BROAD_EXCEPT_TYPES, _LOG_METHODS
from .._helpers import (
    _annotated_error_return,
    _any_logging_in,
    _body_always_exits,
    _call_argument_for_parameter,
    _class_like,
    _dotted_name,
    _function_binds_name,
    _get_name,
    _handler_rebinds_exception,
    _is_empty_sentinel,
    _is_log_call_stmt,
    _iter_block,
    _iter_shallow,
    _return_carries_typed_failure,
    typed_failure_scope,
    visible_helpers,
)

#: Verbs that make a ``*failure*`` / ``*residual*`` call a durable writer.
_RECORD_VERBS = ("write", "record", "store", "persist", "save")

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


class E007Mixin:
    """Rule methods for E007."""

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
                *(call for child in children for call in E007Mixin._eager_calls(child)),
            ]
        if isinstance(expression, (ast.List, ast.Tuple, ast.Set)):
            return [
                call
                for element in expression.elts
                if not isinstance(element, ast.Starred)
                for call in E007Mixin._eager_calls(element)
            ]
        if isinstance(expression, ast.Dict):
            return [
                call
                for element in (*expression.keys, *expression.values)
                if element is not None
                for call in E007Mixin._eager_calls(element)
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

    def _is_awaited_offloaded_residual_failure_record(
        self, stmt: ast.stmt, call: ast.Call | None
    ) -> bool:
        """Whether an awaited SDK thread offload invokes the imported residual writer."""
        if (
            call is None
            or not isinstance(stmt, ast.Expr)
            or not isinstance(stmt.value, ast.Await)
            or stmt.value.value is not call
            or not isinstance(call.func, ast.Name)
            or self._import_origins.get(call.func.id)
            != "application_sdk.execution.heartbeat.run_in_thread"
            or not call.args
            or any(isinstance(argument, ast.Starred) for argument in call.args)
        ):
            return False
        # Apply the direct writer's provenance and required-argument checks to
        # the callable and arguments forwarded through run_in_thread.
        writer_call = ast.Call(
            func=call.args[0],
            args=call.args[1:],
            keywords=call.keywords,
        )
        return self._is_residual_failure_record_call(writer_call)

    def _has_residual_failure_record(
        self, handler: ast.ExceptHandler, stmt: ast.Return
    ) -> bool:
        """True when the handler writes a residual record before a per-item ``None``.

        A record may be written directly or by an awaited SDK ``run_in_thread``
        offload. An empty aggregate (``{}``, ``[]``) still reads as a genuine
        empty result to the caller, so it qualifies only when a WARNING/ERROR
        also logged the binding through a sanitizer before the return. The
        record may read the binding through a local built from it.
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
            call = self._unwrapped_call(previous)
            if self._is_failure_record_call(call, handler.name, carriers) or (
                self._is_awaited_offloaded_residual_failure_record(previous, call)
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
                E007Mixin._only_assigns(statement.body)
                and E007Mixin._only_assigns(statement.orelse)
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
