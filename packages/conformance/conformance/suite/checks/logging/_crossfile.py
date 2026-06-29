"""Cross-file helpers for L002 (NonCanonicalLoggerFactory) and L016 (BasicConfigNoop).

These functions require the full set of parsed module trees and are called from
``scan_all`` in ``__init__.py``; they are extracted here so ``__init__.py`` stays
as pure public-API orchestration.
"""

from __future__ import annotations

import ast

from conformance.suite.checks._ast_common import _IgnoreDirective, make_finding
from conformance.suite.schema.findings import Finding

from ._constants import FACTORY_PATTERNS
from ._helpers import main_block_lines


def _detect_factory(tree: ast.Module) -> str | None:
    """Return the logger factory type for *tree*.

    Returns ``None`` when no logger acquisition is detected — the file does
    not obtain a logger object and should not be checked by L002.

    Factory types (first match wins):
    * ``"sdk_adapter"`` — ``from application_sdk... import get_logger`` (canonical)
    * ``"loguru"``      — ``from loguru import logger`` (direct loguru)
    * ``"structlog"``   — ``structlog.get_logger(...)`` in an assignment
    * ``"stdlib"``      — ``logging.getLogger(...)`` / bare ``getLogger(...)`` in an assignment
    * ``"sdk_adapter"`` — bare ``get_logger(...)`` with no stdlib alias in scope
    """
    # 1. SDK adapter import — most specific, check before anything else
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if "application_sdk" in (node.module or ""):
                for alias in node.names:
                    if alias.name == "get_logger":
                        return "sdk_adapter"

    # 2. Loguru direct import (from loguru import logger)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if (node.module or "").split(".")[0] == "loguru":
                for alias in node.names:
                    if alias.name == "logger":
                        return "loguru"

    # Collect aliases of stdlib getLogger so that
    # ``from logging import getLogger as get_logger`` is not misclassified
    # as the SDK adapter in the bare-call branch below.
    stdlib_getlogger_aliases: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if (node.module or "").split(".")[0] == "logging":
                for alias in node.names:
                    if alias.name == "getLogger":
                        stdlib_getlogger_aliases.add(alias.asname or alias.name)

    # 3–5. Factory calls in assignments — use FACTORY_PATTERNS for attr-calls
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        rhs: ast.expr | None = node.value
        if rhs is None or not isinstance(rhs, ast.Call):
            continue
        func = rhs.func
        # Attribute calls: mod.attr(...) matched against FACTORY_PATTERNS
        if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
            for mod, attr, factory in FACTORY_PATTERNS:
                if mod and func.value.id == mod and func.attr == attr:
                    return factory
        # Bare name calls
        elif isinstance(func, ast.Name):
            if func.id == "getLogger":
                return "stdlib"
            if func.id == "get_logger":
                # Aliased from stdlib → treat as stdlib, not SDK adapter
                return (
                    "stdlib" if func.id in stdlib_getlogger_aliases else "sdk_adapter"
                )

    return None


def _collect_basicconfig_calls(
    tree: ast.Module, filename: str, directives: dict[int, _IgnoreDirective]
) -> list[Finding]:
    """Collect placeholder findings for every ``logging.basicConfig()`` call.

    The caller (``scan_all``) promotes the 2nd+ non-main calls to real L016
    findings.  We record position here and filter in the cross-file pass.

    Only ``logging.basicConfig()`` and its aliases (``import logging as L;
    L.basicConfig()``) are collected.  Third-party methods that happen to be
    named ``basicConfig`` (e.g. ``mock.basicConfig()``) are excluded by
    restricting the Attribute-call branch to receivers whose name is bound to
    the ``logging`` module.
    """
    # Collect names bound to 'import logging [as X]' to avoid false-positives
    logging_names: set[str] = {"logging"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] == "logging":
                    logging_names.add(alias.asname or alias.name.split(".")[0])

    main_lines = main_block_lines(tree)
    calls: list[Finding] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        is_basicconfig = (
            isinstance(func, ast.Attribute)
            and func.attr == "basicConfig"
            and isinstance(func.value, ast.Name)
            and func.value.id in logging_names
        ) or (isinstance(func, ast.Name) and func.id == "basicConfig")
        if not is_basicconfig:
            continue
        lineno = getattr(node, "lineno", 1)
        if lineno in main_lines:
            continue  # inside __main__ block — exempt
        calls.append(
            make_finding(
                filename=filename,
                rule_id="L016",
                node=node,
                message=(
                    "Multiple logging.basicConfig() calls — second and later calls "
                    "are silent no-ops (basicConfig() does nothing when the root "
                    "logger already has handlers). Consolidate into a single call."
                ),
                directives=directives,
            )
        )
    return calls
