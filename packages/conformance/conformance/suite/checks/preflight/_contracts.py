"""Bounded checks for typed preflight results and gate input contracts."""

from __future__ import annotations

import ast
from functools import lru_cache

from conformance.suite.checks._ast_common import make_finding
from conformance.suite.checks.prescriptions._decorator_provenance import (
    is_entrypoint_decorator,
)
from conformance.suite.schema.findings import Finding

from ._common import Registry, Source, find_preflight_check_sites

Function = ast.FunctionDef | ast.AsyncFunctionDef
_UNKNOWN = object()


@lru_cache(maxsize=256)
def _origins(tree: ast.Module) -> dict[str, str]:
    result: dict[str, str] = {}
    for imp in ast.walk(tree):
        if isinstance(imp, ast.ImportFrom):
            for alias in imp.names:
                result[alias.asname or alias.name] = (
                    f"{'.' * imp.level}{imp.module + '.' if imp.module else ''}{alias.name}"
                )
        elif isinstance(imp, ast.Import):
            for alias in imp.names:
                result[alias.asname or alias.name.split(".")[0]] = (
                    alias.name if alias.asname else alias.name.split(".")[0]
                )
    return result


def _qualified(src: Source, node: ast.AST | None) -> str:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        try:
            return _qualified(src, ast.parse(node.value, mode="eval").body)
        except SyntaxError:
            return ""
    if isinstance(node, ast.Attribute):
        return _qualified(src, node.value) + "." + node.attr
    if isinstance(node, ast.Name):
        name = _origins(src.tree).get(node.id, node.id)
        if name.startswith("."):
            level = len(name) - len(name.lstrip("."))
            package = src.rel.split("/")[:-1]
            prefix = package[: len(package) - level + 1]
            name = ".".join([*prefix, name.lstrip(".")])
        return name
    return ""


def _sdk(src: Source, node: ast.AST | None, name: str) -> bool:
    value = _qualified(src, node)
    return value.startswith("application_sdk.") and value.rsplit(".", 1)[-1] == name


def _literal(node: ast.AST | None) -> object:
    if isinstance(node, ast.Constant):
        return node.value
    return _UNKNOWN


def _kwargs(node: ast.Call) -> dict[str, ast.expr]:
    return {kw.arg: kw.value for kw in node.keywords if kw.arg is not None}


def _nodes(node: ast.AST):
    yield node
    for child in ast.iter_child_nodes(node):
        if not isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            yield from _nodes(child)


