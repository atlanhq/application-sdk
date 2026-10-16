"""E015, E016, E019 — exception-chaining and message-hygiene rules."""

from __future__ import annotations

import ast
from pathlib import Path

from ._helpers import (
    _call_argument_for_parameter,
    _dotted_name,
    _function_binds_name,
    _get_name,
    _message_kw_has_exc_text,
    _sdk_error_takes_cause,
    module_helpers,
)
from ._sdk_errors import names_sdk_error, sdk_error_names

ExceptionHelper = tuple[ast.FunctionDef, frozenset[str], ast.Module]


def _imported_module_file(
    module: str | None, level: int, source: Path, root: Path
) -> Path | None:
    """Resolve an import to a Python module inside the scanned repository."""
    resolved_root = root.resolve()
    try:
        relative_source = source.resolve().relative_to(resolved_root)
    except ValueError:
        return None

    if level:
        parts = list(relative_source.parent.parts)
        climb = level - 1
        if climb > len(parts):
            return None
        if climb:
            parts = parts[:-climb]
    else:
        parts = []
    if module:
        parts.extend(module.split("."))
    if not parts:
        return None

    candidates = (
        resolved_root.joinpath(*parts).with_suffix(".py"),
        resolved_root.joinpath(*parts, "__init__.py"),
    )
    for candidate in candidates:
        if candidate.is_file() and candidate.resolve().is_relative_to(resolved_root):
            return candidate.resolve()
    return None


def _bound_name(node: ast.Import | ast.ImportFrom, alias: ast.alias) -> str:
    if alias.asname:
        return alias.asname
    if isinstance(node, ast.Import):
        return alias.name.split(".")[0]
    return alias.name


def _import_binding_is_unique(
    tree: ast.Module,
    import_node: ast.ImportFrom,
    imported_alias: ast.alias,
    local_name: str,
) -> bool:
    """Reject imported helpers shadowed or rebound anywhere in their module."""
    matching_imports = 0
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                if _bound_name(node, alias) == local_name:
                    matching_imports += 1
                    if node is not import_node or alias is not imported_alias:
                        return False
        if (
            isinstance(node, ast.Name)
            and node.id == local_name
            and isinstance(node.ctx, (ast.Store, ast.Del))
        ):
            return False
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and (
            node.name == local_name
        ):
            return False
        if isinstance(node, ast.arg) and node.arg == local_name:
            return False
        if isinstance(node, ast.ExceptHandler) and node.name == local_name:
            return False
        if isinstance(node, (ast.Global, ast.Nonlocal)) and local_name in node.names:
            return False
    return matching_imports == 1


def exception_helpers(
    tree: ast.Module,
    *,
    path: Path | None,
    root: Path | None,
    sdk_errors: frozenset[str],
) -> dict[str, ExceptionHelper]:
    """Resolve stable local/imported functions that may classify a raised error.

    Resolution is limited to unshadowed top-level function imports from source
    files in the scanned repository. The later E016 check still proves the
    function's return paths and SDK cause flow before accepting a call.
    """
    resolved: dict[str, ExceptionHelper] = {
        name: (function, sdk_errors, tree)
        for name, function in module_helpers(tree).items()
    }
    if path is None or root is None:
        return resolved

    for import_node in tree.body:
        if not isinstance(import_node, ast.ImportFrom):
            continue
        module_path = _imported_module_file(
            import_node.module, import_node.level, path, root
        )
        if module_path is None:
            continue
        try:
            imported_tree = ast.parse(module_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, SyntaxError):
            continue
        imported_functions = module_helpers(imported_tree)
        imported_sdk_errors = sdk_error_names(
            imported_tree, path=module_path, root=root
        )
        for alias in import_node.names:
            local_name = _bound_name(import_node, alias)
            if not _import_binding_is_unique(tree, import_node, alias, local_name):
                continue
            function = imported_functions.get(alias.name)
            if function is not None:
                resolved[local_name] = (
                    function,
                    imported_sdk_errors,
                    imported_tree,
                )
    return resolved


