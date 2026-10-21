"""E-series error-handling checks — AST-based.

Scans Python source files for error-handling anti-patterns defined in the
E001–E021 rule catalog.  Every check is purely deterministic: the same
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
* ``exception_chaining`` — E015, E016, E019 (chaining and message hygiene)
* ``security``           — E017 (secrets in error evidence fields)
* ``dependency_audience`` — E021 (DEPENDENCY_UNAVAILABLE overridden to USER)

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

from .._deprecated_module import module_is_deprecated
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
from ._dependency_category import dependency_unavailable_classes
from ._helpers import is_broad_suppress, is_builtin_raise, module_helpers
from ._sdk_errors import sdk_error_names
from .exception_chaining import exception_helpers

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


def scan_text(
    text: str, file: str, *, path: Path | None = None, root: Path | None = None
) -> list[Finding]:
    """Scan Python source *text* and return all E-series findings.

    *path* and *root* let E004 and E021 follow the scanned repo's own imports
    when they resolve an error class's SDK ancestry (see :mod:`._sdk_errors`
    and :mod:`._dependency_category`), and let E007 see a deprecation marker
    on an enclosing package (see :mod:`.._deprecated_module`).
    """
    try:
        tree = ast.parse(text, filename=file)
    except SyntaxError:
        return []

    imports = _collect_imports(tree)
    atlan_ioerror = imports.get("IOError") == _ATLAN_LEGACY_MODULE
    legacy_aliases = _collect_legacy_aliases(tree)

    directives = _parse_directives(text)
    local_helpers = module_helpers(tree)
    sdk_errors = sdk_error_names(tree, path=path, root=root)
    raised_error_helpers = exception_helpers(
        tree, path=path, root=root, sdk_errors=sdk_errors
    )
    checker = Checker(
        filename=file,
        directives=directives,
        atlan_ioerror_imported=atlan_ioerror,
        legacy_aliases=legacy_aliases,
        local_helpers=local_helpers,
        sdk_errors=sdk_errors,
        raised_error_helpers=raised_error_helpers,
        dependency_classes=dependency_unavailable_classes(tree, path=path, root=root),
        deprecated_module=module_is_deprecated(tree, path=path, root=root),
    )
    checker.visit(tree)
    return checker._findings


def scan_path(path: Path, root: Path) -> list[Finding]:
    """Scan a single Python file, producing repo-root-relative URIs."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    try:
        rel = path.relative_to(root)
    except ValueError:
        rel = path
    return scan_text(text, str(rel), path=path, root=root)


def _scan_all(paths: list[Path], root: Path) -> list[Finding]:
    # scan_path, not scan_text: E004 and E021 follow the repo's own imports,
    # which needs each file's path and the repo root.
    return [finding for path in paths for finding in scan_path(path, root)]


main = make_cli_main(
    scan_all=_scan_all,
    discover=discover,
    description="E-series: scan Python files for error-handling anti-patterns.",
)
"""CLI entry point for E-series error-handling checks."""


if __name__ == "__main__":
    sys.exit(main())