class _Checker:
    def __init__(self, reg: Registry):
        self.reg = reg
        self.findings: list[Finding] = []
        self.seen: set[tuple[str, int, str]] = set()

    def emit(self, src: Source, node: ast.AST, rule: str, message: str) -> None:
        key = (src.rel, node.lineno, rule)
        if key not in self.seen:
            self.seen.add(key)
            self.findings.append(
                make_finding(
                    filename=src.rel,
                    rule_id=rule,
                    node=node,
                    message=message,
                    directives=src.directives,
                )
            )

    def symbol(self, src: Source, node: ast.AST):
        name = _qualified(src, node)
        target = src
        if "." in name:
            module, name = name.rsplit(".", 1)
            target = next(
                (
                    s
                    for s in self.reg.sources
                    if s.rel.removesuffix(".py")
                    .replace("/", ".")
                    .removesuffix(".__init__")
                    == module
                ),
                None,
            )
        if target is None:
            return None
        definition = next(
            (
                n
                for n in target.tree.body
                if isinstance(n, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
                and n.name == name
            ),
            None,
        )
        return (target, definition) if definition is not None else None

    def error_names(self, src: Source, node: ast.AST, visited=frozenset()) -> set[str]:
        name = _qualified(src, node)
        if name.startswith("application_sdk.errors.") and name.rsplit(".", 1)[
            -1
        ].endswith("Error"):
            return {name, "Exception", "BaseException"}
        resolved = self.symbol(src, node)
        if resolved is None:
            return set()
        owner, cls = resolved
        if not isinstance(cls, ast.ClassDef) or id(cls) in visited:
            return set()
        bases = set().union(
            *(self.error_names(owner, base, visited | {id(cls)}) for base in cls.bases)
        )
        return bases | {name} if bases else set()

    def typed_error(self, src: Source, node: ast.AST) -> bool:
        return bool(self.error_names(src, node))

    def defaults(
        self, src: Source, node: ast.AST, visited=frozenset()
    ) -> dict[str, ast.AST]:
        name = _qualified(src, node)
        if name.startswith("application_sdk.errors."):
            if name.rsplit(".", 1)[-1] in {"ObjectStoreReadError", "DiskFullError"}:
                return {}
            return {"suggested_action": ast.Constant(value=None)}
        resolved = self.symbol(src, node)
        if resolved is None:
            return {}
        owner, cls = resolved
        if not isinstance(cls, ast.ClassDef) or id(cls) in visited:
            return {}
        if any(
            isinstance(stmt, ast.FunctionDef) and stmt.name == "__init__"
            for stmt in cls.body
        ):
            return {}
        result = {}
        for base in cls.bases:
            result.update(self.defaults(owner, base, visited | {id(cls)}))
        for stmt in cls.body:
            if (
                isinstance(stmt, ast.AnnAssign)
                and isinstance(stmt.target, ast.Name)
                and stmt.value is not None
            ):
                result[stmt.target.id] = stmt.value
            elif isinstance(stmt, ast.Assign):
                for target in stmt.targets:
                    if isinstance(target, ast.Name):
                        result[target.id] = stmt.value
        return result

    def failure(
        self, src: Source, error: ast.AST | None, bindings: dict[str, ast.AST]
    ) -> None:
        visited: set[str] = set()
        while (
            isinstance(error, ast.Name)
            and error.id in bindings
            and error.id not in visited
        ):
            visited.add(error.id)
            error = bindings[error.id]
        if not isinstance(error, ast.Call):
            return
        if (
            isinstance(error.func, ast.Attribute)
            and error.func.attr == "to_failure_details"
        ):
            error = error.func.value
        if not isinstance(error, ast.Call):
            return
        details = _sdk(src, error.func, "FailureDetails")
        typed = self.typed_error(src, error.func)
        if not (details or typed):
            return
        if any(kw.arg is None for kw in error.keywords):
            self.emit(
                src,
                error,
                "P065",
                "Expanded failure constructor arguments are unresolved; verify message and suggested_action in an executed failed-check scenario.",
            )
            return
        values = self.defaults(src, error.func)
        values.update(_kwargs(error))
        for field in ("message", "suggested_action"):
            value = _literal(values.get(field))
            if field == "suggested_action" and value is _UNKNOWN and field in values:
                self.emit(
                    src,
                    error,
                    "P065",
                    "Computed suggested_action is unresolved; verify the final failed-check action is nonblank and appropriate in an executed scenario.",
                )
            if (
                value is None
                or isinstance(value, str)
                and not value.strip()
                or details
                and field not in values
            ):
                self.emit(
                    src,
                    error,
                    "P053",
                    f"Preflight failure has missing or blank {field}; provide a meaningful explanation and audience-appropriate next action. Unresolved factory values require behavioral validation.",
                )

    def result(self, src: Source, call: ast.Call, bindings: dict[str, ast.AST]) -> None:
        kwargs = _kwargs(call)
        if _sdk(src, call.func, "PreflightCheck") and "status" in kwargs:
            self.emit(
                src,
                call,
                "P052",
                "PreflightCheck has no status field; supply passed as a boolean. An ignored status keyword leaves passed at its false default.",
            )
        if not _sdk(src, call.func, "PreflightOutput"):
            return
        status = kwargs.get("status")
        status_name = (
            status.attr if isinstance(status, ast.Attribute) else _literal(status)
        )
        if isinstance(status_name, str):
            status_name = status_name.upper()
        pending = [status]
        seen = set()
        while pending:
            candidate = pending.pop()
            if isinstance(candidate, ast.Name) and candidate.id not in seen:
                seen.add(candidate.id)
                pending.append(bindings.get(candidate.id))
            elif isinstance(candidate, ast.IfExp):
                pending.extend([candidate.body, candidate.orelse])
            elif (
                isinstance(candidate, ast.Constant)
                and isinstance(candidate.value, str)
                and candidate.value.upper() == "PARTIAL"
                or isinstance(candidate, ast.Attribute)
                and candidate.attr == "PARTIAL"
            ):
                self.emit(
                    src,
                    call,
                    "P066",
                    "PARTIAL preflight results are deprecated. Return NOT_READY for a blocking failure or READY when extraction can proceed; preserve truthful typed check evidence. Do not replace PARTIAL blindly.",
                )
                break
        checks = kwargs.get("checks")
        if not isinstance(checks, (ast.List, ast.Tuple)):
            if "checks" in kwargs:
                self.emit(
                    src,
                    call,
                    "P065",
                    "Computed preflight aggregation is unresolved: mandatory/advisory roles, short-circuiting, and retry/fallback semantics need executed handler scenarios. This is not a proven verdict violation.",
                )
            return
        if not checks.elts:
            if status_name == "NOT_READY":
                self.emit(
                    src,
                    call,
                    "P055",
                    "Handler NOT_READY result has no failed check evidence; include the evaluated blocking check.",
                )
            return
        passed = [
            _literal(_kwargs(c).get("passed"))
            if isinstance(c, ast.Call) and _sdk(src, c.func, "PreflightCheck")
            else _UNKNOWN
            for c in checks.elts
        ]
        if (
            status_name == "READY"
            and any(p is False for p in passed)
            or status_name == "NOT_READY"
            and all(p is True for p in passed)
        ):
            self.emit(
                src,
                call,
                "P055",
                "Preflight status contradicts its literal check results; aggregate mandatory and advisory checks explicitly and test both outcomes.",
            )

    def helper(self, src: Source, func: Function, call: ast.Call):
        resolved = self.symbol(src, call.func)
        if resolved is not None and isinstance(
            resolved[1], (ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            return resolved
        if (
            isinstance(call.func, ast.Attribute)
            and isinstance(call.func.value, ast.Name)
            and call.func.value.id in {"self", "cls"}
        ):
            for cls in ast.walk(src.tree):
                if isinstance(cls, ast.ClassDef) and func in cls.body:
                    target = next(
                        (
                            n
                            for n in cls.body
                            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                            and n.name == call.func.attr
                        ),
                        None,
                    )
                    if target is not None:
                        return src, target
        return None

    def body(
        self,
        src: Source,
        func: Function,
        visited: frozenset[int] = frozenset(),
        protected: frozenset[str] = frozenset(),
    ) -> None:
        if id(func) in visited:
            return
        visited = visited | {id(func)}
        bindings: dict[str, ast.AST] = {}
        assignments: dict[str, list[ast.AST]] = {}
        for node in _nodes(func):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        assignments.setdefault(target.id, []).append(node.value)
        bindings = {
            name: values[0] for name, values in assignments.items() if len(values) == 1
        }

        def walk(
            node: ast.AST,
            caught: frozenset[str],
            expected: bool = False,
            error_name: str | None = None,
        ) -> None:
            if isinstance(node, ast.Try):
                handled = set()
                for handler in node.handlers:
                    types = (
                        handler.type.elts
                        if isinstance(handler.type, ast.Tuple)
                        else [handler.type]
                    )
                    handled.update(
                        _qualified(src, t) if t is not None else "BaseException"
                        for t in types
                    )
                for stmt in node.body:
                    walk(stmt, caught | handled, expected)
                for handler in node.handlers:
                    types = (
                        handler.type.elts
                        if isinstance(handler.type, ast.Tuple)
                        else [handler.type]
                    )
                    typed = any(
                        t is not None and self.typed_error(src, t) for t in types
                    )
                    broad = any(
                        t is None
                        or _qualified(src, t) in {"Exception", "BaseException"}
                        for t in types
                    )
                    if broad:
                        typed = any(
                            isinstance(n, ast.Raise)
                            and isinstance(n.exc, ast.Call)
                            and self.typed_error(src, n.exc.func)
                            for stmt in node.body
                            for n in _nodes(stmt)
                        )
                    for stmt in handler.body:
                        walk(stmt, caught, typed, handler.name)
                for stmt in [*node.orelse, *node.finalbody]:
                    walk(stmt, caught, expected)
                return
            if isinstance(node, ast.Raise):
                expr = node.exc
                if (
                    isinstance(expr, ast.Call)
                    and self.typed_error(src, expr.func)
                    and not self.error_names(src, expr.func).intersection(caught)
                    or (
                        expr is None
                        or isinstance(expr, ast.Name)
                        and expr.id == error_name
                    )
                    and expected
                    and not caught.intersection({"Exception", "BaseException"})
                ):
                    self.emit(
                        src,
                        node,
                        "P054",
                        "Expected typed preflight failure escapes the handler. Return a typed PreflightOutput verdict; the strict gate does not preserve the legacy raised-error fail-open behavior.",
                    )
            if isinstance(node, ast.Call):
                self.result(src, node, bindings)
                helper = self.helper(src, func, node)
                if helper is not None:
                    self.body(helper[0], helper[1], visited, caught)
            for child in ast.iter_child_nodes(node):
                if not isinstance(
                    child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
                ):
                    walk(child, caught, expected, error_name)

        for stmt in func.body:
            walk(stmt, protected)

    def handler(self, src: Source, func: Function) -> None:
        args = [
            a
            for a in [*func.args.posonlyargs, *func.args.args, *func.args.kwonlyargs]
            if a.arg not in {"self", "cls"}
        ]
        if not any(_sdk(src, a.annotation, "PreflightInput") for a in args) or not _sdk(
            src, func.returns, "PreflightOutput"
        ):
            self.emit(
                src,
                func,
                "P052",
                "Preflight handler must declare SDK PreflightInput and PreflightOutput annotations, including aliases; optional HandlerContext is supported.",
            )
        for node in _nodes(func):
            if isinstance(node, ast.Return) and (
                isinstance(node.value, ast.Dict)
                or isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, bool)
            ):
                self.emit(
                    src,
                    node,
                    "P052",
                    "Preflight handler returns a legacy dictionary or boolean; return the SDK PreflightOutput contract.",
                )
        self.body(src, func)

    def inputs(self, src: Source) -> None:
        for func in ast.walk(src.tree):
            if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            decorators = [
                d for d in func.decorator_list if is_entrypoint_decorator(d, src.prov)
            ]
            for decorator in decorators:
                name: object = func.name
                if isinstance(decorator, ast.Call) and "name" in _kwargs(decorator):
                    name = _literal(_kwargs(decorator)["name"])
                if not isinstance(name, str):
                    continue
                for node in _nodes(func):
                    if (
                        not isinstance(node, ast.Call)
                        or not _sdk(src, node.func, "PreflightInput")
                        or any(kw.arg is None for kw in node.keywords)
                    ):
                        continue
                    kwargs = _kwargs(node)
                    entrypoint = _literal(kwargs.get("entrypoint"))
                    if (
                        "entrypoint" not in kwargs
                        or isinstance(entrypoint, str)
                        and entrypoint != name
                    ):
                        self.emit(
                            src,
                            node,
                            "P056",
                            "Workflow-constructed PreflightInput does not preserve its known entrypoint; pass the selected entrypoint and resolve credentials before the SDK gate. Interactive inputs may omit entrypoint.",
                        )


def scan(reg: Registry) -> list[Finding]:
    checker = _Checker(reg)
    for src, func in find_preflight_check_sites(reg):
        checker.handler(src, func)
    for src in reg.sources:
        checker.inputs(src)
    from ._error_flow import scan as scan_error_flow

    scan_error_flow(checker)
    return checker.findings
