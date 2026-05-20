"""Internal: per-cloud DuckDB ``CREATE SECRET`` builders for Polaris ATTACH.

Used by future ``LakehouseQuery`` ATTACH paths and any other SDK code that
needs DuckDB to read directly from Polaris-managed object storage. Mirrors
the patterns in automation-engine-app's ``DuckDBSqlEngine`` ``_setup_*``
methods.

This module does NOT execute SQL itself — callers pass in a DuckDB
connection. Each function issues exactly the ``CREATE SECRET`` statements
required and logs at INFO level.

Cloud paths:

  * **AWS**: ``credential_chain`` S3 secret. DuckDB walks the AWS SDK
    default chain (env vars → config → IMDS → IRSA).
  * **GCP**: native ``gcs`` ``credential_chain`` secret. Resolves HMAC keys
    from AWS-named env vars (mapped from the ``gcp-hmac-keys`` k8s secret
    by the Helm chart).
  * **Azure**: extracts a Polaris-vended SAS token by loading any table in
    the namespace via PyIceberg and reading the SAS from ``table.config``.
    Creates a DuckDB Azure secret with ``CONNECTION_STRING``.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

from application_sdk.lakehouse._polaris.cloud import aws_region

if TYPE_CHECKING:
    import duckdb
    from pyiceberg.catalog import Catalog

logger = logging.getLogger(__name__)

_ADLS_SAS_TOKEN_RE = re.compile(r"^adls\.sas-token\.([^.]+)\.dfs\.core\.windows\.net$")

# Azure storage account names: 3-24 lowercase alphanumeric (Microsoft spec).
_AZURE_ACCOUNT_NAME_RE = re.compile(r"^[a-z0-9]{3,24}$")
# SAS tokens are URL-encoded query strings — must not contain quotes or
# semicolons (which would break out of the SQL string or CONNECTION_STRING).
_SAS_TOKEN_DISALLOWED_RE = re.compile(r"['\";]")


def create_aws_s3_secret(con: duckdb.DuckDBPyConnection) -> None:
    """Create DuckDB S3 ``credential_chain`` secret. Region from ``AWS_REGION``."""
    region = aws_region()
    logger.info(
        "[AWS] creating s3 credential_chain secret (region=%s)", region or "<unset>"
    )
    con.execute(
        f"""
        CREATE SECRET s3_secret (
            TYPE s3,
            PROVIDER credential_chain,
            REGION '{region}'
        );
        """
    )


def create_gcp_gcs_secret(con: duckdb.DuckDBPyConnection) -> None:
    """Create DuckDB GCS ``credential_chain`` secret.

    Relies on ``AWS_ACCESS_KEY_ID`` / ``AWS_SECRET_ACCESS_KEY`` being mapped
    from the ``gcp-hmac-keys`` k8s secret (Helm chart convention). DuckDB's
    GCS provider reads HMAC keys from those AWS-named env vars natively.
    """
    logger.info("[GCP] creating gcs credential_chain secret")
    con.execute(
        """
        CREATE SECRET gcs_secret (
            TYPE gcs,
            PROVIDER credential_chain
        );
        """
    )


def create_azure_secret_from_polaris(
    con: duckdb.DuckDBPyConnection,
    catalog: Catalog,
    namespace: str,
) -> None:
    """Create a DuckDB Azure secret using a Polaris-vended SAS token.

    Loads any table in ``namespace`` to trigger Polaris SAS vending, extracts
    the account-scoped SAS from ``table.config``, and creates the DuckDB
    Azure secret. Raises ``ValueError`` if the namespace has no tables or
    Polaris does not vend an Azure SAS for the table.
    """
    con.execute("INSTALL azure; LOAD azure;")

    tables = catalog.list_tables(namespace)
    if not tables:
        raise ValueError(
            f"No tables in namespace {namespace!r} — cannot fetch Azure vended credentials"
        )
    table = catalog.load_table(tables[0])
    config = table.config

    sas_token: str | None = None
    account_name: str | None = None
    for key, value in config.items():
        m = _ADLS_SAS_TOKEN_RE.match(key)
        if m:
            account_name = m.group(1)
            sas_token = value
            break

    if not sas_token or not account_name:
        raise ValueError(
            f"Polaris did not vend Azure SAS token for {tables[0]}; "
            f"config keys present: {list(config.keys())}"
        )

    # Validate before f-string interpolation into CREATE SECRET. The values
    # come from a Polaris response so the trust level is "infrastructure"
    # not "user", but defense-in-depth — a malformed config entry here is
    # otherwise a SQL injection vector.
    if not _AZURE_ACCOUNT_NAME_RE.match(account_name):
        raise ValueError(
            f"Invalid Azure account name from Polaris: {account_name!r} "
            "(expected 3-24 lowercase alphanumeric)"
        )
    if _SAS_TOKEN_DISALLOWED_RE.search(sas_token):
        raise ValueError(
            "SAS token from Polaris contains disallowed characters "
            "(quote/semicolon) — refusing to interpolate into SQL"
        )

    logger.info(
        "[Azure] vended SAS extracted: account=%s, token=%s",
        account_name,
        ("***" + sas_token[-4:]) if sas_token else "(empty)",
    )
    con.execute(
        f"""
        CREATE SECRET azure_secret (
            TYPE AZURE,
            PROVIDER CONFIG,
            CONNECTION_STRING 'AccountName={account_name};SharedAccessSignature={sas_token}'
        );
        """
    )
