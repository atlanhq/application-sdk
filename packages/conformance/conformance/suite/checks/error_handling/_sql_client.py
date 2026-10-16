"""Which classes in a module are the SDK's SQL client, and whether the app's SDK binds.

E022 grades a ``run_query`` override on a subclass of the SDK's
``BaseSQLClient``.  A class qualifies when a base resolves to one of
:data:`SDK_SQL_CLIENT_CLASSES` imported from :data:`SDK_SQL_CLIENT_MODULES`
(a bare name, aliased or not, or an attribute through a module alias), or to
a class that does: defined in the same module, or imported by name from a
module of the scanned repo (absolute or relative).  Anything the resolver
cannot follow stays unproven and is not graded.

A class defined in the module shadows an import of the same name, so the SDK's
own ``application_sdk/clients/sql.py`` — where ``AsyncBaseSQLClient`` derives
from the *locally declared* ``BaseSQLClient`` — never resolves as a subclass of
itself.

Those two class names and the parameter the base declares are what the SDK
declares today.  ``tests/test_run_query_params.py`` re-reads them from
``application_sdk/clients/sql.py`` on every SDK-side run, so a rename, a new
public ``BaseSQLClient`` subclass, or a dropped ``params`` fails the suite
before it can grade the fleet against a stale shape.

SDK-version gate
----------------
``params`` first ships on ``BaseSQLClient.run_query`` in application-sdk
3.46.0.  Below it the override has nothing to match, so the gate reads the
SDK version the app's ``uv.lock`` resolves (the reader ``P051`` / ``P053`` /
``P056`` use) and stays silent unless it is confirmed at or above
:data:`RUN_QUERY_PARAMS_SDK_FLOOR`.
"""

from __future__ import annotations

import ast
from functools import lru_cache
from pathlib import Path

from conformance.suite.checks._version import (
    locked_sdk_version,
    parse_version,
    version_reached,
)

from ._sdk_errors import _Resolver as _RepoResolver

#: Modules the SDK exports its SQL client classes from.
SDK_SQL_CLIENT_MODULES: frozenset[str] = frozenset(
    {"application_sdk.clients", "application_sdk.clients.sql"}
)

#: The SDK's public ``BaseSQLClient`` and its public subclasses.
SDK_SQL_CLIENT_CLASSES: frozenset[str] = frozenset(
    {"BaseSQLClient", "AsyncBaseSQLClient"}
)

#: The method the SDK makes the override point, and the argument it must keep.
RUN_QUERY = "run_query"
RUN_QUERY_PARAM = "params"

#: First application-sdk release whose ``BaseSQLClient.run_query`` takes
#: ``params`` (application-sdk#4310).  Silent below it.
RUN_QUERY_PARAMS_SDK_FLOOR = (3, 46, 0)


def _dotted(expr: ast.expr) -> str | None:
    if isinstance(expr, ast.Name):
        return expr.id
    if isinstance(expr, ast.Attribute):
        head = _dotted(expr.value)
        return f"{head}.{expr.attr}" if head is not None else None
    return None


def _is_sdk_client(qualified: str) -> bool:
    module, _, name = qualified.rpartition(".")
    return module in SDK_SQL_CLIENT_MODULES and name in SDK_SQL_CLIENT_CLASSES


class _Resolver(_RepoResolver):
    """Reuses :mod:`._sdk_errors`' in-repo module loading to follow class bases."""

    @staticmethod
    def _absolute_imports(tree: ast.Module) -> dict[str, str]:
        """Module-level names bound by an absolute import, to what they name."""
        bound: dict[str, str] = {}
        for node in tree.body:
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.asname:
                        bound[alias.asname] = alias.name
                    else:
                        root = alias.name.split(".")[0]
                        bound[root] = root
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                for alias in node.names:
                    bound[alias.asname or alias.name] = f"{node.module}.{alias.name}"
        return bound

    def name_is_client(
        self,
        tree: ast.Module,
        path: Path | None,
        name: str,
        visited: frozenset[tuple[Path | None, str]],
    ) -> bool:
        """True if *name*, as bound at module level in *tree*, is an SDK SQL client."""
        key = (path, name)
        if key in visited:
            return False
        visited = visited | {key}
        for node in tree.body:
            if isinstance(node, ast.ClassDef) and node.name == name:
                return self.class_is_client(tree, path, node, visited)
        for node in tree.body:
            if not isinstance(node, ast.ImportFrom):
                continue
            for alias in node.names:
                if (alias.asname or alias.name) != name:
                    continue
                if node.level == 0 and node.module is not None:
                    if _is_sdk_client(f"{node.module}.{alias.name}"):
                        return True
                    if node.module.split(".")[0] == "application_sdk":
                        return False
                target = self._module_file(node.module, node.level, path)
                loaded = self._load(target) if target is not None else None
                if loaded is None:
                    return False
                return self.name_is_client(loaded, target, alias.name, visited)
        return False

    def class_is_client(
        self,
        tree: ast.Module,
        path: Path | None,
        node: ast.ClassDef,
        visited: frozenset[tuple[Path | None, str]] = frozenset(),
    ) -> bool:
        return any(self._base_is_client(tree, path, b, visited) for b in node.bases)

    def _base_is_client(
        self,
        tree: ast.Module,
        path: Path | None,
        base: ast.expr,
        visited: frozenset[tuple[Path | None, str]],
    ) -> bool:
        if isinstance(base, ast.Name):
            return self.name_is_client(tree, path, base.id, visited)
        dotted = _dotted(base)
        if dotted is None:
            return False
        head, _, rest = dotted.partition(".")
        origin = self._absolute_imports(tree).get(head)
        return origin is not None and _is_sdk_client(f"{origin}.{rest}")


def sql_client_classes(
    tree: ast.Module, *, path: Path | None = None, root: Path | None = None
) -> frozenset[str]:
    """Names of the module-level classes in *tree* that derive from the SDK's
    ``BaseSQLClient``.

    Without *root*, bases imported from the scanned repo cannot be followed and
    those classes stay unproven (not graded).
    """
    resolver = _Resolver(root)
    module_path = path.resolve() if path is not None else None
    return frozenset(
        node.name
        for node in tree.body
        if isinstance(node, ast.ClassDef)
        and resolver.class_is_client(tree, module_path, node)
    )


@lru_cache(maxsize=32)
def _gate(root: Path, _lock_stamp: int) -> bool:
    locked = locked_sdk_version(root)
    if locked is None:
        return False
    parsed = parse_version(locked)
    return parsed is not None and version_reached(RUN_QUERY_PARAMS_SDK_FLOOR, parsed)


def sdk_binds_run_query_params(root: Path) -> bool:
    """True iff ``root/uv.lock`` resolves an SDK whose ``run_query`` takes ``params``.

    Fails closed — an unreadable version is not evidence the argument exists,
    and asking an app to accept one its SDK never passes is a finding nobody
    can act on.  The E-series scans file by file, so the lock is read once per
    repo (keyed on its modification time) rather than once per file.
    """
    try:
        stamp = (root / "uv.lock").stat().st_mtime_ns
    except OSError:
        return False
    return _gate(root.resolve(), stamp)
