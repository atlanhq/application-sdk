"""Preflight probe lifetime, failure exposure, and upgrade diagnostics."""

from __future__ import annotations

import ast
from pathlib import Path

from conformance.suite.checks._ast_common import make_finding
from conformance.suite.schema.findings import Finding

from ._common import Registry, Source, iter_function_nodes, reachable_preflight_sites
from ._contracts import _qualified

_BLOCKING = {
    "time.sleep",
    "requests.get",
    "requests.post",
    "requests.put",
    "requests.request",
    "requests.delete",
    "requests.head",
    "pyodbc.connect",
    "psycopg2.connect",
    "pymysql.connect",
    "teradatasql.connect",
    "snowflake.connector.connect",
    "socket.getaddrinfo",
    "socket.create_connection",
}
_DEADLINES = {"asyncio.wait_for", "asyncio.timeout", "asyncio.timeout_at"}
_REMOVED = {"_GATE_BROKEN_CATEGORIES", "_is_gate_broken"}
_ENV = "ATLAN_PREFLIGHT_GATE_MODE"


def _contains_budget(node: ast.AST) -> bool:
    return any(
        isinstance(n, ast.Attribute) and n.attr == "timeout_seconds"
        for n in ast.walk(node)
    )


def _ancestor_nodes(node: ast.AST, parents: dict[ast.AST, ast.AST]):
    while node in parents:
        node = parents[node]
        yield node


def _deadline(node: ast.AST, parents: dict[ast.AST, ast.AST], src: Source) -> bool:
    for ancestor in _ancestor_nodes(node, parents):
        if (
            isinstance(ancestor, ast.Call)
            and _qualified(src, ancestor.func) in _DEADLINES
        ):
            return True
        if isinstance(ancestor, ast.AsyncWith) and any(
            isinstance(i.context_expr, ast.Call)
            and _qualified(src, i.context_expr.func) in _DEADLINES
            for i in ancestor.items
        ):
            return True
    return False


def _exception_text(node: ast.AST, names: set[str], src: Source) -> bool:
    if isinstance(node, ast.Call) and _qualified(src, node.func) in {
        "application_sdk.errors.redact_secrets",
        "application_sdk.errors.base.redact_secrets",
        "application_sdk.errors.sanitize_cause_repr",
        "application_sdk.errors.base.sanitize_cause_repr",
    }:
        return False
    if isinstance(node, ast.Name):
        return node.id in names
    return any(
        _exception_text(child, names, src) for child in ast.iter_child_nodes(node)
    )


