"""E001–E011 and E014 — silent exception swallowing rules."""

from __future__ import annotations

import ast

from .._ast_common._sanitizers import call_uses_sanitizer, is_sanitizer_call
from ._constants import _BROAD_EXCEPT_TYPES, _LOG_METHODS, _OPTIONAL_IMPORT_TYPES
from ._helpers import (
    _any_logging_in,
    _body_always_raises,
    _body_has_bypassing_exit,
    _body_is_only_loop_control_no_logging,
    _body_is_only_pass,
    _body_returns_typed_failure,
    _call_argument_for_parameter,
    _filter_body_wrapped,
    _find_filter_method,
    _get_name,
    _handler_rebinds_exception,
    _has_exc_info,
    _inherits_logging_filter,
    _is_empty_sentinel,
    _is_gather_call,
    _is_log_call_stmt,
    _iter_shallow,
    _return_carries_typed_failure,
    redaction_scope,
    typed_failure_scope,
    visible_helpers,
)


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
        scope = redaction_scope(node, sdk_errors=self._sdk_errors)
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
                # Do not summarize assignments, loops, or opaque calls before a
                # redacted log: their effect on whether the failure reaches it is
                # not proven by this deliberately small flow analysis.
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

        def _handler_logs_through_helper() -> bool:
            if node.name is None:
                return False
            for index, statement in enumerate(node.body):
                if not isinstance(statement, ast.Expr) or not isinstance(
                    statement.value, ast.Call
                ):
                    continue
                call = statement.value
                if not isinstance(call.func, ast.Name):
                    continue
                helper = local_helpers.get(call.func.id)
                if (
                    helper is None
                    or helper.decorator_list
                    or helper.args.vararg is not None
                    or helper.args.kwarg is not None
                ):
                    continue
                prefix = node.body[:index]
                if any(
                    not isinstance(
                        previous, (ast.Assign, ast.AnnAssign, ast.Expr, ast.Pass)
                    )
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
                parameters = [
                    *helper.args.posonlyargs,
                    *helper.args.args,
                    *helper.args.kwonlyargs,
                ]
                for parameter in parameters:
                    passed = _call_argument_for_parameter(call, helper, parameter.arg)
                    if (
                        isinstance(passed, ast.Name)
                        and passed.id == node.name
                        and _helper_logs_parameter(helper, parameter.arg)
                    ):
                        return True
            return False

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
        self._add(
            "E004",
            node,
            f"'except {exc_type}' catches everything. Acceptable only at top-level handlers "
            f"(worker loops, HTTP handlers) when logged with exc_info=True. "
            f"Narrow the exception type or add exc_info=True logging.",
        )

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
        self, class_node: ast.ClassDef, name: str
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
        if not self._supported_method_decorators(helper):
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
            # Same statement forms as the same-class path: a bare call, or one
            # whose result is assigned (``result = _report(problem)``).
            if isinstance(statement, (ast.Expr, ast.Assign, ast.AnnAssign)):
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
            if isinstance(statement, ast.Expr):
                expression = statement.value
            elif isinstance(statement, ast.Assign | ast.AnnAssign):
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
                helper = self._class_helper(class_node, call.func.attr)
                if helper is None:
                    continue
                # ``cls.method(x)`` on a plain method is unbound: ``x`` lands in
                # ``self``, so the descriptor-binding argument shift is wrong.
                if caller_is_classmethod and not helper.decorator_list:
                    continue
                argument = self._bound_method_argument(call, helper, handler.name)
                if (
                    isinstance(argument, ast.Name)
                    and argument.id == handler.name
                    and self._helper_logs_parameter_before_return(helper, handler.name)
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
        for i, stmt in enumerate(node.body):
            if not isinstance(stmt, ast.Return) or stmt.value is None:
                continue
            if scope is not None and _return_carries_typed_failure(stmt, scope):
                continue
            if not (
                _any_logging_in(node.body[:i])
                or self._helper_logs_caught_exception(node, node.body[:i])
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

    def _is_logged_sentinel_return(
        self, handler: ast.ExceptHandler, stmt: ast.Return
    ) -> bool:
        # Logging does not make a broad catch's empty return safe: the caller
        # still cannot tell the failure from a genuine empty result. Narrow
        # catches stay out — `except KeyError: ...; return None` is usually the
        # lookup's contract. So do procedures (`-> None`), where `return None`
        # is a plain early exit, and wholesale-deprecated modules, which are
        # being removed rather than fixed.
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
        self._add(
            "E008",
            node,
            f"'except {exc_type}' with no logging — import failures are silently hidden. "
            f"Log at DEBUG if the module is preferred but not required, or add a comment "
            f"explaining the optional dependency and use the suppression directive.",
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