def _module_rebinds_name(tree: ast.Module, name: str) -> bool:
    """Whether a module changes a name used as a builtin or SDK class."""
    import_count = 0
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Name)
            and node.id == name
            and isinstance(node.ctx, (ast.Store, ast.Del))
        ):
            return True
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and (
            node.name == name
        ):
            return True
        if isinstance(node, ast.arg) and node.arg == name:
            return True
        if isinstance(node, ast.ExceptHandler) and node.name == name:
            return True
        if isinstance(node, (ast.Global, ast.Nonlocal)) and name in node.names:
            return True
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            import_count += sum(
                _bound_name(node, alias) == name for alias in node.names
            )
    return import_count > 1


def _stable_name(name: str, function: ast.FunctionDef, module: ast.Module) -> bool:
    return not _function_binds_name(function, name) and not _module_rebinds_name(
        module, name
    )


def _class_expression_is_stable(
    expression: ast.expr, function: ast.FunctionDef, module: ast.Module
) -> bool:
    if isinstance(expression, ast.Tuple):
        return bool(expression.elts) and all(
            _class_expression_is_stable(item, function, module)
            for item in expression.elts
        )
    dotted = _dotted_name(expression)
    if dotted is None:
        return False
    root_name = (
        expression.id if isinstance(expression, ast.Name) else expression.value.id
    )
    return _stable_name(root_name, function, module)


def _isinstance_test_is_safe(
    expression: ast.expr, function: ast.FunctionDef, module: ast.Module
) -> bool:
    if (
        not isinstance(expression, ast.Call)
        or not isinstance(expression.func, ast.Name)
        or expression.func.id != "isinstance"
        or len(expression.args) != 2
        or expression.keywords
        or not _stable_name("isinstance", function, module)
        or not isinstance(expression.args[0], ast.Name)
    ):
        return False
    return _class_expression_is_stable(expression.args[1], function, module)


def _is_sdk_error_type(
    expression: ast.expr,
    sdk_errors: frozenset[str],
    function: ast.FunctionDef,
    module: ast.Module,
) -> bool:
    if isinstance(expression, ast.Tuple):
        return bool(expression.elts) and all(
            _is_sdk_error_type(item, sdk_errors, function, module)
            for item in expression.elts
        )
    dotted = _dotted_name(expression)
    if dotted is None or not names_sdk_error(sdk_errors, dotted):
        return False
    root_name = (
        expression.id if isinstance(expression, ast.Name) else expression.value.id
    )
    return _stable_name(root_name, function, module)


def _is_sdk_error_guard(
    expression: ast.expr,
    exception_parameter: str,
    sdk_errors: frozenset[str],
    function: ast.FunctionDef,
    module: ast.Module,
) -> bool:
    return (
        _isinstance_test_is_safe(expression, function, module)
        and isinstance(expression, ast.Call)
        and isinstance(expression.args[0], ast.Name)
        and expression.args[0].id == exception_parameter
        and _is_sdk_error_type(expression.args[1], sdk_errors, function, module)
    )


def _pure_value(
    expression: ast.expr, function: ast.FunctionDef, module: ast.Module
) -> bool:
    """Accept simple values and builtin type introspection, not opaque calls."""
    if isinstance(expression, (ast.Constant, ast.Name)):
        return True
    if isinstance(expression, ast.Attribute):
        return _pure_value(expression.value, function, module)
    if isinstance(expression, ast.Call):
        return (
            isinstance(expression.func, ast.Name)
            and expression.func.id == "type"
            and len(expression.args) == 1
            and not expression.keywords
            and _stable_name("type", function, module)
            and _pure_value(expression.args[0], function, module)
        )
    if isinstance(expression, ast.JoinedStr):
        return all(_pure_value(value, function, module) for value in expression.values)
    if isinstance(expression, ast.FormattedValue):
        return _pure_value(expression.value, function, module) and (
            expression.format_spec is None
            or _pure_value(expression.format_spec, function, module)
        )
    if isinstance(expression, (ast.Tuple, ast.List, ast.Set)):
        return all(_pure_value(value, function, module) for value in expression.elts)
    if isinstance(expression, ast.Dict):
        return all(
            key is not None and _pure_value(key, function, module)
            for key in expression.keys
        ) and all(_pure_value(value, function, module) for value in expression.values)
    if isinstance(expression, (ast.BinOp, ast.UnaryOp, ast.BoolOp, ast.Compare)):
        return all(
            _pure_value(child, function, module)
            for child in ast.iter_child_nodes(expression)
            if isinstance(child, ast.expr)
        )
    if isinstance(expression, ast.IfExp):
        return all(
            _pure_value(child, function, module)
            for child in (expression.test, expression.body, expression.orelse)
        )
    if isinstance(expression, ast.Subscript):
        return _pure_value(expression.value, function, module) and _pure_value(
            expression.slice, function, module
        )
    if isinstance(expression, ast.Slice):
        return all(
            item is None or _pure_value(item, function, module)
            for item in (expression.lower, expression.upper, expression.step)
        )
    return False


