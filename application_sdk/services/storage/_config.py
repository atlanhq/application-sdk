"""Storage configuration: DAPR YAML parsing + env var auto-detection + store factory.

Two configuration paths (tried in order):
1. DAPR YAML parsing — reuses existing component YAML files
2. Env var auto-detection — fallback for simpler setups
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from application_sdk.services.storage._errors import StorageConfigError

logger = logging.getLogger(__name__)

# DAPR binding type to provider mapping
BINDING_TYPE_MAP = {
    "bindings.aws.s3": "s3",
    "bindings.gcp.bucket": "gcs",
    "bindings.azure.blobstorage": "azure",
    "bindings.local.storage": "local",
    "bindings.localstorage": "local",
}


@dataclass
class StorageBindingConfig:
    """Parsed storage binding configuration.

    Contains all information needed to create an obstore instance.
    """

    name: str
    """Component name."""

    provider: str
    """Storage provider: s3, gcs, azure, local, or memory."""

    bucket: str
    """Bucket name (S3/GCS) or container name (Azure)."""

    config: dict[str, Any] = field(default_factory=dict)
    """Provider-specific configuration for obstore constructor."""


# =============================================================================
# Environment Variable expansion
# =============================================================================


def _expand_env_vars(value: str) -> str:
    """Expand environment variables in a value. Supports ${VAR} and $VAR syntax."""
    if not isinstance(value, str):
        return value

    result = value
    for match in re.finditer(r"\$\{([^}]+)\}", value):
        var_name = match.group(1)
        var_value = os.environ.get(var_name, "")
        result = result.replace(match.group(0), var_value)

    for match in re.finditer(r"\$([A-Za-z_][A-Za-z0-9_]*)", result):
        var_name = match.group(1)
        if f"${{{var_name}}}" not in value:
            var_value = os.environ.get(var_name, "")
            result = result.replace(match.group(0), var_value)

    return result


def _metadata_to_dict(metadata_list: list[dict[str, str]]) -> dict[str, str]:
    """Convert DAPR metadata list to dict, expanding env vars."""
    result = {}
    for item in metadata_list:
        name = item.get("name", "")
        value = item.get("value", "")
        if name:
            result[name] = _expand_env_vars(value)
    return result


# =============================================================================
# DAPR YAML Parsing (provider-specific)
# =============================================================================


def _parse_s3_config(metadata: dict[str, str]) -> tuple[str, dict[str, Any]]:
    """Parse S3 binding metadata into obstore config."""
    bucket = metadata.get("bucket", "")
    if not bucket:
        raise StorageConfigError("S3 binding missing required 'bucket' field")

    config: dict[str, Any] = {}
    if "region" in metadata:
        config["region"] = metadata["region"]
    if metadata.get("accessKey"):
        config["access_key_id"] = metadata["accessKey"]
    if metadata.get("secretKey"):
        config["secret_access_key"] = metadata["secretKey"]
    if metadata.get("sessionToken"):
        config["session_token"] = metadata["sessionToken"]
    if metadata.get("endpoint"):
        config["endpoint"] = metadata["endpoint"]
    if "forcePathStyle" in metadata:
        force_path = metadata["forcePathStyle"].lower() == "true"
        config["virtual_hosted_style_request"] = not force_path

    return bucket, config


def _parse_gcs_config(metadata: dict[str, str]) -> tuple[str, dict[str, Any]]:
    """Parse GCS binding metadata into obstore config."""
    bucket = metadata.get("bucket", "")
    if not bucket:
        raise StorageConfigError("GCS binding missing required 'bucket' field")

    config: dict[str, Any] = {}
    if "client_email" in metadata and "private_key" in metadata:
        sa_key = {
            "type": "service_account",
            "client_email": metadata["client_email"],
            "private_key": metadata["private_key"],
        }
        if "project_id" in metadata:
            sa_key["project_id"] = metadata["project_id"]
        sa_key["token_uri"] = metadata.get(
            "token_uri", "https://oauth2.googleapis.com/token"
        )
        config["service_account_key"] = json.dumps(sa_key)
    elif "service_account_key" in metadata:
        config["service_account_key"] = metadata["service_account_key"]

    return bucket, config


def _parse_azure_config(metadata: dict[str, str]) -> tuple[str, dict[str, Any]]:
    """Parse Azure binding metadata into obstore config."""
    container = metadata.get("containerName", "")
    if not container:
        raise StorageConfigError("Azure binding missing required 'containerName' field")

    config: dict[str, Any] = {"container_name": container}
    if metadata.get("accountName"):
        config["account_name"] = metadata["accountName"]
    if metadata.get("accountKey"):
        config["account_key"] = metadata["accountKey"]
    if metadata.get("endpoint"):
        config["endpoint"] = metadata["endpoint"]

    return container, config


def _parse_local_config(metadata: dict[str, str]) -> tuple[str, dict[str, Any]]:
    """Parse local binding metadata into obstore config."""
    root_path = metadata.get("rootPath", metadata.get("root", "."))
    return root_path, {}


# =============================================================================
# DAPR Binding Parsing (YAML)
# =============================================================================


def parse_binding_dict(spec: dict[str, Any], name: str = "") -> StorageBindingConfig:
    """Parse DAPR binding spec dict into storage config."""
    binding_type = spec.get("type", "")
    if binding_type not in BINDING_TYPE_MAP:
        raise StorageConfigError(
            f"Unsupported binding type: {binding_type}. "
            f"Supported types: {list(BINDING_TYPE_MAP.keys())}",
            binding_name=name,
        )

    provider = BINDING_TYPE_MAP[binding_type]
    metadata_list = spec.get("metadata", [])
    metadata = _metadata_to_dict(metadata_list)

    parsers = {
        "s3": _parse_s3_config,
        "gcs": _parse_gcs_config,
        "azure": _parse_azure_config,
        "local": _parse_local_config,
    }

    parser = parsers.get(provider)
    if not parser:
        raise StorageConfigError(f"Unknown provider: {provider}", binding_name=name)

    bucket, config = parser(metadata)
    return StorageBindingConfig(
        name=name, provider=provider, bucket=bucket, config=config
    )


def parse_binding_yaml(path: Path) -> StorageBindingConfig:
    """Parse a DAPR binding YAML file into storage config."""
    try:
        import yaml
    except ImportError as e:
        raise StorageConfigError(
            "PyYAML is required for parsing DAPR binding files. "
            "Install with: pip install pyyaml",
            config_file=str(path),
            cause=e,
        ) from e

    try:
        content = path.read_text()
        doc = yaml.safe_load(content)
    except FileNotFoundError as e:
        raise StorageConfigError(
            f"Binding file not found: {path}",
            config_file=str(path),
            cause=e,
        ) from e
    except yaml.YAMLError as e:
        raise StorageConfigError(
            f"Invalid YAML in binding file: {path}",
            config_file=str(path),
            cause=e,
        ) from e

    if not isinstance(doc, dict):
        raise StorageConfigError(
            f"Expected YAML dict, got {type(doc).__name__}",
            config_file=str(path),
        )

    metadata_section = doc.get("metadata", {})
    name = metadata_section.get("name", path.stem)
    spec = doc.get("spec", {})
    if not spec:
        raise StorageConfigError(
            "Binding YAML missing 'spec' section",
            config_file=str(path),
            binding_name=name,
        )

    try:
        return parse_binding_dict(spec, name=name)
    except StorageConfigError:
        raise
    except Exception as e:
        raise StorageConfigError(
            f"Failed to parse binding spec: {e}",
            config_file=str(path),
            binding_name=name,
            cause=e,
        ) from e


def load_binding_from_directory(
    components_dir: Path,
    binding_name: str,
) -> StorageBindingConfig:
    """Load a named binding from a DAPR components directory."""
    if not components_dir.exists():
        raise StorageConfigError(
            f"Components directory not found: {components_dir}",
            binding_name=binding_name,
        )

    # Direct file lookup
    for ext in ("yaml", "yml"):
        yaml_path = components_dir / f"{binding_name}.{ext}"
        if yaml_path.exists():
            return parse_binding_yaml(yaml_path)

    # Search all YAML files for matching metadata.name
    try:
        import yaml
    except ImportError as e:
        raise StorageConfigError(
            "PyYAML is required for loading DAPR bindings. "
            "Install with: pip install pyyaml",
            binding_name=binding_name,
            cause=e,
        ) from e

    for pattern in ("*.yaml", "*.yml"):
        for yaml_path in components_dir.glob(pattern):
            try:
                content = yaml_path.read_text()
                doc = yaml.safe_load(content)
                if isinstance(doc, dict):
                    metadata = doc.get("metadata", {})
                    if metadata.get("name") == binding_name:
                        return parse_binding_yaml(yaml_path)
            except Exception:
                logger.warning(
                    "Failed to parse DAPR component file: %s", yaml_path, exc_info=True
                )

    raise StorageConfigError(
        f"Binding '{binding_name}' not found in {components_dir}",
        binding_name=binding_name,
    )


# =============================================================================
# Env Var Auto-Detection (fallback when DAPR YAML not available)
# =============================================================================


def resolve_config_from_env(store_name: str) -> StorageBindingConfig | None:
    """Try to build a StorageBindingConfig from environment variables.

    Detection order:
    1. ATLAN_OBJECT_STORE_BACKEND=local → LocalStore
    2. AZURE_STORAGE_ACCOUNT + AZURE_STORAGE_ACCESS_KEY → AzureStore
    3. HMAC_ACCESS_KEY + HMAC_SECRET → S3Store (GCS via HMAC)
    4. Default → S3Store (AWS default credential chain)

    Returns None if no env vars suggest a valid config.
    """
    backend = os.getenv("ATLAN_OBJECT_STORE_BACKEND", "").lower()
    bucket = os.getenv("ATLAN_OBJECT_STORE_BUCKET", "")

    if backend == "local":
        root = os.getenv("ATLAN_OBJECT_STORE_LOCAL_ROOT", "./local/dapr/objectstore")
        return StorageBindingConfig(
            name=store_name,
            provider="local",
            bucket=root,
            config={},
        )

    azure_account = os.getenv("AZURE_STORAGE_ACCOUNT", "")
    azure_key = os.getenv("AZURE_STORAGE_ACCESS_KEY", "")
    if azure_account and azure_key:
        container = bucket or "default"
        return StorageBindingConfig(
            name=store_name,
            provider="azure",
            bucket=container,
            config={
                "account_name": azure_account,
                "account_key": azure_key,
                "container_name": container,
            },
        )

    hmac_key = os.getenv("HMAC_ACCESS_KEY", "")
    hmac_secret = os.getenv("HMAC_SECRET", "")
    if hmac_key and hmac_secret:
        endpoint = os.getenv("GCS_ENDPOINT_URL", "https://storage.googleapis.com")
        return StorageBindingConfig(
            name=store_name,
            provider="s3",
            bucket=bucket or "default",
            config={
                "access_key_id": hmac_key,
                "secret_access_key": hmac_secret,
                "endpoint": endpoint,
                "virtual_hosted_style_request": False,
            },
        )

    # Default S3 with AWS credential chain
    s3_config: dict[str, Any] = {}
    s3_endpoint = os.getenv("S3_ENDPOINT_URL", "") or os.getenv("AWS_ENDPOINT_URL", "")
    if s3_endpoint:
        s3_config["endpoint"] = s3_endpoint
        s3_config["virtual_hosted_style_request"] = False

    region = os.getenv("AWS_DEFAULT_REGION", "") or os.getenv("AWS_REGION", "")
    if region:
        s3_config["region"] = region

    access_key = os.getenv("AWS_ACCESS_KEY_ID", "")
    secret_key = os.getenv("AWS_SECRET_ACCESS_KEY", "")
    if access_key and secret_key:
        s3_config["access_key_id"] = access_key
        s3_config["secret_access_key"] = secret_key

    return StorageBindingConfig(
        name=store_name,
        provider="s3",
        bucket=bucket or "default",
        config=s3_config,
    )


# =============================================================================
# Store Factory
# =============================================================================


def create_store(config: StorageBindingConfig) -> Any:
    """Create an obstore instance from binding config."""
    try:
        from obstore.store import AzureStore, GCSStore, LocalStore, S3Store
    except ImportError as e:
        raise StorageConfigError(
            "obstore is required for storage operations. "
            "Install with: pip install 'obstore>=0.5.0'",
            binding_name=config.name,
            cause=e,
        ) from e

    factories = {
        "s3": lambda: S3Store(bucket=config.bucket, **config.config),
        "gcs": lambda: GCSStore(bucket=config.bucket, **config.config),
        "azure": lambda: AzureStore(**config.config),
        "local": lambda: LocalStore(prefix=config.bucket),
    }

    factory = factories.get(config.provider)
    if not factory:
        raise StorageConfigError(
            f"Unknown storage provider: {config.provider}",
            binding_name=config.name,
        )

    try:
        return factory()
    except StorageConfigError:
        raise
    except Exception as e:
        raise StorageConfigError(
            f"Failed to create {config.provider} store: {e}",
            binding_name=config.name,
            cause=e,
        ) from e


def resolve_store_config(store_name: str) -> StorageBindingConfig:
    """Resolve storage config for a given store_name.

    Tries DAPR YAML first, falls back to env var auto-detection.
    """
    # Try DAPR YAML first
    env_path = os.environ.get("DAPR_COMPONENTS_PATH")
    components_dir = Path(env_path) if env_path else Path("./components")

    if components_dir.exists():
        try:
            return load_binding_from_directory(components_dir, store_name)
        except StorageConfigError:
            logger.debug(
                "DAPR binding '%s' not found in %s, falling back to env vars",
                store_name,
                components_dir,
            )

    # Fallback to env var auto-detection
    config = resolve_config_from_env(store_name)
    if config is not None:
        return config

    raise StorageConfigError(
        f"Cannot resolve storage config for '{store_name}'. "
        "Set DAPR component YAML or environment variables.",
        binding_name=store_name,
    )
