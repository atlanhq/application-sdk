"""Internal: PyIceberg catalog construction and identifier helpers.

This module is the ONLY place pyiceberg's ``load_catalog`` is called. Public
SDK surfaces (``LakehouseReader.from_env`` / ``LakehouseWriter.from_env``) all
funnel through here so that the credential acquisition story can evolve
without touching app code.
"""

from __future__ import annotations

import logging
import os

from pyiceberg.catalog import Catalog, load_catalog

logger = logging.getLogger(__name__)


def load_catalog_from_env() -> Catalog:
    """Build a PyIceberg REST catalog from environment variables.

    Resolution order:
      1. ``ICEBERG_CATALOG_URI`` — explicit URI (used by databricks-app,
         sample-app, popularity-app).
      2. ``ATLAN_DOMAIN_NAME`` — derive
         ``https://<domain>/api/polaris/api/catalog`` (publish-app convention).

    Required:
      - One of the URI sources above.
      - ``ICEBERG_CLIENT_ID``
      - ``ICEBERG_CLIENT_SECRET``

    Optional:
      - ``ICEBERG_WAREHOUSE`` (default: ``context_store``)
    """
    uri = os.environ.get("ICEBERG_CATALOG_URI") or _uri_from_domain()
    if not uri:
        raise RuntimeError(
            "Lakehouse credentials not configured: set ICEBERG_CATALOG_URI "
            "(or ATLAN_DOMAIN_NAME), ICEBERG_CLIENT_ID, and "
            "ICEBERG_CLIENT_SECRET."
        )
    try:
        client_id = os.environ["ICEBERG_CLIENT_ID"]
        client_secret = os.environ["ICEBERG_CLIENT_SECRET"]
    except KeyError as missing:
        raise RuntimeError(
            f"Lakehouse credentials not configured: {missing.args[0]} is required."
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


def _uri_from_domain() -> str | None:
    domain = os.environ.get("ATLAN_DOMAIN_NAME")
    if not domain:
        return None
    return f"https://{domain}/api/polaris/api/catalog"


def identifier(namespace: str, table_name: str) -> tuple[str, ...]:
    """Build a fully-qualified identifier tuple.

    Supports nested namespaces written with dots, e.g. ``"apps.databricks"``
    becomes ``("apps", "databricks", table_name)``.
    """
    ns_parts = tuple(part for part in namespace.split(".") if part)
    return (*ns_parts, table_name)


def namespace_tuple(namespace: str) -> tuple[str, ...]:
    return tuple(part for part in namespace.split(".") if part)


def ensure_namespace(catalog: Catalog, namespace: str) -> None:
    """Create the namespace if it doesn't exist; no-op if it already does."""
    try:
        catalog.create_namespace(namespace_tuple(namespace))
    except Exception as exc:
        logger.debug("Namespace %s likely exists: %s", namespace, exc)
