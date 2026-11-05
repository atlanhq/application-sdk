"""AST detection for P064 / P065 — offloaded work that a cancel cannot stop.

Both rules are exact syntax matches inside one module; neither guesses at how
long a call runs or whether a loop is "long".  Anything the match cannot see —
a callable defined in another module, a cursor handed in as an argument, a
batched result passed to a helper — is an accepted false negative.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator

from conformance.suite.checks._ast_common import _IgnoreDirective, make_finding
from conformance.suite.schema.findings import Finding

#: DB-API cursor methods that send a statement to the source.
_CURSOR_STATEMENT_METHODS = frozenset({"execute", "executemany"})

#: The SDK accessor for the running ``run_in_thread`` call's cancel handle.
_CANCEL_HANDLE_ACCESSOR = "current_cancel_handle"

_SCOPE_NODES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)


def _walk_local(node: ast.AST) -> Iterator[ast.AST]:
    """Walk *node*'s body without descending into nested functions or classes."""
    stack = list(ast.iter_child_nodes(node))
    while stack:
        child = stack.pop()
        yield child
        if not isinstance(child, _SCOPE_NODES):
            stack.extend(ast.iter_child_nodes(child))


def _is_cursor_call(node: ast.AST) -> bool:
    """``<expr>.cursor(...)``."""
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "cursor"
    )


def _cursor_names(scope: ast.AST) -> set[str]:
    """Names bound in *scope* from ``<expr>.cursor(...)``."""
    names: set[str] = set()
    for node in _walk_local(scope):
        if isinstance(node, ast.Assign) and _is_cursor_call(node.value):
            names.update(t.id for t in node.targets if isinstance(t, ast.Name))
        elif isinstance(node, (ast.With, ast.AsyncWith)):
            for item in node.items:
                if _is_cursor_call(item.context_expr) and isinstance(
                    item.optional_vars, ast.Name
                ):
                    names.add(item.optional_vars.id)
    return names


def _is_cursor_statement(node: ast.AST, cursors: set[str]) -> bool:
    """``<cursor>.execute`` / ``executemany`` on a cursor this scope opened."""
    if not (isinstance(node, ast.Attribute) and node.attr in _CURSOR_STATEMENT_METHODS):
        return False
    receiver = node.value
    return (isinstance(receiver, ast.Name) and receiver.id in cursors) or (
        _is_cursor_call(receiver)
    )


def _is_handle_lookup(node: ast.AST | None) -> bool:
    """``current_cancel_handle()`` or ``<module>.current_cancel_handle()``."""
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    return (isinstance(func, ast.Name) and func.id == _CANCEL_HANDLE_ACCESSOR) or (
        isinstance(func, ast.Attribute) and func.attr == _CANCEL_HANDLE_ACCESSOR
    )


def _registers_cancel(scope: ast.AST) -> bool:
    """Whether *scope* (nested functions included) registers a driver cancel.

    Only a ``.set(...)`` call on the call's handle registers one — on
    ``current_cancel_handle()`` itself, or on a name bound from it (``=`` or
    ``:=``).  Looking the handle up without setting it registers nothing.
    """
    handles: set[str] = set()
    for node in ast.walk(scope):
        if isinstance(node, (ast.Assign, ast.AnnAssign)) and _is_handle_lookup(
            node.value
        ):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            handles.update(t.id for t in targets if isinstance(t, ast.Name))
        elif isinstance(node, ast.NamedExpr) and _is_handle_lookup(node.value):
            handles.add(node.target.id)
    for node in ast.walk(scope):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "set"
            and node.args
        ):
            continue
        receiver = node.func.value
        if _is_handle_lookup(receiver) or (
            isinstance(receiver, ast.Name) and receiver.id in handles
        ):
            return True
    return False


def _unregistered_statement(scope: ast.AST) -> ast.Call | None:
    """The first cursor statement *scope* runs with no driver cancel registered."""
    if _registers_cancel(scope):
        return None
    cursors = _cursor_names(scope)
    calls = [
        node
        for node in _walk_local(scope)
        if isinstance(node, ast.Call) and _is_cursor_statement(node.func, cursors)
    ]
    return min(calls, key=lambda c: (c.lineno, c.col_offset)) if calls else None


def _is_run_in_thread(call: ast.Call) -> bool:
    """``run_in_thread(...)`` or ``<anything>.run_in_thread(...)``."""
    func = call.func
    return (isinstance(func, ast.Name) and func.id == "run_in_thread") or (
        isinstance(func, ast.Attribute) and func.attr == "run_in_thread"
    )


def _unwrap_partial(node: ast.AST) -> ast.AST:
    """See through ``functools.partial(fn, ...)`` / ``partial(fn, ...)``."""
    while (
        isinstance(node, ast.Call)
        and node.args
        and (
            (isinstance(node.func, ast.Name) and node.func.id == "partial")
            or (isinstance(node.func, ast.Attribute) and node.func.attr == "partial")
        )
    ):
        node = node.args[0]
    return node


def _is_bound(node: ast.AST) -> bool:
    """``<handle>.bind(fn)`` — the call carries a ``CancelHandle``."""
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "bind"
    )


