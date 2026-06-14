"""E-series error-handling checks — AST-based.

Scans Python source files for error-handling anti-patterns defined in the
E001–E018 rule catalog.  Every check is purely deterministic: the same
source text always produces the same set of findings.

Inline suppression
------------------
Add a ``# conformance: ignore[EXXX] <reason>`` comment on the offending line
or on the line immediately above it to acknowledge a finding with justification::

    except ImportError:  # conformance: ignore[E008] third-party optional dep
        pass

    # conformance: ignore[E002] StopIteration expected in manual iterator
    except StopIteration:
        pass
"""

from __future__ import annotations

import argparse
import ast
import io
import json
import re
import sys
import tokenize
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from suite.schema.findings import Finding, findings_to_report

SERIES = "E"

# P012 — builtin exception types that should be replaced by typed AppError leaves
BUILTIN_RAISES: frozenset[str] = frozenset(
    {
        "ValueError",
        "RuntimeError",
        "Exception",
        "TypeError",
        "NotImplementedError",
        "OSError",
        "KeyError",
        "LookupError",
    }
)

# P013 — legacy AtlanError subclass names (all from application_sdk.common.error_codes)
LEGACY_ATLAN_ERRORS: frozenset[str] = frozenset(
    {
        "ClientError",
        "ApiError",
        "OrchestratorError",
        "WorkflowError",
        "IOError",
        "CommonError",
        "DocGenError",
        "ActivityError",
        "AtlanError",
    }
)
_ATLAN_LEGACY_MODULE = "application_sdk.common.error_codes"

# P018 — bare parent AppError leaf classes (application_sdk/errors/leaves.py)
LEAF_CLASSES: frozenset[str] = frozenset(
    {
        "CancelledError",
        "AppTimeoutError",
        "RateLimitedError",
        "AuthError",
        "AppPermissionDeniedError",
        "NotFoundError",
        "AlreadyExistsError",
        "InvalidInputError",
        "PreconditionError",
        "DependencyUnavailableError",
        "ResourceExhaustedError",
        "DataIntegrityError",
        "InternalError",
        "UnimplementedError",
    }
)

# P012 carve-outs: builtin raises acceptable in these method names (stdlib interop)
INTEROP_METHODS: frozenset[str] = frozenset({"__post_init__", "__init__"})
# P012 carve-outs: acceptable in Pydantic validator-decorated functions
INTEROP_DECORATORS: frozenset[str] = frozenset({"field_validator", "validator"})
# P012 escalation: note CRITICAL when inside Temporal activity body
ACTIVITY_DECORATORS: frozenset[str] = frozenset({"task", "defn"})

# Directories excluded from discovery
EXCLUDE_DIRS: frozenset[str] = frozenset(
    {
        ".git",
        ".venv",
        "venv",
        "__pycache__",
        "build",
        "dist",
        "node_modules",
        "tests",
        "test",
        "conformance",
        "docs",
        ".tox",
        "site-packages",
        ".mypy_cache",
        ".ruff_cache",
        ".pytest_cache",
        "htmlcov",
    }
)

_SECRET_SUFFIXES: tuple[str, ...] = ("_secret", "_password", "_token")
_BROAD_EXCEPT_TYPES: frozenset[str] = frozenset({"Exception", "BaseException"})
_OPTIONAL_IMPORT_TYPES: frozenset[str] = frozenset(
    {"ImportError", "ModuleNotFoundError"}
)
_LOG_METHODS: frozenset[str] = frozenset(
    {"debug", "info", "warning", "warn", "error", "critical", "exception"}
)

# Directive regex: "# conformance: ignore[E001,E002] some reason"
_SUPPRESS_RE = re.compile(
    r"^#\s*conformance\s*:\s*ignore\s*(?:\[([^\]]*)\])?\s*(.*)",
    re.IGNORECASE,
)


# A noqa comment is accepted ONLY when it names at least one mapped code AND
# supplies non-empty justification text after a visual separator (—, –, or -).
# Bare "# noqa" and "# noqa: CODE" with no justification are rejected.
_NOQA_RE = re.compile(
    r"^#\s*noqa\s*:\s*([A-Z][A-Z0-9]*(?:\s*,\s*[A-Z][A-Z0-9]*)*)\s*[—–\-]+\s*(.+)$",
    re.IGNORECASE,
)
_NOQA_TO_RULES: dict[str, frozenset[str]] = {
    "S110": frozenset({"E001", "E002"}),  # try-except-pass (bare or typed)
    "BLE001": frozenset({"E004"}),  # blind/broad exception catch
    "S112": frozenset({"E014"}),  # try-except-continue (loop swallow)
}


