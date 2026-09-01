"""E001–E011 and E014 — silent exception swallowing rules."""

from __future__ import annotations

import ast

from .._ast_common._sanitizers import call_uses_sanitizer
from ._constants import _BROAD_EXCEPT_TYPES, _OPTIONAL_IMPORT_TYPES
from ._helpers import (
    _any_logging_in,
    _body_always_raises,
    _body_has_bypassing_exit,
    _body_is_only_loop_control_no_logging,
    _body_is_only_pass,
    _filter_body_wrapped,
    _find_filter_method,
    _get_name,
    _has_exc_info,
    _inherits_logging_filter,
    _is_gather_call,
    _iter_function_body,
    _iter_shallow,
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
            if self._is_documented_best_effort(node):
                return
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

    def _is_documented_best_effort(self, node: ast.ExceptHandler) -> bool:
        """Recognize the small set of documented, result-preserving no-ops.

        E002 is intentionally not made comment-driven: a comment alone cannot
        establish that swallowing an exception is safe. These are the concrete
        best-effort seams used by applications: heartbeat callbacks, JSON list
        parsing that falls back to the original value, and destructor cleanup.
        """
        if not self._try_stack:
            return False
        try_node = self._try_stack[-1]
        function = self._function_stack[-1] if self._function_stack else None
        if function is None:
            return False
        comments = self._source.lower().splitlines()
        start = max(0, function.lineno - 1)
        end = min(len(comments), try_node.lineno - 1)
        docstring = (
            function.body[0].value.value
            if function.body
            and isinstance(function.body[0], ast.Expr)
            and isinstance(function.body[0].value, ast.Constant)
            and isinstance(function.body[0].value.value, str)
            else ""
        )
        documented = (
            any(
                "best-effort" in line or "best effort" in line
                for line in comments[max(start, end - 4) : end]
            )
            or "best-effort" in docstring.lower()
            or "best effort" in docstring.lower()
        )
        if not documented:
            return False

        # Heartbeat/rate-reporting callbacks are optional side effects.
        if len(try_node.body) == 1 and isinstance(try_node.body[0], ast.Expr):
            call = try_node.body[0].value
            if isinstance(call, ast.Call) and _get_name(call.func) == "heartbeat_fn":
                return True

        # JSON list parsing may retain the original value when decoding fails.
        caught_names = (
            [_get_name(element) for element in node.type.elts]
            if isinstance(node.type, ast.Tuple)
            else [_get_name(node.type)]
        )
        if set(caught_names) & {"JSONDecodeError", "ValueError"}:
            if len(try_node.body) == 1 and isinstance(try_node.body[0], ast.Assign):
                assignment = try_node.body[0]
                call = assignment.value
                if (
                    len(assignment.targets) == 1
                    and isinstance(assignment.targets[0], ast.Name)
                    and isinstance(call, ast.Call)
                    and _get_name(call.func) == "loads"
                    and len(call.args) == 1
                    and isinstance(call.args[0], ast.Name)
                ):
                    original_name = call.args[0].id
                    return any(
                        isinstance(n, ast.Return)
                        and isinstance(n.value, ast.Name)
                        and n.value.id == original_name
                        and n.lineno > (try_node.end_lineno or try_node.lineno)
                        for n in _iter_function_body(function.body)
                    )

        # Destructor cleanup releases a resource whose close failure is already
        # handled and logged by the close method itself.
        if function.name == "__del__" and len(try_node.body) == 1:
            stmt = try_node.body[0]
            if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call):
                return _get_name(stmt.value.func) == "close"
        return False

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
        # Pass only if the handler re-raises with the trace preserved on *every*
        # path (bare `raise`, `raise X(...)`, or `raise X(...) from e`): a broad
        # catch that translates-and-chains swallows nothing. A guaranteeing raise
        # alone is not enough — a preceding `return`/`break`/`continue` (swallow)
        # or `raise ... from None` (trace-loss) bypasses it, so those disqualify.
        if _body_always_raises(node.body) and not _body_has_bypassing_exit(node.body):
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
                call
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
            if call_uses_sanitizer(call):
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

    def _check_p007(self, node: ast.ExceptHandler) -> None:
        for i, stmt in enumerate(node.body):
            if not isinstance(stmt, ast.Return) or stmt.value is None:
                continue
            if _any_logging_in(node.body[:i]):
                continue
            self._add(
                "E007",
                stmt,
                "except block returns a value without logging — the error is hidden. "
                "Log before returning or raise a domain-specific exception.",
            )

    # ── E008 ─────────────────────────────────────────────────────────────────

    def _check_p008(self, node: ast.ExceptHandler) -> None:
        if node.type is None:
            return
        exc_type = _get_name(node.type)
        if exc_type not in _OPTIONAL_IMPORT_TYPES:
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
                    if has_re:
                        # Only track single Name targets: chained assignments
                        # (a = b = ...) would emit one finding per target, and
                        # attribute targets (self.x = ...) produce false positives
                        # because the inspection-side check only matches ast.Name.
                        if len(node.targets) == 1 and isinstance(
                            node.targets[0], ast.Name
                        ):
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
                    if isinstance(func, ast.Name) and func.id == "isinstance":
                        if node.args and isinstance(node.args[0], ast.Name):
                            if node.args[0].id in names:
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
                if isinstance(node, ast.Subscript):
                    if isinstance(node.value, ast.Name) and node.value.id in names:
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
    ``dest.append(var)`` and ``dest += var`` -- and transitively, so a chain of
    copies resolves too. Element-wise copies are deliberately excluded: those
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
            if src in names and dest is not None and dest not in names:
                names.add(dest)
                changed = True
    return names


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
