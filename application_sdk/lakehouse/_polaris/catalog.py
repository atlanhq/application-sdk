"""Internal: build a PyIceberg REST catalog pointed at Polaris.

This is the only place we encode Polaris-specific connection details
(URL pattern, OAuth scope, sigv4-disabled, per-cloud config). Public SDK
surfaces funnel through here so the credential acquisition story can
evolve without touching app code.

Cloud handling:

* On **Azure**, two extra config keys are required for Polaris to vend
  SAS tokens and for PyIceberg to use ``adlfs`` for storage I/O:
  ``header.X-Iceberg-Access-Delegation: vended-credentials`` and
  ``py-io-impl: pyiceberg.io.fsspec.FsspecFileIO``. The Daft ADLS
  scoped-key normalisation patch is also applied so Daft picks up the
  account-scoped keys Polaris vends.
* On **AWS** and **GCP**, default PyIceberg config is enough — vended
  S3/GCS credentials flow through PyIceberg's native FileIO.
"""

from __future__ import annotations

import os

from pyiceberg.catalog import Catalog, load_catalog

from application_sdk.lakehouse._polaris import cloud as _cloud
from application_sdk.lakehouse._polaris.azure_adls_patch import patch_daft_adls_io


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
      - ``CLOUD`` (default: ``aws``; one of ``aws|gcp|azure``)
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
    cloud = _cloud.detect_cloud()

    config: dict[str, str] = {
        "type": "rest",
        "uri": uri,
        "warehouse": warehouse,
        "credential": f"{client_id}:{client_secret}",
        "scope": "PRINCIPAL_ROLE:ALL",
        "rest.sigv4-enabled": "false",
    }
    if cloud == "azure":
        # Tell Polaris to vend SAS tokens, and use adlfs in PyIceberg.
        config["header.X-Iceberg-Access-Delegation"] = "vended-credentials"
        config["py-io-impl"] = "pyiceberg.io.fsspec.FsspecFileIO"
        # Normalise ADLS account-scoped keys for Daft's IO converter.
        patch_daft_adls_io()

    return load_catalog(warehouse, **config)


def _uri_from_domain() -> str | None:
    domain = os.environ.get("ATLAN_DOMAIN_NAME")
    if not domain:
        return None
    return f"https://{domain}/api/polaris/api/catalog"
