"""Parse Dapr component YAML files and create obstore stores.

Supports the following Dapr binding types:

    bindings.localstorage / bindings.local.storage → LocalStore
    bindings.aws.s3 / bindings.s3                  → S3Store
    bindings.azure.blobstorage                      → AzureStore
    bindings.gcp.bucket / bindings.gcs              → GCSStore
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
) -> "ObjectStore":
    """Create an obstore store from a Dapr component binding YAML file.

    Scans all ``*.yaml`` files in *components_dir* for a ``Component``
    whose ``metadata.name`` matches *name*, then creates the appropriate
    store based on ``spec.type``.

    Args:
        name: The Dapr component name (e.g. ``"objectstore"``).
        components_dir: Directory containing Dapr component YAML files.

    Returns:
        A configured obstore store instance.

    Raises:
        StorageConfigError: If no matching component is found, or the
            binding type is not supported.
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

    if store_kind == "s3":
        from obstore.store import S3Store  # noqa: PLC0415 — defensive: keep inline

        bucket = meta.get("bucket", "")
        config: dict[str, str] = {}
        client_options = None
        if "region" in meta:
            config["aws_region"] = meta["region"]
        if "accessKey" in meta:
            config["aws_access_key_id"] = meta["accessKey"]
        if "secretKey" in meta:
            config["aws_secret_access_key"] = meta["secretKey"]
        if "endpoint" in meta:
            config["aws_endpoint"] = meta["endpoint"]
            client_options = {"user_agent": "aws-sdk-go-v2 atlan-application-sdk"}
        if meta.get("forcePathStyle", "").lower() == "true":
            config["aws_virtual_hosted_style_request"] = "false"
        return S3Store(bucket=bucket, config=config, client_options=client_options)

    if store_kind == "azure":
        from obstore.store import AzureStore  # noqa: PLC0415 — defensive: keep inline

        account = meta.get("accountName", "")
        container = meta.get("containerName", "")
        az_config: dict[str, str] = {"azure_storage_account_name": account}
        if "accountKey" in meta:
            az_config["azure_storage_account_key"] = meta["accountKey"]
        return AzureStore(container_name=container, config=az_config)

    if store_kind == "gcs":
        import orjson  # noqa: PLC0415 — defensive: keep inline
        from obstore.store import GCSStore  # noqa: PLC0415 — defensive: keep inline

        bucket = meta.get("bucket", "")
        gcs_config: dict[str, str] = {}

        # Build service account JSON from Dapr component metadata fields
        # (injected from gcp-service-account-creds secret via Helm).
        sa_data = {k: meta[k] for k in GCS_SERVICE_ACCOUNT_FIELDS if k in meta}
        if sa_data:
            # Normalize escaped newlines in private_key PEM blocks — Helm
            # templating from K8s secrets can produce literal two-char "\n".
            if "private_key" in sa_data:
                sa_data["private_key"] = sa_data["private_key"].replace("\\n", "\n")
            gcs_config["service_account_key"] = orjson.dumps(sa_data).decode()

        return GCSStore(bucket=bucket, config=gcs_config)

    raise StorageConfigError(f"Store kind not implemented: {store_kind!r}")
