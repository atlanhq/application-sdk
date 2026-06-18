"""L-series logging checks — AST-based and TOML-based.

Scans Python source files for logging anti-patterns defined in the L001–L021
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

Cross-file and TOML rules (require seeing all files / project config):

* ``L002`` InconsistentLoggerFactory — implemented in :func:`scan_all`
* ``L016`` BasicConfigNoopAfterFirstCall — implemented in :func:`scan_all`
* ``L021`` MissingLoggingLintRules — TOML check in :func:`scan_all` (``_toml.py``)

``_checker.py`` assembles the per-file mixins into a single ``Checker`` visitor.
``_crossfile.py`` implements the cross-file helpers for L002 and L016.
``_toml.py`` implements the ``pyproject.toml`` ruff-config check (L021).
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
from ._crossfile import _collect_basicconfig_calls, _detect_factory
from ._helpers import is_adapter_file
from ._performance import WarnThenRaiseVisitor
from ._toml import check_ruff_config

__all__ = ["SERIES", "discover", "main", "scan_all", "scan_path", "scan_text"]


# ---------------------------------------------------------------------------
# Per-file scan (L001–L015, L017–L020; excludes cross-file L002, L016)
# ---------------------------------------------------------------------------


def scan_text(text: str, file: str) -> list[Finding]:
    """Scan Python source *text* and return all per-file L-series findings."""
    directives = _parse_directives(text)
    result = build_checker(text, file, directives)
    if result is None:
        return []
    checker, tree = result
    checker.visit(tree)
    findings = list(checker._findings)

    # L009 uses its own visitor (needs statement-list context)
    l009_visitor = WarnThenRaiseVisitor(
        filename=file,
        directives=directives,
        logging_module_names=checker._logging_module_names,
    )
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
# Cross-file scan — full suite including L002 + L016
# ---------------------------------------------------------------------------


def scan_all(paths: list[Path], root: Path) -> list[Finding]:
    """Multi-pass scan over *paths*, emitting all L-series findings.

    Pass 1 — parse every file, run per-file checks (L001–L015, L017–L021),
    and collect data for cross-file rules (factory types, basicConfig calls).

    Pass 2a — L002: flag every file that obtains a logger via a non-SDK factory
    (``logging.getLogger``, ``structlog.get_logger``, direct loguru import)
    instead of the canonical SDK adapter.

    Pass 2b — L016: flag 2nd+ non-main basicConfig() calls.

    Pass 3  — L021: check pyproject.toml for required ruff logging lint rules.
    """
    findings: list[Finding] = []

    # Pass 1 — per-file rules + data collection
    factory_by_file: dict[str, str] = {}  # rel_path -> factory_type
    adapter_by_file: dict[str, bool] = {}  # rel_path -> is adapter/definition file
    directives_by_file: dict[str, dict[int, _IgnoreDirective]] = {}
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
        directives_by_file[rel_str] = directives

        # Per-file rules
        result = build_checker(text, rel_str, directives)
        if result is not None:
            checker, tree_parsed = result
            checker.visit(tree_parsed)
            findings.extend(checker._findings)

            l009_visitor = WarnThenRaiseVisitor(
                filename=rel_str,
                directives=directives,
                logging_module_names=checker._logging_module_names,
            )
            l009_visitor.visit(tree_parsed)
            findings.extend(l009_visitor._findings)

            tree = tree_parsed  # use the already-parsed tree for cross-file data

        # L002 — collect factory type and adapter status for this file
        factory = _detect_factory(tree)
        if factory is not None:
            factory_by_file[rel_str] = factory
        adapter_by_file[rel_str] = is_adapter_file(tree)

        # L016 — collect basicConfig calls from this file
        basicconfig_calls.extend(_collect_basicconfig_calls(tree, rel_str, directives))

    # Pass 2a — L002 NonCanonicalLoggerFactory
    # Emit a finding for every file that uses a non-SDK logger factory.
    # Adapter/definition files are exempt (they implement the factory itself).
    _FACTORY_LABEL = {
        "stdlib": "stdlib logging.getLogger()",
        "structlog": "structlog.get_logger()",
        "loguru": "from loguru import logger",
    }
    for rel_str, factory in factory_by_file.items():
        if factory == "sdk_adapter":
            continue
        if adapter_by_file.get(rel_str, False):
            continue
        label = _FACTORY_LABEL.get(factory, factory)
        fake_node = ast.Module(body=[], type_ignores=[])
        fake_node.lineno = 1  # type: ignore[attr-defined]
        fake_node.col_offset = 0  # type: ignore[attr-defined]
        findings.append(
            make_finding(
                filename=rel_str,
                rule_id="L002",
                node=fake_node,
                message=(
                    f"Non-canonical logger factory ({label}) — use "
                    "`from application_sdk.observability.logger_adaptor import "
                    "get_logger` instead. Direct use of stdlib logging, structlog, "
                    "or loguru bypasses the adapter that injects Temporal correlation "
                    "IDs and routes records through OTel."
                ),
                directives=directives_by_file.get(rel_str, {}),
            )
        )

    # Pass 2b — L016 BasicConfigNoopAfterFirstCall
    if len(basicconfig_calls) > 1:
        # The first call is acceptable; flag all subsequent ones
        for finding in basicconfig_calls[1:]:
            findings.append(finding)

    # Pass 3 — L021 MissingLoggingLintRules (pyproject.toml TOML check)
    toml_path = root / "pyproject.toml"
    if toml_path.exists():
        findings.extend(check_ruff_config(toml_path, root))

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
