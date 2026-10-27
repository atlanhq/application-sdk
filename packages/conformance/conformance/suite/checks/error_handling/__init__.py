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
import io
import re
import sys
import tokenize
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
from ..logging._helpers import is_script_file
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
    "scan_all",
    "scan_path",
    "scan_text",
]


# ---------------------------------------------------------------------------
# Public check API
# ---------------------------------------------------------------------------


_NON_EXPLANATORY_COMMENT = re.compile(
    r"#\s*(conformance\s*:|noqa\b|type\s*:\s*ignore|pragma\b|pyright\s*:|fmt\s*:)",
    re.IGNORECASE,
)


def _comment_lines_of(text: str) -> frozenset[int]:
    """Lines carrying an explanatory comment — tool directives do not count."""
    lines: set[int] = set()
    try:
        for token in tokenize.generate_tokens(io.StringIO(text).readline):
            if token.type == tokenize.COMMENT and not _NON_EXPLANATORY_COMMENT.match(
                token.string
            ):
                if token.string.lstrip("#").strip():
                    lines.add(token.start[0])
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return frozenset()
    return frozenset(lines)


def _preflight_lines_of(text: str, file: str, path: Path | None) -> frozenset[int]:
    """F005's preflight scope within this one file (no cross-file reach)."""
    from ..preflight._common import preflight_scope_lines, registry_from_texts

    registry = registry_from_texts([(path or Path(file), file, text)])
    return preflight_scope_lines(registry).get(file, frozenset())


def scan_text(
    text: str,
    file: str,
    *,
    path: Path | None = None,
    root: Path | None = None,
    preflight_lines: frozenset[int] | None = None,
) -> list[Finding]:
    """Scan Python source *text* and return all E-series findings.

    *path* and *root* let E004 and E021 follow the scanned repo's own imports
    when they resolve an error class's SDK ancestry (see :mod:`._sdk_errors`
    and :mod:`._dependency_category`), and let E007 see a deprecation marker
    on an enclosing package (see :mod:`.._deprecated_module`).

    *preflight_lines* is F005's preflight scope for this file — the ``def``
    lines of the functions F005 inspects — which E004 reuses so the two rules
    agree on where WARNING is barred.  :func:`_scan_all` computes it across
    the whole repo; without it, the scope is computed from this file alone.
    """
    try:
        tree = ast.parse(text, filename=file)
    except SyntaxError:
        return []
    if preflight_lines is None:
        preflight_lines = _preflight_lines_of(text, file, path)

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
        script_file=is_script_file(text, tree),
        preflight_lines=preflight_lines,
        comment_lines=_comment_lines_of(text),
    )
    checker.visit(tree)
    return checker._findings


def scan_path(
    path: Path, root: Path, *, preflight_lines: frozenset[int] | None = None
) -> list[Finding]:
    """Scan a single Python file, producing repo-root-relative URIs."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    try:
        rel = path.relative_to(root)
    except ValueError:
        rel = path
    return scan_text(
        text, str(rel), path=path, root=root, preflight_lines=preflight_lines
    )


def _scan_all(paths: list[Path], root: Path) -> list[Finding]:
    # scan_path, not scan_text: E004 and E021 follow the repo's own imports,
    # which needs each file's path and the repo root. F005's preflight scope
    # follows helpers across files, so it is computed once over every path.
    from ..preflight._common import build_registry, preflight_scope_lines

    scope = preflight_scope_lines(build_registry(paths, root))

    def _rel(path: Path) -> str:
        try:
            return str(path.relative_to(root))
        except ValueError:
            return str(path)

    return [
        finding
        for path in paths
        for finding in scan_path(
            path, root, preflight_lines=scope.get(_rel(path), frozenset())
        )
    ]


#: Whole-repo entry point for the suite runner: same findings as scanning each
#: path, plus F005's preflight scope followed across files for E004.
scan_all = _scan_all


main = make_cli_main(
    scan_all=_scan_all,
    discover=discover,
    description="E-series: scan Python files for error-handling anti-patterns.",
)
"""CLI entry point for E-series error-handling checks."""


if __name__ == "__main__":
    sys.exit(main())
