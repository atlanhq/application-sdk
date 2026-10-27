"""Which classes in a module carry the DEPENDENCY_UNAVAILABLE category.

E021 flags a DEPENDENCY_UNAVAILABLE error whose ``audience`` is overridden to
``USER``. Whether a class has that category is a question about its MRO, not
just about whether ``DependencyUnavailableError`` appears somewhere in its
ancestry: ``StorageNotFoundError(NotFoundError, StorageError)`` reaches it
through ``StorageError`` yet is NOT_FOUND, because ``NotFoundError`` comes first
and defines ``category``. So the category is read off the first base, in
declaration order, that resolves to an SDK error, unless the class body assigns
``category`` itself.

The SDK's own DEPENDENCY_UNAVAILABLE subclasses live in many modules
(``application_sdk.storage``, ``application_sdk.clients.azure``, ...) and are
not on disk inside a consumer app repo, so they are matched by name against
:data:`SDK_DEPENDENCY_UNAVAILABLE_CLASSES` whenever the import comes from an
``application_sdk`` module. ``tests/test_dependency_category_drift.py`` keeps
that set equal to a fresh read of the SDK source. App-defined intermediates are
followed through the scanned repo's own imports, as in :mod:`._sdk_errors`.
"""

from __future__ import annotations

import ast
from enum import Enum
from pathlib import Path

from ._sdk_errors import _Resolver as _SdkErrorResolver

_SDK_PACKAGE = "application_sdk"

#: Every SDK error class (outside ``application_sdk.testing``) whose category is
#: DEPENDENCY_UNAVAILABLE. Regenerate by hand from the drift test's output when
#: the SDK adds or re-bases one.
SDK_DEPENDENCY_UNAVAILABLE_CLASSES: frozenset[str] = frozenset(
    {
        "AwsClientCreationError",
        "AzureConnectionError",
        "AzureServiceError",
        "BindingError",
        "ColdStartRaceError",
        "CredentialVaultError",
        "DaprNotDetectedError",
        "DaprSidecarUnreachableError",
        "DependencyUnavailableError",
        "EventPublishError",
        "LockAcquisitionError",
        "MarkerUploadError",
        "ObjectStoreDownloadError",
        "ObjectStorePreflightError",
        "ObjectStoreReadError",
        "PubSubError",
        "RedisConnectionError",
        "RedisLockError",
        "RedisProtocolError",
        "SecretStoreError",
        "SecretStoreTransientError",
        "SecretStoreUnavailableError",
        "SecretStoreUnreachableError",
        "StateDownloadError",
        "StateStoreError",
        "StorageBucketRelocationError",
        "StorageError",
        "StorageGatewayAuthUnavailableError",
        "StorageGatewayNoRouteError",
        "TemporalAuthTokenAcquireError",
        "TemporalConnectError",
    }
)

_DEPENDENCY_UNAVAILABLE = "DEPENDENCY_UNAVAILABLE"


class _Category(Enum):
    DEPENDENCY_UNAVAILABLE = "dependency_unavailable"
    OTHER = "other"


def _is_sdk_module(module: str | None) -> bool:
    return module is not None and (
        module == _SDK_PACKAGE or module.startswith(_SDK_PACKAGE + ".")
    )


def _sdk_name_category(name: str) -> _Category | None:
    """Category of an SDK-imported name; ``None`` for a non-error (a mixin)."""
    if name in SDK_DEPENDENCY_UNAVAILABLE_CLASSES:
        return _Category.DEPENDENCY_UNAVAILABLE
    if name.endswith("Error"):
        return _Category.OTHER
    return None


def _assigned_category(body: list[ast.stmt]) -> _Category | None:
    """The category a class body assigns itself, if it assigns one.

    The last assignment wins, as it does when Python executes the body.
    """
    for stmt in reversed(body):
        if isinstance(stmt, ast.Assign):
            targets = stmt.targets
            value: ast.expr | None = stmt.value
        elif isinstance(stmt, ast.AnnAssign):
            targets = [stmt.target]
            value = stmt.value
        else:
            continue
        if value is None or not any(
            isinstance(t, ast.Name) and t.id == "category" for t in targets
        ):
            continue
        if isinstance(value, ast.Attribute) and value.attr == _DEPENDENCY_UNAVAILABLE:
            return _Category.DEPENDENCY_UNAVAILABLE
        return _Category.OTHER
    return None


class _Resolver(_SdkErrorResolver):
    """Reuses :mod:`._sdk_errors`' in-repo module loading; answers a different
    question of each class (its category, not whether it is an SDK error)."""

    @staticmethod
    def _sdk_module_aliases(tree: ast.Module) -> set[str]:
        """Local names bound to an ``application_sdk`` module."""
        aliases: set[str] = set()
        for node in tree.body:
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if _is_sdk_module(alias.name) and alias.asname:
                        aliases.add(alias.asname)
            elif (
                isinstance(node, ast.ImportFrom)
                and node.level == 0
                and _is_sdk_module(node.module)
            ):
                for alias in node.names:
                    if not alias.name[:1].isupper():
                        aliases.add(alias.asname or alias.name)
        return aliases

    def name_category(
        self,
        tree: ast.Module,
        path: Path | None,
        name: str,
        visited: frozenset[tuple[Path | None, str]] = frozenset(),
    ) -> _Category | None:
        key = (path, name)
        if key in visited:
            return None
        visited = visited | {key}
        for node in tree.body:
            if isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    if (alias.asname or alias.name) != name:
                        continue
                    if node.level == 0 and _is_sdk_module(node.module):
                        return _sdk_name_category(alias.name)
                    target = self.module_file(node.module, node.level, path)
                    loaded = self.load(target) if target is not None else None
                    if loaded is None:
                        return None
                    return self.name_category(loaded, target, alias.name, visited)
        for node in tree.body:
            if isinstance(node, ast.ClassDef) and node.name == name:
                return self.class_category(tree, path, node, visited)
        return None

    def class_category(
        self,
        tree: ast.Module,
        path: Path | None,
        node: ast.ClassDef,
        visited: frozenset[tuple[Path | None, str]] = frozenset(),
    ) -> _Category | None:
        own = _assigned_category(node.body)
        if own is not None:
            return own
        for base in node.bases:
            found = self._base_category(tree, path, base, visited)
            if found is not None:
                return found
        return None

    def _base_category(
        self,
        tree: ast.Module,
        path: Path | None,
        base: ast.expr,
        visited: frozenset[tuple[Path | None, str]],
    ) -> _Category | None:
        if isinstance(base, ast.Name):
            return self.name_category(tree, path, base.id, visited)
        if (
            isinstance(base, ast.Attribute)
            and isinstance(base.value, ast.Name)
            and base.value.id in self._sdk_module_aliases(tree)
        ):
            return _sdk_name_category(base.attr)
        return None


def dependency_unavailable_classes(
    tree: ast.Module, *, path: Path | None = None, root: Path | None = None
) -> frozenset[str]:
    """Names of the module-level classes in *tree* whose category is
    DEPENDENCY_UNAVAILABLE.

    Without *root*, bases imported from the scanned repo cannot be followed and
    those classes stay unproven (not flagged).
    """
    resolver = _Resolver(root)
    module_path = path.resolve() if path is not None else None
    return frozenset(
        node.name
        for node in tree.body
        if isinstance(node, ast.ClassDef)
        and resolver.class_category(tree, module_path, node)
        is _Category.DEPENDENCY_UNAVAILABLE
    )