def _sdk_error_return(
    expression: ast.expr,
    exception_parameter: str,
    sdk_errors: frozenset[str],
    function: ast.FunctionDef,
    module: ast.Module,
) -> bool:
    if (
        not isinstance(expression, ast.Call)
        or not _sdk_error_takes_cause(expression, exception_parameter, sdk_errors)
        or any(isinstance(argument, ast.Starred) for argument in expression.args)
        or any(keyword.arg is None for keyword in expression.keywords)
    ):
        return False
    cause_keywords = [
        keyword for keyword in expression.keywords if keyword.arg == "cause"
    ]
    dotted = _dotted_name(expression.func)
    if (
        len(cause_keywords) != 1
        or dotted is None
        or not names_sdk_error(sdk_errors, dotted)
    ):
        return False
    root_name = (
        expression.func.id
        if isinstance(expression.func, ast.Name)
        else expression.func.value.id
    )
    if not _stable_name(root_name, function, module):
        return False
    return all(
        _pure_value(argument, function, module) for argument in expression.args
    ) and all(
        _pure_value(keyword.value, function, module) for keyword in expression.keywords
    )


def _helper_preserves_cause(
    function: ast.FunctionDef,
    exception_parameter: str,
    sdk_errors: frozenset[str],
    module: ast.Module,
) -> bool:
    """Prove each return carries the input as an SDK cause or typed error."""
    if function.decorator_list:
        return False
    body = list(function.body)
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        body.pop(0)
    if not body or not isinstance(body[-1], ast.Return):
        return False
    if any(
        isinstance(node, ast.Name)
        and node.id == exception_parameter
        and isinstance(node.ctx, (ast.Store, ast.Del))
        for node in ast.walk(function)
    ):
        return False

    def returns_preserve(stmts: list[ast.stmt], guarded_sdk_error: bool) -> bool:
        for statement in stmts:
            if isinstance(statement, ast.Return):
                value = statement.value
                if isinstance(value, ast.Name) and value.id == exception_parameter:
                    if not guarded_sdk_error:
                        return False
                elif not _sdk_error_return(
                    value, exception_parameter, sdk_errors, function, module
                ):
                    return False
            elif isinstance(statement, ast.If):
                if not _isinstance_test_is_safe(statement.test, function, module):
                    return False
                positive_guard = _is_sdk_error_guard(
                    statement.test,
                    exception_parameter,
                    sdk_errors,
                    function,
                    module,
                )
                if not returns_preserve(
                    statement.body, guarded_sdk_error or positive_guard
                ) or not returns_preserve(statement.orelse, guarded_sdk_error):
                    return False
            elif isinstance(statement, ast.Pass):
                continue
            else:
                return False
        return True

    return returns_preserve(body, False)