# ---------------------------------------------------------------------------
# Directive parsing
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _IgnoreDirective:
    """Parsed ``# conformance: ignore[...]`` directive."""

    rule_ids: frozenset[str] | None  # None = suppress every rule on this line
    justification: str


def _parse_directives(source: str) -> dict[int, _IgnoreDirective]:
    """Return ``{lineno: directive}`` for all suppression comments.

    Recognises two forms:

    * ``# conformance: ignore[E001,E002] reason`` — explicit conformance directive
    * ``# noqa: S110 — reason`` — noqa shorthand when a mapped code + justification
      text are both present (bare ``# noqa`` and ``# noqa: CODE`` without text are
      rejected; unknown codes produce no suppression)
    """
    directives: dict[int, _IgnoreDirective] = {}
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(source).readline))
    except tokenize.TokenError:
        return directives
    for tok_type, tok_string, (srow, _), *_ in tokens:
        if tok_type != tokenize.COMMENT:
            continue

        # ── conformance: ignore[...] ──────────────────────────────────────────
        m = _SUPPRESS_RE.search(tok_string)
        if m:
            raw_ids, justification = m.group(1), (m.group(2) or "").strip()
            rule_ids: frozenset[str] | None
            if raw_ids:
                rule_ids = frozenset(
                    r.strip().upper() for r in raw_ids.split(",") if r.strip()
                )
            else:
                rule_ids = None
            directives[srow] = _IgnoreDirective(
                rule_ids=rule_ids, justification=justification
            )
            continue

        # ── # noqa: CODE — justification ─────────────────────────────────────
        m = _NOQA_RE.search(tok_string)
        if not m:
            continue
        justification = m.group(2).strip()
        mapped: set[str] = set()
        for code in (c.strip().upper() for c in m.group(1).split(",")):
            if code in _NOQA_TO_RULES:
                mapped.update(_NOQA_TO_RULES[code])
        if not mapped:
            continue  # all codes unknown — no suppression
        directives[srow] = _IgnoreDirective(
            rule_ids=frozenset(mapped), justification=justification
        )
    return directives


def parse_ignore_directive(comment: str) -> _IgnoreDirective | None:
    """Parse a raw comment string. Returns None if it is not a conformance directive."""
    m = _SUPPRESS_RE.search(comment)
    if not m:
        return None
    raw_ids, justification = m.group(1), (m.group(2) or "").strip()
    rule_ids: frozenset[str] | None
    if raw_ids:
        rule_ids = frozenset(r.strip().upper() for r in raw_ids.split(",") if r.strip())
    else:
        rule_ids = None
    return _IgnoreDirective(rule_ids=rule_ids, justification=justification)


# ---------------------------------------------------------------------------
# Import collection (for P013 IOError disambiguation)
# ---------------------------------------------------------------------------


def _collect_imports(tree: ast.Module) -> dict[str, str]:
    """Return ``{imported_name: source_module}`` for top-level imports."""
    imports: dict[str, str] = {}
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                name = alias.asname if alias.asname else alias.name.split(".")[0]
                imports[name] = alias.name
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            for alias in node.names:
                name = alias.asname if alias.asname else alias.name
                imports[name] = module
    return imports


def _collect_legacy_aliases(tree: ast.Module) -> frozenset[str]:
    """Return names bound to LEGACY_ATLAN_ERRORS via aliased imports from the legacy module."""
    aliases: set[str] = set()
    for node in ast.iter_child_nodes(tree):
        if not isinstance(node, ast.ImportFrom):
            continue
        if (node.module or "") != _ATLAN_LEGACY_MODULE:
            continue
        for alias in node.names:
            if alias.name in LEGACY_ATLAN_ERRORS and alias.asname:
                aliases.add(alias.asname)
    return frozenset(aliases)


# ---------------------------------------------------------------------------
# AST helpers
# ---------------------------------------------------------------------------


def _get_name(node: ast.expr | ast.AST | None) -> str | None:
    """Extract a simple name string from a Name or Attribute node."""
    if node is None:
        return None
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _raise_exc_name(exc: ast.expr) -> str | None:
    """Get the exception class name from a raise-expression.

    Handles both ``raise ValueError`` (Name) and ``raise ValueError(...)``
    (Call wrapping a Name or Attribute).
    """
    if isinstance(exc, ast.Call):
        return _get_name(exc.func)
    return _get_name(exc)