class _Scopes:
    """Function and method definitions a ``run_in_thread`` argument can name."""

    def __init__(self, tree: ast.Module) -> None:
        self.module_functions: dict[str, ast.FunctionDef] = {
            n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)
        }
        self.parents: dict[ast.AST, ast.AST] = {}
        for parent in ast.walk(tree):
            for child in ast.iter_child_nodes(parent):
                self.parents[child] = parent

    def enclosing(self, node: ast.AST, kinds: tuple[type, ...]) -> ast.AST | None:
        current = self.parents.get(node)
        while current is not None and not isinstance(current, kinds):
            current = self.parents.get(current)
        return current

    def resolve(self, arg: ast.AST, call: ast.Call) -> ast.AST | None:
        """The function body *arg* names, if it is defined in this module."""
        if isinstance(arg, ast.Lambda):
            return arg
        if isinstance(arg, ast.Name):
            function = self.enclosing(call, (ast.FunctionDef, ast.AsyncFunctionDef))
            while function is not None:
                for node in _walk_local(function):
                    if isinstance(node, ast.FunctionDef) and node.name == arg.id:
                        return node
                function = self.enclosing(
                    function, (ast.FunctionDef, ast.AsyncFunctionDef)
                )
            return self.module_functions.get(arg.id)
        if (
            isinstance(arg, ast.Attribute)
            and isinstance(arg.value, ast.Name)
            and arg.value.id in ("self", "cls")
        ):
            cls = self.enclosing(call, (ast.ClassDef,))
            if isinstance(cls, ast.ClassDef):
                for node in cls.body:
                    if isinstance(node, ast.FunctionDef) and node.name == arg.attr:
                        return node
        return None


def check_p064(
    tree: ast.Module, filename: str, directives: dict[int, _IgnoreDirective]
) -> list[Finding]:
    """P064: a ``run_in_thread`` call runs a cursor statement it cannot cancel."""
    scopes = _Scopes(tree)
    findings: list[Finding] = []
    for call in ast.walk(tree):
        if not (isinstance(call, ast.Call) and _is_run_in_thread(call) and call.args):
            continue
        arg = _unwrap_partial(call.args[0])
        if _is_bound(arg):
            continue
        statement: ast.AST | None = None
        what = ""
        caller = scopes.enclosing(call, (ast.FunctionDef, ast.AsyncFunctionDef))
        if caller is not None and _is_cursor_statement(arg, _cursor_names(caller)):
            statement, what = arg, ast.unparse(arg)
        else:
            body = scopes.resolve(arg, call)
            if body is not None:
                statement = _unregistered_statement(body)
                what = ast.unparse(arg)
        if statement is None:
            continue
        findings.append(
            make_finding(
                filename=filename,
                rule_id="P064",
                node=call,
                message=(
                    f"`run_in_thread({what})` sends a DB-API cursor statement "
                    f"(line {statement.lineno}) with no driver cancel registered: "
                    "a cancel stops the await, but the query keeps running on the "
                    "source and holds this thread. Register the driver's cancel "
                    "from inside the offloaded function with "
                    "`current_cancel_handle().set(...)`, or bind a `CancelHandle`."
                ),
                directives=directives,
            )
        )
    return findings


def _is_batched_results_await(node: ast.AST | None) -> bool:
    """``await <expr>.get_batched_results(...)``."""
    return (
        isinstance(node, ast.Await)
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Attribute)
        and node.value.func.attr == "get_batched_results"
    )


def _bound_names(node: ast.AST) -> Iterator[str]:
    """Every name an assignment-like target binds."""
    for sub in ast.walk(node):
        if isinstance(sub, ast.Name) and isinstance(sub.ctx, ast.Store):
            yield sub.id


def _batched_result_names(function: ast.AsyncFunctionDef) -> set[str]:
    """Names bound exactly once in *function*, from ``await ....get_batched_results(...)``."""
    from_batched: set[str] = set()
    binding_counts: dict[str, int] = {}
    for node in _walk_local(function):
        if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                for name in _bound_names(target):
                    binding_counts[name] = binding_counts.get(name, 0) + 1
            if (
                isinstance(node, (ast.Assign, ast.AnnAssign))
                and _is_batched_results_await(node.value)
                and len(targets) == 1
                and isinstance(targets[0], ast.Name)
            ):
                from_batched.add(targets[0].id)
        elif isinstance(node, (ast.For, ast.AsyncFor, ast.With, ast.AsyncWith)):
            bound = (
                [node.target]
                if isinstance(node, (ast.For, ast.AsyncFor))
                else [i.optional_vars for i in node.items if i.optional_vars]
            )
            for target in bound:
                for name in _bound_names(target):
                    binding_counts[name] = binding_counts.get(name, 0) + 1
        elif isinstance(node, ast.NamedExpr):
            binding_counts[node.target.id] = binding_counts.get(node.target.id, 0) + 1
    return {name for name in from_batched if binding_counts.get(name) == 1}


def check_p065(
    tree: ast.Module, filename: str, directives: dict[int, _IgnoreDirective]
) -> list[Finding]:
    """P065: an ``async def`` iterates ``get_batched_results`` with a sync loop."""
    findings: list[Finding] = []
    for function in ast.walk(tree):
        if not isinstance(function, ast.AsyncFunctionDef):
            continue
        names = _batched_result_names(function)

        def _is_batched(iterable: ast.AST, names: set[str] = names) -> bool:
            return _is_batched_results_await(iterable) or (
                isinstance(iterable, ast.Name) and iterable.id in names
            )

        for node in _walk_local(function):
            sites: list[tuple[ast.AST, ast.AST]] = []
            if isinstance(node, ast.For):
                sites.append((node, node.iter))
            elif isinstance(
                node, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)
            ):
                sites.extend(
                    (node, gen.iter) for gen in node.generators if not gen.is_async
                )
            for site, iterable in sites:
                if not _is_batched(iterable):
                    continue
                findings.append(
                    make_finding(
                        filename=filename,
                        rule_id="P065",
                        node=site,
                        message=(
                            "`get_batched_results(...)` is iterated with a sync `for` "
                            "inside an `async def`: every chunk is fetched and built "
                            "on the event loop, which stalls heartbeats, and a cancel "
                            "cannot stop the read. Use `async for` — each chunk is "
                            "then read off the loop and a cancel stops the read."
                        ),
                        directives=directives,
                    )
                )
    return findings
