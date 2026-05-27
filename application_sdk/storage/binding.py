"""Parse Dapr component YAML files and create obstore stores.

Supports the following Dapr binding types:

    bindings.localstorage / bindings.local.storage → LocalStore
    bindings.aws.s3 / bindings.s3                  → S3Store
    bindings.azure.blobstorage                      → AzureStore
    bindings.gcp.bucket / bindings.gcs              → GCSStore

Dapr metadata fields that have no obstore equivalent are silently ignored:
  decodeBase64, encodeBase64 (S3/GCS/Azure — Dapr-layer payload transforms)
  storageClass (S3), contentType (GCS)
  publicAccessLevel, disableEntityManagement, getBlobRetryCount (Azure)
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from obstore.store import ObjectStore

# GCP service account JSON fields injected from Kubernetes secret via Helm.
GCS_SERVICE_ACCOUNT_FIELDS: tuple[str, ...] = (
    "type",
    "project_id",
    "private_key_id",
    "private_key",
    "client_email",
    "client_id",
    "auth_uri",
    "token_uri",
    "auth_provider_x509_cert_url",
    "client_x509_cert_url",
    "universe_domain",
)

# Map Dapr binding type strings to store kind tokens.
BINDING_TYPE_MAP: dict[str, str] = {
    "bindings.localstorage": "local",
    "bindings.local.storage": "local",
    "bindings.aws.s3": "s3",
    "bindings.s3": "s3",
    "bindings.azure.blobstorage": "azure",
    "bindings.gcp.bucket": "gcs",
    "bindings.gcs": "gcs",
}

# Dapr azureEnvironment enum → obstore / Azure authority host URLs.
_AZURE_AUTHORITY_HOSTS: dict[str, str] = {
    "AzurePublicCloud": "https://login.microsoftonline.com",
    "AzureChinaCloud": "https://login.chinacloudapi.cn",
    "AzureUSGovernmentCloud": "https://login.microsoftonline.us",
    "AzureGermanCloud": "https://login.microsoftonline.de",
}

# Lazy logger — deferred to break circular import (observability ↔ storage).
_logger = None


def _get_logger():
    global _logger
    if _logger is None:
        from application_sdk.observability.logger_adaptor import (  # noqa: PLC0415
            get_logger,
        )

        _logger = get_logger(__name__)
    return _logger


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _nonempty(meta: dict[str, str], *keys: str) -> str:
    """Return the first non-empty string value for any of *keys*, or ''."""
    for k in keys:
        v = meta.get(k, "")
        if v:
            return v
    return ""


def _coerce_bool(value: str) -> bool:
    """Coerce a Dapr metadata bool string to Python bool (case-insensitive)."""
    return value.strip().lower() in {"true", "1", "yes"}


def _resolve_metadata_value(item: dict) -> str:
    """Resolve a single Dapr metadata item to its string value.

    Supports plain ``value`` fields and ``secretKeyRef`` references.
    For ``secretKeyRef``, the secret is resolved from environment variables
    using the ref's ``key`` (falling back to ``name``).  This mirrors the
    behaviour of the ``secretstores.local.env`` Dapr component used in
    Docker Compose / SDR deployments where secrets are injected as env vars.
    """
    if "value" in item:
        return str(item["value"])

    secret_ref = item.get("secretKeyRef")
    if secret_ref:
        env_key = secret_ref.get("key") or secret_ref.get("name", "")
        if env_key:
            return os.environ.get(env_key, "")

    return ""


def _parse_dapr_metadata(metadata_list: list[dict[str, str]]) -> dict[str, str]:
    """Convert Dapr metadata list format to a flat dict.

    Handles both plain ``value`` entries and ``secretKeyRef`` entries
    (resolved via environment variables).
    """
    return {
        item["name"]: _resolve_metadata_value(item) for item in (metadata_list or [])
    }


def create_store_from_binding(
    name: str,
    *,
    components_dir: Path | str = Path("./components"),
) -> ObjectStore:
    """Create an obstore store from a Dapr component binding YAML file.

    Scans all ``*.yaml`` files in *components_dir* for a ``Component``
    whose ``metadata.name`` matches *name*, then creates the appropriate
    store based on ``spec.type``.

    Supported auth modes by store type:

    **S3** (``bindings.aws.s3`` / ``bindings.s3``):
    - Static access key: ``accessKey`` + ``secretKey`` (+ optional ``sessionToken``)
    - AssumeRole via STS: ``assumeRoleArn`` (+ ``sessionName``)
    - EC2 instance profile / IRSA / env vars: omit all credential fields

    **Azure Blob** (``bindings.azure.blobstorage``):
    - Account key: ``accountKey``
    - SAS token: ``sasToken`` / ``sasKey``
    - Service principal: ``azureTenantId`` + ``azureClientId`` + ``azureClientSecret``
    - AKS Workload Identity: ``azureTenantId`` + ``azureClientId`` (AZURE_FEDERATED_TOKEN_FILE injected by webhook)
    - User-assigned managed identity: ``azureClientId`` only
    - Certificate-based SP: ``azureCertificateFile`` or ``azureCertificate`` + ``azureTenantId`` + ``azureClientId``
    - System-assigned managed identity / DefaultAzureCredential: omit all credential fields

    **GCS** (``bindings.gcp.bucket`` / ``bindings.gcs``):
    - Inline SA key: any of the SA JSON fields that include ``private_key`` or ``private_key_id``
    - ADC / Workload Identity: ``bucket`` + ``project_id`` only (no private_key)

    Args:
        name: The Dapr component name (e.g. ``"objectstore"``).
        components_dir: Directory containing Dapr component YAML files.

    Returns:
        A configured obstore store instance.

    Raises:
        StorageConfigError: If no matching component is found, the
            binding type is not supported, or a required option is invalid.
    """
    import yaml  # noqa: PLC0415 — defensive: keep inline

    from application_sdk.storage.errors import (  # noqa: PLC0415 — circular: storage/__init__.py loads sibling modules
        StorageConfigError,
    )

    components_path = Path(components_dir)
    component: dict | None = None

    for yaml_file in sorted(components_path.glob("*.yaml")):
        with yaml_file.open() as fh:
            doc = yaml.safe_load(fh)
        if (
            doc
            and doc.get("kind") == "Component"
            and doc.get("metadata", {}).get("name") == name
        ):
            component = doc
            break

    if component is None:
        raise StorageConfigError(
            f"No Dapr component named '{name}' found in {components_path}"
        )

    spec = component.get("spec", {})
    binding_type: str = spec.get("type", "")
    store_kind = BINDING_TYPE_MAP.get(binding_type)

    if store_kind is None:
        raise StorageConfigError(
            f"Unsupported binding type: {binding_type!r} (component={name})"
        )

    meta = _parse_dapr_metadata(spec.get("metadata", []))

    if store_kind == "local":
        root_path = meta.get("rootPath", "./objectstore")
        from application_sdk.storage.factory import (  # noqa: PLC0415 — circular: storage/__init__.py loads sibling modules
            create_local_store,
        )

        return create_local_store(root_path)

    # BLDX-1155: every store gets the SDK-default ClientConfig + RetryConfig
    # so timeouts and pool sizes are sized for GB-class transfers, not for
    # small objects.  Without this, S3Store falls back to obstore's small-
    # object defaults (30 s timeout) and 100 MB+ downloads time out mid-stream.
    from application_sdk.storage._obstore_config import (  # noqa: PLC0415
        log_obstore_config,
        obstore_client_options,
        obstore_retry_config,
    )

    sdk_client_options = obstore_client_options()
    sdk_retry_config = obstore_retry_config()

    if store_kind == "s3":
        from obstore.store import S3Store  # noqa: PLC0415 — defensive: keep inline

        bucket = meta.get("bucket", "")
        config: dict[str, str] = {}
        client_options: dict[str, object] = dict(sdk_client_options)
        credential_provider = None

        if _nonempty(meta, "region"):
            config["aws_region"] = meta["region"]
        if _nonempty(meta, "endpoint"):
            config["aws_endpoint"] = meta["endpoint"]
            client_options["user_agent"] = "aws-sdk-go-v2 atlan-application-sdk"
        if _coerce_bool(meta.get("forcePathStyle", "")):
            config["aws_virtual_hosted_style_request"] = "false"
        if _coerce_bool(meta.get("disableSSL", "")):
            client_options["allow_http"] = True
        if _coerce_bool(meta.get("insecureSSL", "")):
            client_options["allow_invalid_certificates"] = True
            _get_logger().warning(
                "insecureSSL=true disables certificate verification for S3 binding '%s'",
                name,
            )

        if _nonempty(meta, "trustAnchorArn"):
            raise StorageConfigError(
                "IAM Roles Anywhere (trustAnchorArn) is not yet supported; "
                "use assumeRoleArn for STS or omit credentials for IRSA / instance-profile auth"
            )

        if _nonempty(meta, "assumeRoleArn"):
            from application_sdk.storage._credential_providers import (  # noqa: PLC0415
                make_s3_assume_role_provider,
            )

            credential_provider = make_s3_assume_role_provider(
                role_arn=meta["assumeRoleArn"],
                session_name=_nonempty(meta, "sessionName") or "atlan-application-sdk",
                region=_nonempty(meta, "region") or None,
                base_access_key=_nonempty(meta, "accessKey") or None,
                base_secret_key=_nonempty(meta, "secretKey") or None,
            )
        else:
            if _nonempty(meta, "accessKey"):
                config["aws_access_key_id"] = meta["accessKey"]
            if _nonempty(meta, "secretKey"):
                config["aws_secret_access_key"] = meta["secretKey"]
            if _nonempty(meta, "sessionToken"):
                config["aws_session_token"] = meta["sessionToken"]

        log_obstore_config(
            "s3", client_options=client_options, retry_config=sdk_retry_config
        )
        return S3Store(
            bucket=bucket,
            config=config if config else None,
            client_options=client_options,
            retry_config=sdk_retry_config,
            credential_provider=credential_provider,
        )

    if store_kind == "azure":
        from obstore.store import AzureStore  # noqa: PLC0415 — defensive: keep inline

        account = meta.get("accountName", "")
        container = meta.get("containerName", "")
        az_config: dict[str, str] = {}
        az_client_options: dict[str, object] = dict(sdk_client_options)
        az_credential_provider = None

        if account:
            az_config["azure_storage_account_name"] = account
        if _nonempty(meta, "endpoint"):
            az_config["azure_storage_endpoint"] = meta["endpoint"]
            if meta["endpoint"].startswith("http://"):
                az_client_options["allow_http"] = True
        if _coerce_bool(meta.get("useEmulator", "")):
            az_config["azure_storage_use_emulator"] = "true"
            az_client_options["allow_http"] = True

        if _nonempty(meta, "azureEnvironment"):
            env_name = meta["azureEnvironment"]
            host = _AZURE_AUTHORITY_HOSTS.get(env_name)
            if host is None:
                raise StorageConfigError(
                    f"Unknown azureEnvironment {env_name!r}. "
                    f"Valid values: {', '.join(_AZURE_AUTHORITY_HOSTS)}"
                )
            az_config["azure_storage_authority_host"] = host

        tenant_id = _nonempty(meta, "azureTenantId", "tenantId")
        client_id = _nonempty(meta, "azureClientId", "clientId")
        client_secret = _nonempty(meta, "azureClientSecret")
        account_key = _nonempty(meta, "accountKey")
        sas_token = _nonempty(meta, "sasToken", "sasKey")
        cert_data = _nonempty(meta, "azureCertificate")
        cert_file = _nonempty(meta, "azureCertificateFile")
        cert_password = _nonempty(meta, "azureCertificatePassword")

        if account_key:
            az_config["azure_storage_account_key"] = account_key
        elif sas_token:
            az_config["azure_storage_sas_key"] = sas_token
        elif cert_data or cert_file:
            from application_sdk.storage._credential_providers import (  # noqa: PLC0415
                make_azure_certificate_provider,
            )

            az_credential_provider = make_azure_certificate_provider(
                tenant_id=tenant_id,
                client_id=client_id,
                certificate_path=cert_file or None,
                certificate_data=cert_data.encode() if cert_data else None,
                certificate_password=cert_password or None,
                authority_host=az_config.get("azure_storage_authority_host"),
            )
        elif tenant_id and client_id and client_secret:
            az_config["azure_storage_tenant_id"] = tenant_id
            az_config["azure_storage_client_id"] = client_id
            az_config["azure_storage_client_secret"] = client_secret
        elif tenant_id and client_id:
            # AKS Workload Identity: the AAD webhook injects AZURE_FEDERATED_TOKEN_FILE;
            # obstore picks it up when tenant_id + client_id are set with no secret.
            az_config["azure_storage_tenant_id"] = tenant_id
            az_config["azure_storage_client_id"] = client_id
            if _nonempty(meta, "azureFederatedTokenFile"):
                az_config["azure_storage_federated_token_file"] = meta[
                    "azureFederatedTokenFile"
                ]
        elif client_id:
            az_config["azure_storage_client_id"] = client_id

        if _nonempty(meta, "msiEndpoint"):
            az_config["azure_storage_msi_endpoint"] = meta["msiEndpoint"]
        if _nonempty(meta, "msiResourceId"):
            az_config["azure_storage_msi_resource_id"] = meta["msiResourceId"]

        log_obstore_config(
            "azure", client_options=az_client_options, retry_config=sdk_retry_config
        )
        return AzureStore(
            container_name=container,
            config=az_config if az_config else None,
            client_options=az_client_options,
            retry_config=sdk_retry_config,
            credential_provider=az_credential_provider,
        )

    if store_kind == "gcs":
        import orjson  # noqa: PLC0415 — defensive: keep inline
        from obstore.store import GCSStore  # noqa: PLC0415 — defensive: keep inline

        bucket = meta.get("bucket", "")
        gcs_config: dict[str, str] = {}

        # Build service-account JSON only when actual credential material is
        # supplied.  ``project_id`` alone is not credential material — daprd's
        # gcp-bucket binding schema requires ``project_id`` in component
        # metadata even on the ADC path (Workload Identity / GKE metadata
        # server / GOOGLE_APPLICATION_CREDENTIALS), so a config containing
        # only ``bucket`` + ``project_id`` must fall through to ADC rather
        # than be synthesised into a partial SA JSON that obstore would
        # reject for missing ``private_key``.
        if any(meta.get(f) for f in ("private_key", "private_key_id")):
            sa_data = {k: meta[k] for k in GCS_SERVICE_ACCOUNT_FIELDS if k in meta}
            if "private_key" in sa_data:
                sa_data["private_key"] = sa_data["private_key"].replace("\\n", "\n")
            gcs_config["google_service_account_key"] = orjson.dumps(sa_data).decode()

        log_obstore_config(
            "gcs", client_options=sdk_client_options, retry_config=sdk_retry_config
        )
        return GCSStore(
            bucket=bucket,
            config=gcs_config if gcs_config else None,
            client_options=sdk_client_options,
            retry_config=sdk_retry_config,
        )

    raise StorageConfigError(f"Store kind not implemented: {store_kind!r}")
