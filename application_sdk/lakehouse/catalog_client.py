"""Catalog client wrapping a PyIceberg Catalog.

Hides the catalog backend (Polaris, Nessie, Glue, etc.) behind a small surface
focused on identifier resolution and table loading. Apps do not need to know
which REST catalog is in use.

The module-level ``load_catalog_from_env`` builds a REST-catalog ``Catalog``
from the standard ``ICEBERG_*`` environment variables; both ``LakehouseReader``
and ``LakehouseWriter`` ship ``.from_env()`` factories that use it.
"""

from __future__ import annotations

import logging
import os

from pyiceberg.catalog import Catalog
from pyiceberg.table import Table

logger = logging.getLogger(__name__)


def load_catalog_from_env() -> Catalog:
    """Build a PyIceberg REST catalog from environment variables.

    Required:
      - ``ICEBERG_CATALOG_URI``
      - ``ICEBERG_CLIENT_ID``
      - ``ICEBERG_CLIENT_SECRET``

    Optional:
      - ``ICEBERG_WAREHOUSE`` (default: ``context_store``)
    """
    from pyiceberg.catalog import load_catalog

    try:
        uri = os.environ["ICEBERG_CATALOG_URI"]
        client_id = os.environ["ICEBERG_CLIENT_ID"]
        client_secret = os.environ["ICEBERG_CLIENT_SECRET"]
    except KeyError as missing:
        raise RuntimeError(
            f"Lakehouse credentials not configured: {missing.args[0]} is required. "
            f"Set ICEBERG_CATALOG_URI, ICEBERG_CLIENT_ID, and ICEBERG_CLIENT_SECRET."
        ) from missing
    warehouse = os.environ.get("ICEBERG_WAREHOUSE", "context_store")
    return load_catalog(
        warehouse,
        **{
            "type": "rest",
            "uri": uri,
            "warehouse": warehouse,
            "credential": f"{client_id}:{client_secret}",
            "scope": "PRINCIPAL_ROLE:ALL",
            "rest.sigv4-enabled": "false",
        },
    )


class CatalogClient:
    """Resolves dotted namespaces and loads Iceberg tables from a catalog."""

    def __init__(self, catalog: Catalog) -> None:
        self._catalog = catalog

    @classmethod
    def from_env(cls) -> CatalogClient:
        return cls(load_catalog_from_env())

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