def _helper_call_preserves_cause(
    expression: ast.expr,
    exception_name: str,
    helpers: dict[str, ExceptionHelper],
    enclosing_functions: list[ast.FunctionDef | ast.AsyncFunctionDef],
) -> bool:
    if not isinstance(expression, ast.Call) or not isinstance(
        expression.func, ast.Name
    ):
        return False
    helper_name = expression.func.id
    if any(
        _function_binds_name(function, helper_name) for function in enclosing_functions
    ):
        return False
    helper_info = helpers.get(helper_name)
    if helper_info is None:
        return False
    helper, sdk_errors, module = helper_info
    parameters = [*helper.args.posonlyargs, *helper.args.args, *helper.args.kwonlyargs]
    for parameter in parameters:
        argument = _call_argument_for_parameter(expression, helper, parameter.arg)
        if (
            isinstance(argument, ast.Name)
            and argument.id == exception_name
            and _helper_preserves_cause(helper, parameter.arg, sdk_errors, module)
        ):
            return True
    return False


class ExceptionChainingMixin:
    """Rule methods for E015, E016 and E019 (exception-chaining category)."""

    # ── E015 ─────────────────────────────────────────────────────────────────

    def _check_p015(self, node: ast.Raise) -> None:
        if node.exc is None or not isinstance(node.exc, ast.Call):
            return
        exc_binding: str | None = None
        if self._except_stack:
            exc_binding = self._except_stack[-1].name
        for kw in node.exc.keywords:
            if kw.arg == "message" and _message_kw_has_exc_text(kw.value, exc_binding):
                exc_type = _get_name(node.exc.func) or "Error"
                self._add(
                    "E015",
                    node,
                    f"message= on {exc_type} contains interpolated exception text "
                    f"(f-string/str(exc)/repr(exc)) — leaks unsanitised text and breaks "
                    f"dashboard grouping. Put context in a typed evidence field instead.",
                )
                return

    # ── E019 ─────────────────────────────────────────────────────────────────

    def _check_e019(self, node: ast.Call) -> None:
        # Non-raise counterpart of E015: exception text interpolated into the
        # message= of a contract DTO constructed inside an except block — whether
        # the DTO is returned directly (`return AuthOutput(message=str(e))`) or
        # appended/assigned for a later return
        # (`checks.append(PreflightCheck(message=f"...{e}"))`). E015 owns the raise
        # case; _in_raise_call lets that path skip here so the two never overlap.
        if self._in_raise_call:
            return
        if not self._except_stack:
            return
        exc_binding = self._except_stack[-1].name
        if exc_binding is None:
            return
        for kw in node.keywords:
            if kw.arg == "message" and _message_kw_has_exc_text(kw.value, exc_binding):
                ctor = _get_name(node.func) or "the contract"
                self._add(
                    "E019",
                    node,
                    f"message= on {ctor} contains interpolated exception text "
                    f"(f-string/str(exc)/repr(exc)) — leaks unsanitised text across the "
                    f"typed boundary and breaks dashboard grouping. Classify into a typed "
                    f"AppError and return it as error=err.to_failure_details() — a failed "
                    f"result renders error.message, so no message= is needed; a fixed "
                    f"string clears the rule but is not the default fix.",
                )
                return

    # ── E016 ─────────────────────────────────────────────────────────────────

    def _check_p016(self, node: ast.Raise) -> None:
        if node.exc is None:
            return  # bare re-raise — always acceptable
        if node.cause is not None:
            return  # has 'from X' or 'from None' — acceptable
        if not self._except_stack:
            return
        handler = self._except_stack[-1]
        if handler.name is None:
            return  # no binding (except SomeError:) — chaining not applicable
        exc_binding = handler.name
        if _sdk_error_takes_cause(node.exc, exc_binding, self._sdk_errors):
            return  # SDK AppError serializes its typed cause on the wire
        if _helper_call_preserves_cause(
            node.exc, exc_binding, self._exception_helpers, self._function_stack
        ):
            return  # a resolved classifier preserves the cause on every return path
        exc_name = _get_name(node.exc) or "NewError"
        self._add(
            "E016",
            node,
            f"raise {exc_name} inside 'except ... as {exc_binding}:' is missing "
            f"'from {exc_binding}' or a proven SDK cause. The original exception is "
            f"lost in AE dashboards. Use 'raise {exc_name}(...) from {exc_binding}' "
            f"or pass the exception as the SDK error's cause.",
        )
