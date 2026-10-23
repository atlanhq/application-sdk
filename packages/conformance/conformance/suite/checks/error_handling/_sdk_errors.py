"""Which names in a module are proven SDK ``AppError`` classes.

E004 accepts ``raise X(..., cause=exc) from None`` only when ``X`` is an SDK
error: ``AppError`` renders its cause through ``sanitize_cause_repr``, so the
failure survives redacted after the raw traceback is severed. Capitalisation
proves nothing about that — an app class that formats its cause raw would put a
credential-bearing exception in the log — so the class has to be traced to
``application_sdk.errors``.

A name qualifies when it is imported from ``application_sdk.errors`` (or a
submodule), or is a class whose base chain reaches one, defined in the same
module or in a module of the scanned repo it is imported from. Anything the
resolver cannot follow stays unproven.
"""

from __future__ import annotations

import ast
from pathlib import Path

from conformance.suite.checks._ast_common import repo_module_file

_SDK_ERRORS_MODULE = "application_sdk.errors"

#: Suffix marking a module alias in the returned set: ``"errors."`` means
#: ``errors.<Leaf>Error`` is an SDK error.
MODULE_ALIAS_SUFFIX = "."


def _is_sdk_errors_module(module: str | None) -> bool:
    return module is not None and (
        module == _SDK_ERRORS_MODULE or module.startswith(_SDK_ERRORS_MODULE + ".")
    )


def _is_error_leaf(name: str) -> bool:
    return name.rsplit(".", 1)[-1].endswith("Error")


class _Resolver:
    def __init__(self, root: Path | None) -> None:
        self._root = root.resolve() if root is not None else None
        self._trees: dict[Path, ast.Module | None] = {}

    def load(self, path: Path) -> ast.Module | None:
        if path not in self._trees:
            try:
                self._trees[path] = ast.parse(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, SyntaxError):
                self._trees[path] = None
        return self._trees[path]

    def module_file(
        self, module: str | None, level: int, current: Path | None
    ) -> Path | None:
        return repo_module_file(self._root, module, level, current)

    def module_aliases(self, tree: ast.Module) -> set[str]:
        """Local names bound to the ``application_sdk.errors`` module itself."""
        aliases: set[str] = set()
        for node in tree.body:
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if _is_sdk_errors_module(alias.name) and alias.asname:
                        aliases.add(alias.asname)
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                for alias in node.names:
                    if _is_sdk_errors_module(f"{node.module}.{alias.name}"):
                        aliases.add(alias.asname or alias.name)
        return aliases

    def is_sdk_error(
        self,
        tree: ast.Module,
        path: Path | None,
        name: str,
        visited: frozenset[tuple[Path | None, str]] = frozenset(),
    ) -> bool:
        key = (path, name)
        if key in visited:
            return False
        visited = visited | {key}
        for node in tree.body:
            if isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    if (alias.asname or alias.name) != name:
                        continue
                    if node.level == 0 and _is_sdk_errors_module(node.module):
                        return _is_error_leaf(alias.name)
                    target = self.module_file(node.module, node.level, path)
                    loaded = self.load(target) if target is not None else None
                    return loaded is not None and self.is_sdk_error(
                        loaded, target, alias.name, visited
                    )
        for node in tree.body:
            if isinstance(node, ast.ClassDef) and node.name == name:
                return any(
                    self._base_is_sdk_error(tree, path, base, visited)
                    for base in node.bases
                )
        return False

    def _base_is_sdk_error(
        self,
        tree: ast.Module,
        path: Path | None,
        base: ast.expr,
        visited: frozenset[tuple[Path | None, str]],
    ) -> bool:
        if isinstance(base, ast.Name):
            return self.is_sdk_error(tree, path, base.id, visited)
        if isinstance(base, ast.Attribute) and isinstance(base.value, ast.Name):
            return base.value.id in self.module_aliases(tree) and _is_error_leaf(
                base.attr
            )
        return False


def sdk_error_names(
    tree: ast.Module, *, path: Path | None = None, root: Path | None = None
) -> frozenset[str]:
    """Names in *tree* proven to be SDK ``AppError`` classes.

    Bare names (imports and module-level classes) plus each
    ``application_sdk.errors`` module alias with :data:`MODULE_ALIAS_SUFFIX`
    appended. Without *root*, imports from the scanned repo cannot be followed
    and stay unproven.
    """
    resolver = _Resolver(root)
    module_path = path.resolve() if path is not None else None
    candidates: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.ImportFrom):
            candidates.update(alias.asname or alias.name for alias in node.names)
        elif isinstance(node, ast.ClassDef):
            candidates.add(node.name)
    proven = {
        name for name in candidates if resolver.is_sdk_error(tree, module_path, name)
    }
    proven.update(
        alias + MODULE_ALIAS_SUFFIX for alias in resolver.module_aliases(tree)
    )
    return frozenset(proven)


def names_sdk_error(sdk_errors: frozenset[str], dotted: str | None) -> bool:
    """Whether *dotted* (``Leaf`` or ``alias.Leaf``) is in the proven set."""
    if dotted is None:
        return False
    if dotted in sdk_errors:
        return True
    prefix, _, leaf = dotted.rpartition(".")
    return (
        bool(prefix)
        and prefix + MODULE_ALIAS_SUFFIX in sdk_errors
        and (_is_error_leaf(leaf))
    )
