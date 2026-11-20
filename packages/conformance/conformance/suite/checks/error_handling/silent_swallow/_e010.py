"""E010 — ``asyncio.gather(..., return_exceptions=True)`` results never inspected."""

from __future__ import annotations

import ast

from .._helpers import (
    _call_argument_for_parameter,
    _is_gather_call,
    _is_log_call_stmt,
    _iter_shallow,
    visible_helpers,
)


class E010Mixin:
    """Rule methods for E010."""

    _import_origins: dict[str, str]

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
                    if not isinstance(call, ast.Call):
                        continue
                    helper_call = call
                    helper: ast.FunctionDef | None = None
                    if isinstance(call.func, ast.Name):
                        helper = local_helpers.get(call.func.id)
                        if (
                            helper is None
                            and self._import_origins.get(call.func.id)
                            == "application_sdk.execution.heartbeat.run_in_thread"
                            and call.args
                            and isinstance(call.args[0], ast.Name)
                        ):
                            # run_in_thread takes the consumer as its first argument
                            # and forwards the remaining arguments to that helper.
                            helper = local_helpers.get(call.args[0].id)
                            helper_call = ast.Call(
                                func=call.args[0],
                                args=call.args[1:],
                                keywords=call.keywords,
                            )
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
                                helper_call, helper, parameter.arg
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
    them, or using a bare ``continue`` for an exception does not satisfy E010;
    logging the exception before continuing does.
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
