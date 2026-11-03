"""Whether a scanned file belongs to a wholesale-deprecated module.

A module is deprecated wholesale when importing it emits a
``DeprecationWarning`` — a top-level ``warnings.warn(..., DeprecationWarning)``
statement. A package marked that way in its ``__init__.py`` deprecates every
submodule under it, because no submodule can be imported without running it.

E007's logged-sentinel arm and P023 skip such files: they are being removed,
not fixed, so a finding there is noise that no remediation should act on. The
signal is the module's own marker, the same call the deprecated-symbol
manifest recognises, so no path list has to track what is deprecated.
"""

from __future__ import annotations

import ast
from functools import lru_cache
from pathlib import Path

from ._deprecation_warn import _is_deprecation_warn_call


def _emits_module_deprecation(tree: ast.Module) -> bool:
    return any(
        isinstance(statement, ast.Expr)
        and _is_deprecation_warn_call(statement.value) is not None
        for statement in tree.body
    )


@lru_cache(maxsize=256)
def _package_is_deprecated(init: Path) -> bool:
    try:
        tree = ast.parse(init.read_text(encoding="utf-8"), filename=str(init))
    except (OSError, SyntaxError, UnicodeDecodeError, ValueError):
        return False
    return _emits_module_deprecation(tree)


def module_is_deprecated(
    tree: ast.Module, *, path: Path | None = None, root: Path | None = None
) -> bool:
    """True when *tree* or any package enclosing *path* within *root* is deprecated.

    Without *path* only the file's own top level is consulted. Ancestor
    packages are walked up to, and including, *root*; a file outside *root*
    consults only its own directory's package.
    """
    if _emits_module_deprecation(tree):
        return True
    if path is None:
        return False
    resolved = path.resolve()
    top = root.resolve() if root is not None else resolved.parent
    for directory in resolved.parents:
        init = directory / "__init__.py"
        if init != resolved and init.is_file() and _package_is_deprecated(init):
            return True
        if directory == top or top not in directory.parents:
            break
    return False