def _is_log_call_stmt(stmt: ast.stmt) -> bool:
    """True if *stmt* is a bare ``logger.<method>(...)`` expression."""
    if not isinstance(stmt, ast.Expr):
        return False
    call = stmt.value
    if isinstance(call, ast.Await):
        call = call.value
    if not isinstance(call, ast.Call):
        return False
    func = call.func
    return isinstance(func, ast.Attribute) and func.attr in _LOG_METHODS


def _any_logging_in(stmts: list[ast.stmt]) -> bool:
    """True if any logging call appears anywhere within *stmts*."""
    for stmt in stmts:
        if _is_log_call_stmt(stmt):
            return True
        for node in _iter_shallow(stmt):
            if isinstance(node, ast.stmt) and _is_log_call_stmt(node):
                return True
    return False


def _has_exc_info(call: ast.Call) -> bool:
    """True if *call* has ``exc_info=True`` among its keywords."""
    for kw in call.keywords:
        if kw.arg == "exc_info":
            val = kw.value
            if isinstance(val, ast.Constant) and val.value is True:
                return True
    return False


def _body_is_only_pass(stmts: list[ast.stmt]) -> bool:
    """True if the body is exclusively Pass (ignoring docstring constants)."""
    real = [
        s
        for s in stmts
        if not (
            isinstance(s, ast.Expr)
            and isinstance(s.value, ast.Constant)
            and isinstance(s.value.value, str)
        )
    ]
    return len(real) == 1 and isinstance(real[0], ast.Pass)


def _body_is_only_loop_control_no_logging(stmts: list[ast.stmt]) -> bool:
    """True if body only has continue/break/pass with no logging."""
    if _any_logging_in(stmts):
        return False
    real = [
        s
        for s in stmts
        if not (
            isinstance(s, ast.Expr)
            and isinstance(s.value, ast.Constant)
            and isinstance(s.value.value, str)
        )
    ]
    return len(real) > 0 and all(
        isinstance(s, (ast.Continue, ast.Break, ast.Pass)) for s in real
    )


def _is_gather_call(call: ast.Call) -> bool:
    """True if *call* is ``asyncio.gather(...)`` or bare ``gather(...)``."""
    func = call.func
    if isinstance(func, ast.Attribute):
        return (
            func.attr == "gather"
            and isinstance(func.value, ast.Name)
            and func.value.id == "asyncio"
        )
    if isinstance(func, ast.Name):
        return func.id == "gather"
    return False


def is_broad_suppress(node: ast.Call) -> bool:
    """True if *node* is ``contextlib.suppress(Exception|BaseException)``."""
    name = _get_name(node.func)
    if name != "suppress":
        return False
    for arg in node.args:
        if _get_name(arg) in _BROAD_EXCEPT_TYPES:
            return True
    return False


def is_builtin_raise(raise_node: ast.Raise) -> bool:
    """True if this raise targets a name in BUILTIN_RAISES."""
    if raise_node.exc is None:
        return False
    return _raise_exc_name(raise_node.exc) in BUILTIN_RAISES


def _get_decorator_names(
    func: ast.FunctionDef | ast.AsyncFunctionDef,
) -> frozenset[str]:
    names: set[str] = set()
    for dec in func.decorator_list:
        # @decorator → Name; @decorator("arg") → Call whose func is Name/Attribute
        if isinstance(dec, ast.Call):
            n = _get_name(dec.func)
        else:
            n = _get_name(dec)
        if n:
            names.add(n)
    return frozenset(names)


def _inherits_logging_filter(cls: ast.ClassDef) -> bool:
    for base in cls.bases:
        n = _get_name(base)
        if n == "Filter":
            return True
    return False


def _find_filter_method(
    cls: ast.ClassDef,
) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    for item in cls.body:
        if (
            isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
            and item.name == "filter"
        ):
            return item
    return None


