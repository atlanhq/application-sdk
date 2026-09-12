"""Trace error expressions to failed-check outputs without grading intermediate errors."""

from __future__ import annotations

import ast
from dataclasses import dataclass, field

from ._common import Source, find_preflight_check_sites
from ._contracts import Function, _Checker, _kwargs, _literal, _nodes, _qualified, _sdk


@dataclass
class Context:
    source: Source
    function: Function
    arguments: dict[str, Reference] = field(default_factory=dict)


@dataclass
class Reference:
    node: ast.AST
    context: Context


def _returns(statements):
    for stmt in statements:
        if isinstance(stmt, ast.Return):
            yield stmt
            break
        if isinstance(stmt, ast.Raise):
            break
        if isinstance(stmt, ast.If) and isinstance(stmt.test, ast.Constant):
            selected = stmt.body if stmt.test.value else stmt.orelse
            yield from _returns(selected)
            if selected and isinstance(selected[-1], (ast.Return, ast.Raise)):
                break
        elif not isinstance(
            stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
        ):
            for _, value in ast.iter_fields(stmt):
                if isinstance(value, list) and all(
                    isinstance(item, ast.stmt) for item in value
                ):
                    yield from _returns(value)


class ErrorFlow:
    def __init__(self, checker: _Checker):
        self.checker = checker

    def child(self, context: Context, call: ast.Call) -> Context | None:
        if isinstance(call.func, ast.Name):
            parameters = [
                *context.function.args.posonlyargs,
                *context.function.args.args,
                *context.function.args.kwonlyargs,
            ]
            if any(p.arg == call.func.id for p in parameters):
                return None
        target = self.checker.helper(context.source, context.function, call)
        if target is None:
            return None
        source, function = target
        params = [
            p
            for p in [*function.args.posonlyargs, *function.args.args]
            if p.arg not in {"self", "cls"}
        ]
        arguments = {
            p.arg: Reference(arg, context) for p, arg in zip(params, call.args)
        }
        arguments.update(
            {
                kw.arg: Reference(kw.value, context)
                for kw in call.keywords
                if kw.arg is not None
            }
        )
        return Context(source, function, arguments)

    def exclusions(self, context: Context, name: str, line: int) -> set[str]:
        excluded = set()
        parents = {
            child: parent
            for parent in ast.walk(context.function)
            for child in ast.iter_child_nodes(parent)
        }
        uses = [
            node
            for node in _nodes(context.function)
            if isinstance(node, ast.Name) and node.id == name and node.lineno == line
        ]
        candidates = []
        for use in uses:
            current = use
            while current in parents:
                parent = parents[current]
                for _, value in ast.iter_fields(parent):
                    if isinstance(value, list) and current in value:
                        candidates.extend(value[: value.index(current)])
                current = parent
        for node in candidates:
            if not isinstance(node, ast.If):
                continue
            test = node.test
            if not (
                isinstance(test, ast.Call)
                and isinstance(test.func, ast.Name)
                and test.func.id == "isinstance"
                and len(test.args) == 2
                and isinstance(test.args[0], ast.Name)
                and test.args[0].id == name
            ):
                continue
            if not node.body or not isinstance(node.body[-1], ast.Raise):
                continue
            kinds = (
                test.args[1].elts
                if isinstance(test.args[1], ast.Tuple)
                else [test.args[1]]
            )
            excluded.update(_qualified(context.source, kind) for kind in kinds)
        return excluded

    def resolve(self, reference: Reference, excluded=frozenset(), seen=frozenset()):
        node, context = reference.node, reference.context
        key = (id(node), id(context.function))
        if key in seen or len(seen) > 64:
            yield None
            return
        seen = seen | {key}
        src = context.source
        if isinstance(node, ast.Name):
            excluded = excluded | self.exclusions(context, node.id, node.lineno)
            assignments = []
            for stmt in _nodes(context.function):
                if getattr(stmt, "lineno", node.lineno) >= node.lineno:
                    continue
                if isinstance(stmt, ast.Assign) and any(
                    isinstance(t, ast.Name) and t.id == node.id for t in stmt.targets
                ):
                    assignments.append(stmt.value)
                elif (
                    isinstance(stmt, ast.AnnAssign)
                    and isinstance(stmt.target, ast.Name)
                    and stmt.target.id == node.id
                    and stmt.value is not None
                ):
                    assignments.append(stmt.value)
            if len(assignments) == 1:
                yield from self.resolve(
                    Reference(assignments[0], context), excluded, seen
                )
            elif not assignments and node.id in context.arguments:
                yield from self.resolve(context.arguments[node.id], excluded, seen)
            else:
                yield None
        elif isinstance(node, ast.IfExp):
            branches = (node.body, node.orelse)
            if isinstance(node.test, ast.Constant):
                branches = (node.body if node.test.value else node.orelse,)
            for branch in branches:
                yield from self.resolve(Reference(branch, context), excluded, seen)
        elif isinstance(node, ast.Call):
            if (
                isinstance(node.func, ast.Attribute)
                and node.func.attr == "to_failure_details"
            ):
                yield from self.resolve(
                    Reference(node.func.value, context), excluded, seen
                )
            elif self.checker.typed_error(src, node.func) or _sdk(
                src, node.func, "FailureDetails"
            ):
                if not self.checker.error_names(src, node.func).intersection(excluded):
                    yield src, node
            else:
                child = self.child(context, node)
                returns = list(_returns(child.function.body)) if child else []
                if not returns:
                    yield None
                for ret in returns:
                    if ret.value is not None:
                        yield from self.resolve(
                            Reference(ret.value, child), excluded, seen
                        )
        elif not isinstance(node, ast.Constant) or node.value is not None:
            yield None

    def visit(self, context: Context, ancestors=frozenset()) -> None:
        if id(context.function) in ancestors:
            return
        ancestors = ancestors | {id(context.function)}
        src = context.source
        for node in _nodes(context.function):
            if not isinstance(node, ast.Call):
                continue
            if _sdk(src, node.func, "PreflightCheck"):
                kwargs = _kwargs(node)
                if _literal(kwargs.get("passed")) is True:
                    if "error" in kwargs:
                        errors = list(self.resolve(Reference(kwargs["error"], context)))
                        if any(error is not None for error in errors):
                            self.checker.emit(
                                src,
                                node,
                                "P055",
                                "Passed preflight check can carry typed failure evidence; clear errors on the success path.",
                            )
                        elif errors:
                            self.checker.emit(
                                src,
                                node,
                                "P065",
                                "Success-path error expression is unresolved; verify passed checks do not carry failure evidence.",
                            )
                    continue
                if "error" not in kwargs:
                    continue
                unresolved = False
                for result in self.resolve(Reference(kwargs["error"], context)):
                    if result is None:
                        unresolved = True
                    else:
                        self.checker.failure(result[0], result[1], {})
                if unresolved:
                    self.checker.emit(
                        src,
                        node,
                        "P065",
                        "Failed-check error flow is unresolved; suggested_action and typing are not verified. Execute a real-handler scenario for this output.",
                    )
            child = self.child(context, node)
            if child is not None:
                self.visit(child, ancestors)


def scan(checker: _Checker) -> None:
    flow = ErrorFlow(checker)
    for source, function in find_preflight_check_sites(checker.reg):
        flow.visit(Context(source, function))
