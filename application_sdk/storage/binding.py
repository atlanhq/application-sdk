"""Parse Dapr component YAML files and create obstore stores.

Supports the following Dapr binding types:

    bindings.localstorage / bindings.local.storage → LocalStore
    bindings.aws.s3 / bindings.s3                  → S3Store
    bindings.azure.blobstorage                      → AzureStore
    bindings.gcp.bucket / bindings.gcs              → GCSStore
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from obstore.store import ObjectStore

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


def _parse_dapr_metadata(metadata_list: list[dict[str, str]]) -> dict[str, str]:
    """Convert Dapr metadata list format to a flat dict."""
    return {item["name"]: str(item.get("value", "")) for item in (metadata_list or [])}


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
    import yaml

    from application_sdk.storage.errors import StorageConfigError

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
        from application_sdk.storage.factory import create_local_store

        return create_local_store(root_path)

    if store_kind == "s3":
        from obstore.store import S3Store

        bucket = meta.get("bucket", "")
        config: dict[str, str] = {}
        if "region" in meta:
            config["aws_region"] = meta["region"]
        if "accessKey" in meta:
            config["aws_access_key_id"] = meta["accessKey"]
        if "secretKey" in meta:
            config["aws_secret_access_key"] = meta["secretKey"]
        return S3Store(bucket=bucket, config=config)

    if store_kind == "azure":
        from obstore.store import AzureStore

        account = meta.get("accountName", "")
        container = meta.get("containerName", "")
        az_config: dict[str, str] = {"azure_storage_account_name": account}
        if "accountKey" in meta:
            az_config["azure_storage_account_key"] = meta["accountKey"]
        return AzureStore(container_name=container, config=az_config)

    if store_kind == "gcs":
        import json

        from obstore.store import GCSStore

        bucket = meta.get("bucket", "")
        gcs_config: dict[str, str] = {}

        # Build service account JSON from Dapr component metadata fields
        # (injected from gcp-service-account-creds secret via Helm).
        _sa_fields = [
            "type", "project_id", "private_key_id", "private_key",
            "client_email", "client_id", "auth_uri", "token_uri",
            "auth_provider_x509_cert_url", "client_x509_cert_url",
        ]
        sa_data = {k: meta[k] for k in _sa_fields if k in meta}
        if sa_data:
            gcs_config["service_account_key"] = json.dumps(sa_data)

        return GCSStore(bucket=bucket, config=gcs_config if gcs_config else None)

    raise StorageConfigError(f"Store kind not implemented: {store_kind!r}")
