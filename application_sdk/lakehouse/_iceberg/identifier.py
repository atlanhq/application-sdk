"""Internal: Iceberg-format identifier and namespace helpers.

Generic across IRC implementations. ``identifier`` and ``namespace_tuple``
follow the pyiceberg convention of representing nested namespaces as tuples;
``ensure_namespace`` only swallows the specific "already exists" exception
so permission/network errors aren't silently masked.
"""

from __future__ import annotations

import logging

from pyiceberg.catalog import Catalog
from pyiceberg.exceptions import NamespaceAlreadyExistsError

logger = logging.getLogger(__name__)


def identifier(namespace: str, table_name: str) -> tuple[str, ...]:
    """Build a fully-qualified table identifier tuple.

    Supports nested namespaces written with dots, e.g. ``"apps.databricks"``
    becomes ``("apps", "databricks", table_name)``.
    """
    ns_parts = tuple(part for part in namespace.split(".") if part)
    return (*ns_parts, table_name)


def namespace_tuple(namespace: str) -> tuple[str, ...]:
    return tuple(part for part in namespace.split(".") if part)


def ensure_namespace(catalog: Catalog, namespace: str) -> None:
    """Create the namespace if it doesn't exist; no-op if it already does.

    Only ``NamespaceAlreadyExistsError`` is swallowed (logged at DEBUG); any
    other exception (auth, network, catalog config) propagates so callers
    don't mistake "permission denied" for "namespace exists".
    """
    try:
        catalog.create_namespace(namespace_tuple(namespace))
    except NamespaceAlreadyExistsError as exc:
        logger.debug("Namespace %s already exists: %s", namespace, exc)
