"""AST helpers shared across L-series rule modules."""

from __future__ import annotations

import ast
from typing import Literal

from ._constants import ADAPTER_MARKERS, LOG_METHODS, LOGGER_NAMES

Framework = Literal["stdlib", "structlog", "loguru", "unknown"]


# ---------------------------------------------------------------------------
# Framework detection
# ---------------------------------------------------------------------------


def detect_framework(tree: ast.Module) -> Framework:
    """Scan module-level imports and return the logging framework in use.

    Priority: structlog > loguru > stdlib.  When a file imports both stdlib
    ``logging`` and a higher-level framework (common in adapters), the
    higher-level framework wins.
    """
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                if root == "structlog":
                    found.add("structlog")
                elif root == "loguru":
                    found.add("loguru")
                elif root == "logging":
                    found.add("stdlib")
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            root = module.split(".")[0]
            if root == "structlog":
                found.add("structlog")
            elif root == "loguru":
                found.add("loguru")
            elif root == "logging":
                found.add("stdlib")
    if "structlog" in found:
        return "structlog"
    if "loguru" in found:
        return "loguru"
    if "stdlib" in found:
        return "stdlib"
    return "unknown"


# ---------------------------------------------------------------------------
# Logger-call recognition
# ---------------------------------------------------------------------------


def is_logger_call(node: ast.Call) -> bool:
    """True if *node* is a recognised ``logger.<method>(...)`` call.

    Matches both named-variable receivers (``LOGGER_NAMES``) and the stdlib
    module-level form (``logging.info(...)``), so L012/L013/L015 catch
    calls like ``logging.info("…", custom=1)``.
    """
    func = node.func
    if not isinstance(func, ast.Attribute):
        return False
    if func.attr not in LOG_METHODS:
        return False
    obj = func.value
    if not isinstance(obj, ast.Name):
        return False
    return obj.id in LOGGER_NAMES or obj.id == "logging"


def get_logger_method(node: ast.Call) -> str | None:
    """Return the method name when *node* is a logger call, else ``None``."""
    if not isinstance(node.func, ast.Attribute):
        return None
    attr = node.func.attr
    if attr not in LOG_METHODS:
        return None
    obj = node.func.value
    if isinstance(obj, ast.Name) and (obj.id in LOGGER_NAMES or obj.id == "logging"):
        return attr
    return None


def has_exc_info_true(call: ast.Call) -> bool:
    """True if the call has ``exc_info=True`` among its keywords."""
    for kw in call.keywords:
        if kw.arg == "exc_info":
            val = kw.value
            return isinstance(val, ast.Constant) and val.value is True
    return False


def has_exc_info_kwarg(call: ast.Call) -> bool:
    """True if the call has any ``exc_info=`` keyword (regardless of value)."""
    return any(kw.arg == "exc_info" for kw in call.keywords)


# ---------------------------------------------------------------------------
# Adapter-file detection
# ---------------------------------------------------------------------------


def is_adapter_file(tree: ast.Module) -> bool:
    """True if this file defines the logging adapter.

    Files that define ``AtlanLoggerAdapter`` or ``get_logger`` at module
    top-level are the logging infrastructure itself and are exempt from
    L017 (.exception() shim) and L018 (kwargs in the factory / adapter body).

    Only top-level definitions are checked (``tree.body``): a nested method
    named ``get_logger`` inside an unrelated class must not exempt the file.
    """
    for node in tree.body:
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name in ADAPTER_MARKERS:
                return True
    return False


# ---------------------------------------------------------------------------
# Loop-bound detection
# ---------------------------------------------------------------------------


def loop_is_bounded(loop_node: ast.For | ast.AsyncFor) -> bool:
    """True if the loop is clearly bounded to ≤ 10 iterations.

    Covers:
    * ``for x in range(n)`` — literal *n* ≤ 10
    * ``for x in [a, b, …]`` / ``(a, b, …)`` — literal collection ≤ 10 items
    """
    iter_ = loop_node.iter
    # range(n) with a single literal int argument
    if isinstance(iter_, ast.Call):
        func = iter_.func
        func_name = func.id if isinstance(func, ast.Name) else None
        if func_name == "range" and len(iter_.args) == 1:
            arg = iter_.args[0]
            if isinstance(arg, ast.Constant) and isinstance(arg.value, int):
                return arg.value <= 10
    # Literal sequence with a known small number of elements
    if isinstance(iter_, (ast.List, ast.Tuple, ast.Set)):
        return len(iter_.elts) <= 10
    return False


# ---------------------------------------------------------------------------
# __main__ block detection
# ---------------------------------------------------------------------------


def main_block_lines(tree: ast.Module) -> frozenset[int]:
    """Return all line numbers that fall inside ``if __name__ == '__main__':``."""
    lines: set[int] = set()
    for node in tree.body:
        if not isinstance(node, ast.If):
            continue
        test = node.test
        if not isinstance(test, ast.Compare):
            continue
        left = test.left
        if not (isinstance(left, ast.Name) and left.id == "__name__"):
            continue
        for comp in test.comparators:
            if isinstance(comp, ast.Constant) and comp.value == "__main__":
                for child in ast.walk(node):
                    if hasattr(child, "lineno"):
                        lines.add(child.lineno)  # type: ignore[attr-defined]
                break
    return frozenset(lines)


# ---------------------------------------------------------------------------
# Credential-name heuristics
# ---------------------------------------------------------------------------


def _is_credential_value_name(name: str) -> bool:
    """True if *name* looks like a credential *value* identifier.

    A name matches when it ends with one of the credential-value suffixes and
    does NOT also end with a label suffix (``_name``, ``_type``, ``_id``, …)
    that indicates the variable holds a label rather than the secret itself.
    """
    from ._constants import CREDENTIAL_LABEL_SUFFIXES, CREDENTIAL_VALUE_SUFFIXES

    lower = name.lower()
    # If the name ends with a label suffix, it is a label, not a value.
    if any(lower.endswith(suf) for suf in CREDENTIAL_LABEL_SUFFIXES):
        return False
    return any(lower.endswith(suf) or lower == suf for suf in CREDENTIAL_VALUE_SUFFIXES)
