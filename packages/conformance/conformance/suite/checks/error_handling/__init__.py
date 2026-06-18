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

import ast
import sys
from pathlib import Path

from conformance.suite.checks._ast_common import (
    EXCLUDE_DIRS,
    _IgnoreDirective,
    _parse_directives,
    discover,
    make_cli_main,
    parse_ignore_directive,
)
from conformance.suite.schema.findings import Finding

from ._checker import Checker
from ._collect import _collect_imports, _collect_legacy_aliases
from ._constants import (
    _ATLAN_LEGACY_MODULE,
    ACTIVITY_DECORATORS,
    BUILTIN_RAISES,
    INTEROP_DECORATORS,
    INTEROP_METHODS,
    LEAF_CLASSES,
    LEGACY_ATLAN_ERRORS,
    SERIES,
)
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


main = make_cli_main(
    scan_text,
    description="E-series: scan Python files for error-handling anti-patterns.",
)
"""CLI entry point for E-series error-handling checks."""


if __name__ == "__main__":
    sys.exit(main())
