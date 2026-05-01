"""Internal: build a PyIceberg REST catalog pointed at Polaris.

This is the only place we encode Polaris-specific connection details
(URL pattern, OAuth scope, sigv4-disabled). Public SDK surfaces funnel
through here so the credential acquisition story can evolve without
touching app code.
"""

from __future__ import annotations

import os

from pyiceberg.catalog import Catalog, load_catalog


def load_catalog_from_env() -> Catalog:
    """Build a PyIceberg REST catalog from environment variables.

    URI resolution:
      1. ``ICEBERG_CATALOG_URI`` — explicit URI (databricks-app, sample-app,
         popularity-app convention).
      2. ``ATLAN_DOMAIN_NAME`` — derive
         ``https://<domain>/api/polaris/api/catalog`` (publish-app convention).

    Required (in addition to a URI):
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
