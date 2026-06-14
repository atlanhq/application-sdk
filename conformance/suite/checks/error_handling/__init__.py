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

Package layout
--------------
Rule implementations are split by category:

* ``silent_swallow``     — E001–E011, E014 (swallowing without logging/re-raising)
* ``untyped_raise``      — E012, E013, E018 (untyped or legacy raise sites)
* ``exception_chaining`` — E015, E016 (chaining and message hygiene)
* ``security``           — E017 (secrets in error evidence fields)

``_checker.py`` assembles these into a single ``Checker`` visitor.
``__init__.py`` (this file) exposes the public scan + CLI API.
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
from pathlib import Path

from suite.schema.findings import Finding, findings_to_report

from ._checker import Checker
from ._collect import _collect_imports, _collect_legacy_aliases
from ._constants import (
    _ATLAN_LEGACY_MODULE,
    ACTIVITY_DECORATORS,
    BUILTIN_RAISES,
    EXCLUDE_DIRS,
    INTEROP_DECORATORS,
    INTEROP_METHODS,
    LEAF_CLASSES,
    LEGACY_ATLAN_ERRORS,
    SERIES,
)
from ._directives import _IgnoreDirective, _parse_directives, parse_ignore_directive
from ._helpers import is_broad_suppress, is_builtin_raise

__all__ = [
    "ACTIVITY_DECORATORS",
    "BUILTIN_RAISES",
    "EXCLUDE_DIRS",
    "INTEROP_DECORATORS",
    "INTEROP_METHODS",
    "LEAF_CLASSES",
    "LEGACY_ATLAN_ERRORS",
    "SERIES",
    "_IgnoreDirective",
    "discover",
    "is_broad_suppress",
    "is_builtin_raise",
    "main",
    "parse_ignore_directive",
    "scan_path",
    "scan_text",
]


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
