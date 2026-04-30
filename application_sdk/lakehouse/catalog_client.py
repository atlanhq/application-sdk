"""Catalog client wrapping a PyIceberg Catalog.

Hides the catalog backend (Polaris, Nessie, Glue, etc.) behind a small surface
focused on identifier resolution and table loading. Apps do not need to know
which REST catalog is in use.
"""

from __future__ import annotations

import logging

from pyiceberg.catalog import Catalog
from pyiceberg.table import Table

logger = logging.getLogger(__name__)


class CatalogClient:
    """Resolves dotted namespaces and loads Iceberg tables from a catalog."""

    def __init__(self, catalog: Catalog) -> None:
        self._catalog = catalog

    @property
    def catalog(self) -> Catalog:
        return self._catalog

    @staticmethod
    def identifier(namespace: str, table_name: str) -> tuple[str, ...]:
        """Build a fully-qualified identifier tuple.

        Supports nested namespaces written with dots, e.g. ``"apps.databricks"``
        becomes ``("apps", "databricks", table_name)``.
        """
        ns_parts = tuple(part for part in namespace.split(".") if part)
        return (*ns_parts, table_name)

    def load_table(self, namespace: str, table_name: str) -> Table:
        return self._catalog.load_table(self.identifier(namespace, table_name))

    def table_exists(self, namespace: str, table_name: str) -> bool:
        try:
            self.load_table(namespace, table_name)
            return True
        except Exception:
            return False

    def ensure_namespace(self, namespace: str) -> None:
        """Create the namespace if it doesn't exist; no-op if it already does."""
        try:
            ns_parts = tuple(part for part in namespace.split(".") if part)
            self._catalog.create_namespace(ns_parts)
        except Exception as exc:
            logger.debug("Namespace %s likely exists: %s", namespace, exc)