def scan(reg: Registry) -> list[Finding]:
    findings: list[Finding] = []
    seen: set[tuple[str, int, str]] = set()

    def emit(src: Source, node: ast.AST, rule: str, message: str) -> None:
        key = (src.rel, node.lineno, rule)
        if key not in seen:
            seen.add(key)
            findings.append(
                make_finding(
                    filename=src.rel,
                    rule_id=rule,
                    node=node,
                    message=message,
                    directives=src.directives,
                )
            )

    for src, func in reachable_preflight_sites(reg):
        parents = {
            child: parent
            for parent in ast.walk(func)
            for child in ast.iter_child_nodes(parent)
        }
        nodes = list(iter_function_nodes(func))
        for node in nodes:
            if not isinstance(node, ast.Call):
                continue
            qualified = _qualified(src, node.func)
            ancestors = list(_ancestor_nodes(node, parents))
            if qualified in _BLOCKING:
                emit(
                    src,
                    node,
                    "P057",
                    "Blocking source operation executes on the preflight event loop. Use a bounded async client or move the operation off the loop under the remaining deadline.",
                )
            is_executor = (
                qualified == "asyncio.to_thread"
                or isinstance(node.func, ast.Attribute)
                and node.func.attr == "run_in_executor"
            )
            if is_executor and not _deadline(node, parents, src):
                emit(
                    src,
                    node,
                    "P057",
                    "Executor probe has no recognized enclosing deadline. Bound every connection phase to the remaining preflight budget; cancellation does not stop the underlying thread.",
                )
            if isinstance(node.func, ast.Attribute) and node.func.attr in {
                "dispose",
                "close",
                "disconnect",
            }:
                awaited = isinstance(parents.get(node), ast.Await)
                if not awaited:
                    emit(
                        src,
                        node,
                        "P059",
                        "Synchronous cleanup on the preflight path may block the event loop. Verify resource ownership and use bounded off-loop cleanup for synchronous drivers.",
                    )
            for kw in node.keywords:
                value = kw.value
                enlarged = (
                    isinstance(value, ast.BinOp)
                    and isinstance(value.op, ast.Add)
                    and _contains_budget(value)
                    and any(
                        isinstance(n, ast.Constant)
                        and isinstance(n.value, (int, float))
                        and n.value > 0
                        for n in (value.left, value.right)
                    )
                )
                floored = (
                    isinstance(value, ast.Call)
                    and isinstance(value.func, ast.Name)
                    and value.func.id == "max"
                    and _contains_budget(value)
                    and any(
                        isinstance(a, ast.Constant)
                        and isinstance(a.value, (int, float))
                        and a.value > 0
                        for a in value.args
                    )
                )
                if kw.arg in {"timeout", "timeout_seconds", "request_timeout"} and (
                    enlarged or floored
                ):
                    emit(
                        src,
                        node,
                        "P058",
                        "Probe timeout can exceed input.timeout_seconds. Use remaining time with headroom instead of extending the deadline or imposing a positive floor.",
                    )
            if qualified == "asyncio.wait_for" and node.args:
                outer = next(
                    (kw.value for kw in node.keywords if kw.arg == "timeout"), None
                )
                if outer is not None:
                    for inner in ast.walk(node.args[0]):
                        if isinstance(inner, ast.Call) and any(
                            kw.arg in {"timeout", "request_timeout"}
                            and ast.dump(kw.value) == ast.dump(outer)
                            for kw in inner.keywords
                        ):
                            emit(
                                src,
                                node,
                                "P058",
                                "Driver request timeout equals the whole probe deadline. Give completion and cleanup headroom; equal nested bounds can turn completion into a timing race.",
                            )
            caught = {
                a.name for a in ancestors if isinstance(a, ast.ExceptHandler) and a.name
            }
            wire = qualified.startswith("application_sdk.") and qualified.rsplit(
                ".", 1
            )[-1] in {"PreflightCheck", "PreflightOutput", "FailureDetails"}
            wire = wire or qualified.startswith("application_sdk.errors.")
            if wire:
                for kw in node.keywords:
                    if kw.arg in {
                        "message",
                        "suggested_action",
                        "evidence",
                    } and _exception_text(kw.value, caught, src):
                        emit(
                            src,
                            node,
                            "P060",
                            "Raw caught exception reaches a preflight wire field. Use a safe message/action and the SDK's sanitized cause; verify with synthetic-secret tests.",
                        )
            if isinstance(node.func, ast.Attribute) and node.func.attr in {
                "exception",
                "error",
                "warning",
            }:
                handler = next(
                    (a for a in ancestors if isinstance(a, ast.ExceptHandler)), None
                )
                protected = parents.get(handler) if handler is not None else None
                expressions = [node]
                if isinstance(protected, (ast.Try, ast.TryStar)):
                    expressions.extend(protected.body)
                credential_values = any(
                    isinstance(n, ast.Name)
                    and isinstance(n.ctx, ast.Load)
                    and n.id.lower() in {"credentials", "creds", "password", "token"}
                    for expression in expressions
                    for n in (expression, *iter_function_nodes(expression))
                    if not isinstance(
                        expression,
                        (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef),
                    )
                )
                traceback = node.func.attr == "exception" or any(
                    kw.arg == "exc_info"
                    and isinstance(kw.value, ast.Constant)
                    and kw.value.value is True
                    for kw in node.keywords
                )
                if caught and credential_values and traceback:
                    emit(
                        src,
                        node,
                        "P060",
                        "Traceback logging accompanies credential reads in the protected operation or log expression and may expose values when diagnostic rendering is enabled. Disable diagnostic-local rendering and verify that synthetic secrets do not reach the log sink.",
                    )
    for src in reg.sources:
        for node in ast.walk(src.tree):
            removed = (
                isinstance(node, ast.ImportFrom)
                and (node.module or "").endswith("preflight_gate")
                and any(a.name in _REMOVED for a in node.names)
            )
            if isinstance(node, ast.Attribute):
                qname = _qualified(src, node)
                removed = (
                    qname.startswith(
                        "application_sdk.execution._temporal.preflight_gate."
                    )
                    and node.attr in _REMOVED
                )
            if isinstance(node, ast.Call) and _qualified(src, node.func) in {
                "os.getenv",
                "os.environ.get",
            }:
                removed = bool(
                    node.args
                    and isinstance(node.args[0], ast.Constant)
                    and node.args[0].value == _ENV
                )
            if (
                isinstance(node, ast.Subscript)
                and _qualified(src, node.value) == "os.environ"
            ):
                removed = (
                    isinstance(node.slice, ast.Constant) and node.slice.value == _ENV
                )
            if removed:
                emit(
                    src,
                    node,
                    "P061",
                    "Legacy gate configuration or private helper is removed by SDK PR #3685. Migration advisory until the release floor is known: declare App.preflight_gate_mode and test public verdict behavior.",
                )
    return findings


def scan_removed_config(root: Path) -> list[Finding]:
    """Inspect deployment declarations without reading credential files."""
    findings: list[Finding] = []
    for directory in (
        root / ".github" / "workflows",
        root / "deploy",
        root / "deployment",
        root / "helm",
        root / "k8s",
    ):
        if not directory.is_dir():
            continue
        for path in sorted(directory.rglob("*")):
            if (
                not path.is_file()
                or path.suffix not in {".yaml", ".yml"}
                or path.is_symlink()
            ):
                continue
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except (OSError, UnicodeDecodeError):
                continue
            for line_number, line in enumerate(lines, 1):
                stripped = line.strip()
                if stripped.startswith("#"):
                    continue
                if stripped.startswith(f"{_ENV}:") or stripped in {
                    f"- name: {_ENV}",
                    f"name: {_ENV}",
                }:
                    findings.append(
                        Finding(
                            rule_id="P061",
                            file=path.relative_to(root).as_posix(),
                            line=line_number,
                            column=1,
                            message="Deployment sets the gate mode override removed by SDK PR #3685. Migrate to App.preflight_gate_mode before adopting that release; release-floor applicability is not yet established.",
                        )
                    )
    return findings