def _filter_body_wrapped(method: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """True if the filter() body is a single Try node (fully wrapped)."""
    real = [
        s
        for s in method.body
        if not (
            isinstance(s, ast.Expr)
            and isinstance(s.value, ast.Constant)
            and isinstance(s.value.value, str)
        )
    ]
    return len(real) == 1 and isinstance(real[0], ast.Try)


def _message_kw_has_exc_text(kw_value: ast.expr, exc_binding: str | None) -> bool:
    """True if a ``message=`` value embeds caught-exception text."""
    if exc_binding is None:
        return False
    # f-string containing the exception binding name
    if isinstance(kw_value, ast.JoinedStr):
        for part in ast.walk(kw_value):
            if isinstance(part, ast.FormattedValue):
                inner = part.value
                if isinstance(inner, ast.Name) and inner.id == exc_binding:
                    return True
                if (
                    isinstance(inner, ast.Call)
                    and _get_name(inner.func) in ("str", "repr")
                    and inner.args
                    and isinstance(inner.args[0], ast.Name)
                    and inner.args[0].id == exc_binding
                ):
                    return True
        return False
    # str(exc) / repr(exc) directly
    if isinstance(kw_value, ast.Call) and (
        _get_name(kw_value.func) in ("str", "repr")
        and kw_value.args
        and (
            isinstance(kw_value.args[0], ast.Name)
            and kw_value.args[0].id == exc_binding
        )
    ):
        return True
    # BinOp concat referencing the binding
    if isinstance(kw_value, ast.BinOp) and isinstance(kw_value.op, ast.Add):
        for node in ast.walk(kw_value):
            if isinstance(node, ast.Name) and node.id == exc_binding:
                return True
    return False


def _iter_shallow(root: ast.AST) -> Iterator[ast.AST]:
    """Yield descendants of *root* without crossing nested function/class defs."""
    queue = list(ast.iter_child_nodes(root))
    while queue:
        node = queue.pop()
        yield node
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            queue.extend(ast.iter_child_nodes(node))


# ---------------------------------------------------------------------------
# Checker visitor
# ---------------------------------------------------------------------------


class Checker(ast.NodeVisitor):
    """Walk a module AST and emit E-series findings."""

    def __init__(
        self,
        filename: str,
        directives: dict[int, _IgnoreDirective],
        atlan_ioerror_imported: bool,
        legacy_aliases: frozenset[str] = frozenset(),
    ) -> None:
        self._filename = filename
        self._directives = directives
        self._atlan_ioerror_imported = atlan_ioerror_imported
        self._legacy_aliases = legacy_aliases
        self._findings: list[Finding] = []
        # Context stacks — managed by visit_* methods
        self._function_stack: list[ast.FunctionDef | ast.AsyncFunctionDef] = []
        self._except_stack: list[ast.ExceptHandler] = []
        self._loop_stack: list[ast.For | ast.AsyncFor | ast.While] = []
        self._class_stack: list[ast.ClassDef] = []

    # ── Finding creation ──────────────────────────────────────────────────────

    def _add(self, rule_id: str, node: ast.AST, message: str) -> None:
        line: int = getattr(node, "lineno", 1)
        col: int = getattr(node, "col_offset", 0) + 1
        suppressed = False
        justification: str | None = None
        for check_line in (line, line - 1):
            if check_line in self._directives:
                d = self._directives[check_line]
                if d.rule_ids is None or rule_id in d.rule_ids:
                    suppressed = True
                    justification = d.justification
                    break
        self._findings.append(
            Finding(
                rule_id=rule_id,
                file=self._filename,
                line=line,
                column=col,
                message=message,
                suppressed=suppressed,
                suppression_justification=justification,
            )
        )

    # ── Context management ────────────────────────────────────────────────────

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:  # type: ignore[override]
        # Reset loop/except context: handlers in a nested function are not
        # "inside" the outer function's loop or except block.
        saved_loops = self._loop_stack
        saved_excepts = self._except_stack
        self._loop_stack = []
        self._except_stack = []
        self._function_stack.append(node)
        self._check_p010_in_function(node)
        self.generic_visit(node)
        self._function_stack.pop()
        self._loop_stack = saved_loops
        self._except_stack = saved_excepts

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:  # type: ignore[override]
        saved_loops = self._loop_stack
        saved_excepts = self._except_stack
        self._loop_stack = []
        self._except_stack = []
        self._function_stack.append(node)  # type: ignore[arg-type]
        self._check_p010_in_function(node)  # type: ignore[arg-type]
        self.generic_visit(node)
        self._function_stack.pop()
        self._loop_stack = saved_loops
        self._except_stack = saved_excepts

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._class_stack.append(node)
        self._check_p011(node)
        self.generic_visit(node)
        self._class_stack.pop()

    def visit_For(self, node: ast.For) -> None:
        self._loop_stack.append(node)
        self.generic_visit(node)
        self._loop_stack.pop()

    def visit_AsyncFor(self, node: ast.AsyncFor) -> None:
        self._loop_stack.append(node)  # type: ignore[arg-type]
        self.generic_visit(node)
        self._loop_stack.pop()

    def visit_While(self, node: ast.While) -> None:
        self._loop_stack.append(node)  # type: ignore[arg-type]
        self.generic_visit(node)
        self._loop_stack.pop()

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        self._except_stack.append(node)
        self._check_p001_p002_p006(node)
        self._check_p004(node)
        self._check_p005(node)
        self._check_p007(node)
        self._check_p008(node)
        self._check_p009(node)
        self._check_p014(node)
        self.generic_visit(node)
        self._except_stack.pop()

    def visit_Raise(self, node: ast.Raise) -> None:
        self._check_p012(node)
        self._check_p013(node)
        self._check_p015(node)
        self._check_p016(node)
        self._check_p017_raise(node)
        self._check_p018(node)
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        self._check_p003(node)
        self._check_p017_call(node)
        self.generic_visit(node)

    # ── P001 / P002 / P006 ───────────────────────────────────────────────────

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
            exc_type = _get_name(node.type) or "Exception"
            self._add(
                "E002",
                node,
                f"'except {exc_type}: pass' silently discards the exception with no trace. "
                "Acceptable only for genuinely trivial best-effort operations — "
                "add a comment and log at DEBUG or use the suppression directive.",
            )

    # ── P003 ─────────────────────────────────────────────────────────────────

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

    # ── P004 ─────────────────────────────────────────────────────────────────

    def _check_p004(self, node: ast.ExceptHandler) -> None:
        if node.type is None:
            return  # bare except handled by P006
        if isinstance(node.type, ast.Tuple):
            broad = {_get_name(e) for e in node.type.elts} & _BROAD_EXCEPT_TYPES
        else:
            name = _get_name(node.type)
            broad = {name} & _BROAD_EXCEPT_TYPES if name else set()
        if not broad:
            return
        exc_type = next(iter(broad))
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
            if func.attr in ("warning", "error", "critical") and _has_exc_info(call):
                return
        self._add(
            "E004",
            node,
            f"'except {exc_type}' catches everything. Acceptable only at top-level handlers "
            f"(worker loops, HTTP handlers) when logged with exc_info=True. "
            f"Narrow the exception type or add exc_info=True logging.",
        )

    # ── P005 ─────────────────────────────────────────────────────────────────

    def _check_p005(self, node: ast.ExceptHandler) -> None:
        for n in _iter_shallow(node):
            if n is node:
                continue
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
            if func.attr in ("warning", "error", "critical") and not _has_exc_info(
                call
            ):
                self._add(
                    "E005",
                    n,
                    f"logger.{func.attr}() inside except block is missing exc_info=True — "
                    f"the stack trace is silently discarded. Add exc_info=True.",
                )

    # ── P007 ─────────────────────────────────────────────────────────────────

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

    # ── P008 ─────────────────────────────────────────────────────────────────

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

    # ── P009 ─────────────────────────────────────────────────────────────────

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

    # ── P010 ─────────────────────────────────────────────────────────────────

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

        for node in bare_gathers:
            self._add(
                "E010",
                node,
                "asyncio.gather(return_exceptions=True) result is discarded — exception "
                "instances in the result list are silently ignored. "
                "Inspect results: 'for r in results: if isinstance(r, Exception): ...'",
            )

        for var_name, assign_node in gather_vars.items():
            inspected = False
            for node in _iter_shallow(func_node):
                # isinstance(var, ...) direct check
                if isinstance(node, ast.Call):
                    func = node.func
                    if isinstance(func, ast.Name) and func.id == "isinstance":
                        if node.args and isinstance(node.args[0], ast.Name):
                            if node.args[0].id == var_name:
                                inspected = True
                                break
                # for r in var: ... — iteration counts as inspection
                if isinstance(node, ast.For):
                    if isinstance(node.iter, ast.Name) and node.iter.id == var_name:
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

    # ── P011 ─────────────────────────────────────────────────────────────────

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

    # ── P012 ─────────────────────────────────────────────────────────────────

    def _in_interop_context(self) -> bool:
        if not self._function_stack:
            return False
        func = self._function_stack[-1]
        if isinstance(func, ast.FunctionDef) and func.name in INTEROP_METHODS:
            return True
        return bool(_get_decorator_names(func) & INTEROP_DECORATORS)

    def _in_activity_context(self) -> bool:
        for func in self._function_stack:
            if _get_decorator_names(func) & ACTIVITY_DECORATORS:
                return True
        return False

    def _check_p012(self, node: ast.Raise) -> None:
        if node.exc is None:
            return
        exc_name = _raise_exc_name(node.exc)
        if exc_name not in BUILTIN_RAISES:
            return
        if self._in_interop_context():
            return
        activity_note = (
            " (NOTE: inside activity/task — AE receives an opaque string, no typed envelope)"
            if self._in_activity_context()
            else ""
        )
        self._add(
            "E012",
            node,
            f"raise {exc_name} where a typed AppError leaf should be used{activity_note}. "
            f"Replace with a domain-specific subclass from application_sdk.errors.",
        )

    # ── P013 ─────────────────────────────────────────────────────────────────

    def _check_p013(self, node: ast.Raise) -> None:
        if node.exc is None:
            return
        exc_name = _raise_exc_name(node.exc)
        # Also flag names aliased from the legacy module (e.g. `import IOError as IOE`)
        if exc_name not in LEGACY_ATLAN_ERRORS and exc_name not in self._legacy_aliases:
            return
        # IOError is also the Python builtin alias for OSError — only flag the literal
        # name when it was imported from application_sdk.common.error_codes; aliased
        # imports (tracked in _legacy_aliases) are always from the legacy module.
        if exc_name == "IOError" and not self._atlan_ioerror_imported:
            return
        self._add(
            "E013",
            node,
            f"raise {exc_name} uses the deprecated AtlanError stack "
            f"(emits DeprecationWarning; produces no typed wire envelope; "
            f"scheduled for removal in v4.0). "
            f"Replace with the appropriate leaf from application_sdk.errors.",
        )

    # ── P014 ─────────────────────────────────────────────────────────────────

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

    # ── P015 ─────────────────────────────────────────────────────────────────

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

    # ── P016 ─────────────────────────────────────────────────────────────────

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
        exc_name = _get_name(node.exc) or "NewError"
        self._add(
            "E016",
            node,
            f"raise {exc_name} inside 'except ... as {exc_binding}:' is missing "
            f"'from {exc_binding}'. The original exception is lost in AE dashboards. "
            f"Use 'raise {exc_name}(...) from {exc_binding}'.",
        )

    # ── P017 ─────────────────────────────────────────────────────────────────

    def _check_p017_raise(self, node: ast.Raise) -> None:
        if node.exc is None or not isinstance(node.exc, ast.Call):
            return
        for kw in node.exc.keywords:
            if kw.arg and any(kw.arg.endswith(s) for s in _SECRET_SUFFIXES):
                self._add(
                    "E017",
                    node,
                    f"Evidence key '{kw.arg}' ends in a secret suffix — "
                    f"the wire layer rejects this at runtime (ValueError). "
                    f"Rename to a safe key (e.g. credential_name, token_type).",
                )
                return

    def _check_p017_call(self, node: ast.Call) -> None:
        """Catch construct-then-raise: ``err = SomeError(api_secret=...); raise err``."""
        func_name = _get_name(node.func)
        if func_name not in LEAF_CLASSES:
            return
        for kw in node.keywords:
            if kw.arg and any(kw.arg.endswith(s) for s in _SECRET_SUFFIXES):
                self._add(
                    "E017",
                    node,
                    f"Evidence key '{kw.arg}' ends in a secret suffix — "
                    f"the wire layer rejects this at runtime (ValueError). "
                    f"Rename to a safe key (e.g. credential_name, token_type).",
                )
                return

    # ── P018 ─────────────────────────────────────────────────────────────────

    def _check_p018(self, node: ast.Raise) -> None:
        if node.exc is None:
            return
        exc_node = node.exc
        exc_name = _raise_exc_name(exc_node)
        kws: list[ast.keyword] = []
        if isinstance(exc_node, ast.Call):
            kws = exc_node.keywords

        if exc_name not in LEAF_CLASSES:
            return

        # Sanctioned bare-parent form: InternalError(classification_pending=True)
        if exc_name == "InternalError":
            for kw in kws:
                if kw.arg == "classification_pending":
                    if isinstance(kw.value, ast.Constant) and kw.value.value is True:
                        return

        self._add(
            "E018",
            node,
            f"raise {exc_name} uses a bare parent leaf without a domain-specific subclass "
            f"that overrides 'code' — collapses all failures of this category into one "
            f"dashboard bucket. Define a subclass with a specific code constant. "
            f"Only acceptable bare-parent form: InternalError(classification_pending=True).",
        )


# ---------------------------------------------------------------------------
# Public check API
# ---------------------------------------------------------------------------


def scan_text(text: str, file: str) -> list[Finding]:
    """Scan Python source *text* and return all E-series findings."""
    try:
        tree = ast.parse(text, filename=file)
    except SyntaxError:
        return []

    imports = _collect_imports(tree)
    atlan_ioerror = imports.get("IOError") == _ATLAN_LEGACY_MODULE
    legacy_aliases = _collect_legacy_aliases(tree)

    directives = _parse_directives(text)
    checker = Checker(
        filename=file,
        directives=directives,
        atlan_ioerror_imported=atlan_ioerror,
        legacy_aliases=legacy_aliases,
    )
    checker.visit(tree)
    return checker._findings


def scan_path(path: Path, root: Path) -> list[Finding]:
    """Scan a single Python file, producing repo-root-relative URIs."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    try:
        rel = path.relative_to(root)
    except ValueError:
        rel = path
    return scan_text(text, str(rel))


def discover(root: Path) -> list[Path]:
    """Discover Python source files under *root*, excluding test and infra dirs.

    Two exclusion layers apply universally (not configurable per-repo):

    * **Named infra dirs** — any path component in ``EXCLUDE_DIRS`` (e.g. ``tests``,
      ``build``, ``.venv``).
    * **Dot-prefixed dirs** — any path component that starts with ``"."`` (e.g.
      ``.github``, ``.claude``, ``.mothership``).  These are CI/dev/skill
      scaffolding — never shipped application code — and this rule holds for every
      app repo that reuses the conformance suite.
    """
    paths: list[Path] = []
    for path in root.rglob("*.py"):
        # Exclude named infra / virtualenv dirs
        parts = set(path.parts)
        if parts & EXCLUDE_DIRS:
            continue
        # Exclude any dot-prefixed directory component (.github, .claude, …)
        rel_parts = path.relative_to(root).parts
        if any(p.startswith(".") for p in rel_parts[:-1]):
            continue
        # Exclude test files by name convention
        name = path.name
        if name.startswith("test_") or name.endswith("_test.py"):
            continue
        paths.append(path)
    return sorted(paths)


def main(argv: list[str] | None = None) -> int:
    """CLI entry point for E-series error-handling checks."""
    parser = argparse.ArgumentParser(
        description="E-series: scan Python files for error-handling anti-patterns."
    )
    parser.add_argument(
        "scan_paths",
        nargs="*",
        default=["."],
        metavar="PATH",
        help="Directories or files to scan (default: .)",
    )
    parser.add_argument(
        "--root",
        default=".",
        metavar="DIR",
        help="Repo root for relative URI construction (default: .)",
    )
    parser.add_argument(
        "--sarif-output",
        metavar="FILE",
        help="Write SARIF report to FILE (default: stdout)",
    )
    parser.add_argument(
        "--validate",
        action="store_true",
        help="Validate emitted SARIF against the official schema",
    )
    parser.add_argument("--tool-version", default="3.17.0", metavar="VERSION")
    args = parser.parse_args(argv)

    root = Path(args.root).resolve()
    findings: list[Finding] = []
    for raw in args.scan_paths:
        p = Path(raw)
        if not p.is_absolute():
            p = root / p
        if p.is_file():
            try:
                rel = p.relative_to(root)
            except ValueError:
                rel = p
            findings.extend(scan_text(p.read_text(encoding="utf-8"), str(rel)))
        elif p.is_dir():
            for py_file in discover(p):
                findings.extend(scan_path(py_file, root))

    report = findings_to_report(findings, tool_version=args.tool_version)

    if args.validate:
        from suite.schema.validate import validate_sarif

        validate_sarif(report)

    payload = json.dumps(report.model_dump(by_alias=True, exclude_none=True), indent=2)
    if args.sarif_output:
        Path(args.sarif_output).write_text(payload, encoding="utf-8")
    else:
        print(payload)

    return report.runs[0].invocations[0].exit_code  # type: ignore[return-value]


if __name__ == "__main__":
    sys.exit(main())
