"""L-series logging checks — AST-based.

Scans Python source files for logging anti-patterns defined in the L001–L020
rule catalog.  Every check is purely deterministic: the same source text always
produces the same set of findings.

Inline suppression
------------------
Add a ``# conformance: ignore[LXXX] <reason>`` comment on the offending line
or on the line immediately above it to acknowledge a finding with justification::

    logger.critical("fatal!")  # conformance: ignore[L007] PagerDuty alert path

    # conformance: ignore[L005] CLI progress indicator, not production logging
    print("Done.")

Package layout
--------------
Rule implementations are split by category:

* ``_format``      — L001, L003, L011, L014, L018, L020 (message-format rules)
* ``_level``       — L006, L007, L017 (log-level rules)
* ``_traceback``   — L004 (missing-traceback)
* ``_security``    — L010 (credential in log)
* ``_performance`` — L008, L009 (performance/noise rules)
* ``_config``      — L012, L013, L015, L019 (per-file config/crash rules)
* ``_print``       — L005 (print bypass)

Cross-file rules (require seeing all files before emitting findings):

* ``L002`` InconsistentLoggerFactory — implemented in :func:`scan_all`
* ``L016`` BasicConfigNoopAfterFirstCall — implemented in :func:`scan_all`

``_checker.py`` assembles the per-file mixins into a single ``Checker`` visitor.
``__init__.py`` (this file) exposes the public scan + CLI API.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

from conformance.suite.checks._ast_common import (
    _IgnoreDirective,
    _parse_directives,
    discover,
    make_cli_main,
    make_finding,
)
from conformance.suite.schema.findings import Finding

from ._checker import build_checker
from ._constants import SERIES
from ._performance import WarnThenRaiseVisitor

__all__ = ["SERIES", "discover", "main", "scan_all", "scan_path", "scan_text"]


# ---------------------------------------------------------------------------
# Per-file scan (L001–L015, L017–L020; excludes cross-file L002, L016)
# ---------------------------------------------------------------------------


def scan_text(text: str, file: str) -> list[Finding]:
    """Scan Python source *text* and return all per-file L-series findings."""
    from conformance.suite.checks._ast_common._directives import (  # noqa: PLC0415
        _parse_directives,
    )

    directives = _parse_directives(text)
    result = build_checker(text, file, directives)
    if result is None:
        return []
    checker, tree = result
    checker.visit(tree)
    findings = list(checker._findings)

    # L009 uses its own visitor (needs statement-list context)
    l009_visitor = WarnThenRaiseVisitor(filename=file, directives=directives)
    l009_visitor.visit(tree)
    findings.extend(l009_visitor._findings)

    return findings


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


# ---------------------------------------------------------------------------
# Cross-file helpers for L002 and L016
# ---------------------------------------------------------------------------


def _detect_factory(tree: ast.Module) -> str | None:
    """Return a factory-type string for the logger factory used in *tree*.

    Returns ``None`` when no logger factory call or loguru import is found —
    the file is not a logger-using module.

    Factory types:
    * ``"stdlib"``   — ``logging.getLogger(...)`` / ``getLogger(...)``
    * ``"structlog"`` — ``structlog.get_logger(...)``
    * ``"sdk"``      — bare ``get_logger(...)`` (SDK canonical adapter)
    * ``"loguru"``   — ``from loguru import logger``
    """
    # Check for loguru import first (no factory call needed)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if (node.module or "").split(".")[0] == "loguru":
                for alias in node.names:
                    if alias.name == "logger":
                        return "loguru"

    # Check for factory calls in assignments: logger = X.get_logger(...)
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        rhs: ast.expr | None = None
        if isinstance(node, ast.Assign):
            rhs = node.value
        elif isinstance(node, ast.AnnAssign):
            rhs = node.value
        if rhs is None:
            continue
        if not isinstance(rhs, ast.Call):
            continue
        func = rhs.func
        # structlog.get_logger(...)
        if (
            isinstance(func, ast.Attribute)
            and func.attr == "get_logger"
            and isinstance(func.value, ast.Name)
            and func.value.id == "structlog"
        ):
            return "structlog"
        # logging.getLogger(...)
        if (
            isinstance(func, ast.Attribute)
            and func.attr == "getLogger"
            and isinstance(func.value, ast.Name)
            and func.value.id == "logging"
        ):
            return "stdlib"
        # bare get_logger(...) — SDK canonical
        if isinstance(func, ast.Name) and func.id == "get_logger":
            return "sdk"
        # bare getLogger(...) — stdlib without module prefix
        if isinstance(func, ast.Name) and func.id == "getLogger":
            return "stdlib"
    return None


def _collect_basicconfig_calls(
    tree: ast.Module, filename: str, directives: dict[int, _IgnoreDirective]
) -> list[Finding]:
    """Collect placeholder findings for every ``basicConfig()`` call.

    The caller (``scan_all``) promotes the 2nd+ non-main calls to real L016
    findings.  We record position here and filter in the cross-file pass.
    """
    from ._helpers import main_block_lines

    main_lines = main_block_lines(tree)
    calls: list[Finding] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        is_basicconfig = (
            isinstance(func, ast.Attribute) and func.attr == "basicConfig"
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


# ---------------------------------------------------------------------------
# Cross-file scan — full suite including L002 + L016
# ---------------------------------------------------------------------------


def scan_all(paths: list[Path], root: Path) -> list[Finding]:
    """Multi-pass scan over *paths*, emitting all L-series findings.

    Pass 1 — parse every file, run per-file checks (L001–L015, L017–L020),
    and collect data for cross-file rules (factory types, basicConfig calls).

    Pass 2 — emit L002 for files deviating from the dominant logger factory,
    and L016 for 2nd+ non-main basicConfig() calls.
    """
    findings: list[Finding] = []

    # Pass 1 — per-file rules + data collection
    factory_counts: dict[str, int] = {}
    factory_by_file: dict[str, str] = {}  # rel_path -> factory_type
    factory_node_by_file: dict[
        str, tuple[ast.AST, str]
    ] = {}  # rel_path -> (node, filename)
    basicconfig_calls: list[Finding] = []  # placeholder findings for L016

    for path in paths:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        try:
            tree = ast.parse(text, filename=str(path))
        except SyntaxError:
            continue
        try:
            rel = path.relative_to(root)
        except ValueError:
            rel = path
        rel_str = str(rel)
        directives = _parse_directives(text)

        # Per-file rules
        result = build_checker(text, rel_str, directives)
        if result is not None:
            checker, tree_parsed = result
            checker.visit(tree_parsed)
            findings.extend(checker._findings)

            l009_visitor = WarnThenRaiseVisitor(filename=rel_str, directives=directives)
            l009_visitor.visit(tree_parsed)
            findings.extend(l009_visitor._findings)

            tree = tree_parsed  # use the already-parsed tree for cross-file data

        # L002 — collect factory type for this file
        factory = _detect_factory(tree)
        if factory is not None:
            factory_counts[factory] = factory_counts.get(factory, 0) + 1
            factory_by_file[rel_str] = factory
            # Record the first matching AST node for the finding location
            factory_node_by_file[rel_str] = (tree, rel_str)

        # L016 — collect basicConfig calls from this file
        basicconfig_calls.extend(_collect_basicconfig_calls(tree, rel_str, directives))

    # Pass 2a — L002 InconsistentLoggerFactory
    if len(factory_counts) > 1:
        dominant = max(factory_counts, key=lambda k: factory_counts[k])
        for rel_str, factory in factory_by_file.items():
            if factory == dominant:
                continue
            tree_ref, fname = factory_node_by_file[rel_str]
            # Emit a file-level finding (line 1, col 1)
            fake_node = ast.Module(body=[], type_ignores=[])
            fake_node.lineno = 1  # type: ignore[attr-defined]
            fake_node.col_offset = 0  # type: ignore[attr-defined]
            directives_empty: dict[int, _IgnoreDirective] = {}
            findings.append(
                make_finding(
                    filename=rel_str,
                    rule_id="L002",
                    node=fake_node,
                    message=(
                        f"Logger factory '{factory}' differs from the dominant factory "
                        f"'{dominant}' ({factory_counts[dominant]} files). "
                        "Mixed factories produce incompatible record formats and break "
                        "correlation IDs. Switch to the canonical factory."
                    ),
                    directives=directives_empty,
                )
            )

    # Pass 2b — L016 BasicConfigNoopAfterFirstCall
    if len(basicconfig_calls) > 1:
        # The first call is acceptable; flag all subsequent ones
        for finding in basicconfig_calls[1:]:
            findings.append(finding)

    return findings


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

main = make_cli_main(
    scan_all=scan_all,
    description="L-series: scan Python files for logging anti-patterns.",
)
"""CLI entry point for L-series logging checks."""


if __name__ == "__main__":
    sys.exit(main())
