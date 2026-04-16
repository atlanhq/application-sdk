"""Cloud store for accessing external customer-provided object stores.

Provides a high-level async API for downloading/uploading/listing files
from external S3, GCS, or Azure buckets using customer-provided credentials.

This is distinct from the tenant's own Dapr-configured store (``storage.ops``).
Use ``CloudStore`` when an app needs to access a customer's cloud bucket
using credentials they provide (e.g., cloud-sourced spec files, data imports).

Usage::

    from application_sdk.storage.cloud import CloudStore

    store = CloudStore.from_credentials({
        "authType": "s3",
        "username": "AKIAIOSFODNN7EXAMPLE",
        "password": "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
        "extra": {"s3_bucket": "customer-bucket", "region": "us-east-1"},
    })

    files = await store.download(prefix="specs/", output_dir="/tmp/specs")
    await store.upload(local_path="/tmp/result.json", key="output/result.json")
    keys = await store.list(prefix="data/")
    data = await store.get_bytes(key="config.json")

Credential format (standard ``csa-connectors-objectstore``)::

    S3:   authType="s3",   username=access_key, password=secret_key,
          extra={s3_bucket, region, aws_role_arn?}
    GCS:  authType="gcs",  username=project_id,  password=service_account_json,
          extra={gcs_bucket}
    ADLS: authType="adls", username=client_id,   password=client_secret,
          extra={storage_account_name, adls_container, azure_tenant_id}
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Any

import obstore as obs
from obstore.store import ObjectStore

# stdlib logger: cannot use get_logger here due to circular import
# (observability -> storage -> cloud -> observability)
logger = logging.getLogger(__name__)


class CloudStore:
    """Async client for external customer-provided cloud object stores.

    Create via the :meth:`from_credentials` factory method.
    """

    def __init__(self, store: ObjectStore, *, provider: str = "unknown") -> None:
        self._store = store
        self._provider = provider

    @property
    def provider(self) -> str:
        """Cloud provider name (``s3``, ``gcs``, ``adls``)."""
        return self._provider

    @property
    def store(self) -> ObjectStore:
        """Underlying obstore instance (for advanced use)."""
        return self._store

    # ------------------------------------------------------------------
    # Factory
    # ------------------------------------------------------------------

    @classmethod
    def from_credentials(cls, credentials: dict[str, Any]) -> CloudStore:
        """Create a CloudStore from a credential dict.

        Supports S3, GCS, and Azure (ADLS) with auto-detection of
        ``authType`` from credential fields when not explicitly set.

        Args:
            credentials: Credential dict with ``authType``, ``username``,
                ``password``, and ``extra`` fields.

        Returns:
            Configured CloudStore instance.

        Raises:
            ValueError: If auth type cannot be determined or required
                fields are missing.
        """
        extra = credentials.get("extra") or credentials.get("extras") or {}
        if isinstance(extra, str):
            extra = json.loads(extra) if extra else {}

        auth_type = (
            credentials.get("authType")
            or credentials.get("auth_type")
            or _infer_auth_type(extra)
        )

        if auth_type == "s3":
            store = _create_s3_store(credentials, extra)
        elif auth_type == "gcs":
            store = _create_gcs_store(credentials, extra)
        elif auth_type == "adls":
            store = _create_azure_store(credentials, extra)
        else:
            raise ValueError(
                f"Cannot determine cloud provider from credentials. "
                f"Set 'authType' to 's3', 'gcs', or 'adls'. Got: {auth_type!r}"
            )

        logger.info("Created CloudStore provider=%s", auth_type)
        return cls(store, provider=auth_type)

    # ------------------------------------------------------------------
    # Read operations
    # ------------------------------------------------------------------

    async def get_bytes(self, key: str) -> bytes:
        """Download a single object and return its contents as bytes.

        Args:
            key: Object key to download.

        Returns:
            Raw bytes of the object.

        Raises:
            FileNotFoundError: If the key does not exist.
        """
        try:
            result = await obs.get_async(self._store, key)
            return await result.bytes_async()
        except Exception as exc:
            if "not found" in str(exc).lower() or "404" in str(exc):
                raise FileNotFoundError(f"Key not found: {key}") from exc
            raise

    async def download(
        self,
        key: str = "",
        output_dir: str | Path = ".",
        *,
        prefix: str = "",
        suffix_filter: set[str] | None = None,
        max_concurrency: int = 4,
    ) -> list[Path]:
        """Download file(s) from the cloud store to a local directory.

        If ``key`` is provided, downloads a single file.
        If only ``prefix`` is provided, lists and downloads all matching files.

        Args:
            key: Specific object key to download (single file mode).
            output_dir: Local directory to write files into.
            prefix: Object key prefix for listing (multi-file mode).
            suffix_filter: Only download files with these extensions
                (e.g. ``{".json", ".yaml"}``). Ignored in single-file mode.
            max_concurrency: Maximum parallel downloads (default 4).

        Returns:
            List of local file paths that were downloaded.

        Raises:
            ValueError: If no files found under the prefix.
        """

        output = Path(output_dir)
        output.mkdir(parents=True, exist_ok=True)

        if key:
            return await self._download_single(key, output)

        return await self._download_prefix(
            prefix, output, suffix_filter, max_concurrency
        )

    async def _download_single(self, key: str, output: Path) -> list[Path]:
        """Download a single file."""
        local_path = output / Path(key).name
        local_path.parent.mkdir(parents=True, exist_ok=True)

        data = await self.get_bytes(key)
        local_path.write_bytes(data)
        logger.info("Downloaded key=%s size=%d local=%s", key, len(data), local_path)
        return [local_path]

    async def _download_prefix(
        self,
        prefix: str,
        output: Path,
        suffix_filter: set[str] | None,
        max_concurrency: int,
    ) -> list[Path]:
        """Download all files under a prefix."""

        list_prefix = f"{prefix.strip('/')}/" if prefix else ""
        logger.info("Listing objects under prefix=%s", list_prefix)

        keys: list[str] = []
        for batch in obs.list(self._store, prefix=list_prefix or None):
            for item in batch:
                obj_path = str(item["path"])
                if suffix_filter:
                    ext = Path(obj_path).suffix.lower()
                    if ext not in suffix_filter:
                        continue
                keys.append(obj_path)

        if not keys:
            raise ValueError(
                f"No files found under prefix: {list_prefix!r}"
                + (f" (filter: {suffix_filter})" if suffix_filter else "")
            )

        sem = asyncio.Semaphore(max_concurrency)
        downloaded: list[Path] = []

        async def _dl(obj_key: str) -> None:
            async with sem:
                rel = (
                    obj_key[len(list_prefix) :]
                    if list_prefix and obj_key.startswith(list_prefix)
                    else Path(obj_key).name
                )
                local_path = output / rel
                local_path.parent.mkdir(parents=True, exist_ok=True)
                data = await self.get_bytes(obj_key)
                local_path.write_bytes(data)
                downloaded.append(local_path)

        await asyncio.gather(*[_dl(k) for k in keys])
        logger.info("Downloaded %d files from prefix=%s", len(downloaded), list_prefix)
        return downloaded

    async def list(self, prefix: str = "", *, suffix: str = "") -> list[str]:
        """List object keys under a prefix.

        Args:
            prefix: Key prefix to filter by.
            suffix: Optional extension filter (e.g. ``".json"``).

        Returns:
            Sorted list of matching object keys.
        """

        list_prefix = f"{prefix.strip('/')}/" if prefix else ""

        def _collect() -> list[str]:
            keys: list[str] = []
            for batch in obs.list(self._store, prefix=list_prefix or None):
                for item in batch:
                    key = str(item["path"])
                    if not suffix or key.endswith(suffix):
                        keys.append(key)
            return sorted(keys)

        return await asyncio.to_thread(_collect)

    # ------------------------------------------------------------------
    # Write operations
    # ------------------------------------------------------------------

    async def upload(
        self,
        local_path: str | Path,
        key: str,
    ) -> int:
        """Upload a local file to the cloud store.

        Args:
            local_path: Path to the local file.
            key: Destination object key.

        Returns:
            Number of bytes uploaded.
        """
        path = Path(local_path)
        data = path.read_bytes()
        await obs.put_async(self._store, key, data)
        logger.info("Uploaded key=%s size=%d", key, len(data))
        return len(data)

    async def upload_bytes(self, key: str, data: bytes) -> int:
        """Upload raw bytes to the cloud store.

        Args:
            key: Destination object key.
            data: Bytes to upload.

        Returns:
            Number of bytes uploaded.
        """
        await obs.put_async(self._store, key, data)
        return len(data)

    async def upload_dir(
        self,
        local_dir: str | Path,
        prefix: str = "",
        *,
        max_concurrency: int = 4,
    ) -> list[str]:
        """Upload all files in a local directory to the cloud store.

        Args:
            local_dir: Local directory to upload from.
            prefix: Destination key prefix.
            max_concurrency: Maximum parallel uploads (default 4).

        Returns:
            List of uploaded object keys.
        """

        local = Path(local_dir)
        files: list[tuple[str, Path]] = []
        for root, _dirs, filenames in os.walk(local, followlinks=False):
            for fname in filenames:
                file_path = Path(root) / fname
                if file_path.is_symlink():
                    continue
                rel = file_path.relative_to(local)
                key = f"{prefix}/{rel}" if prefix else str(rel)
                files.append((key, file_path))

        sem = asyncio.Semaphore(max_concurrency)
        uploaded: list[str] = []

        async def _up(key: str, path: Path) -> None:
            async with sem:
                await self.upload(path, key)
                uploaded.append(key)

        await asyncio.gather(*[_up(k, p) for k, p in files])
        return uploaded


# ---------------------------------------------------------------------------
# Store creation helpers
# ---------------------------------------------------------------------------


def _infer_auth_type(extra: dict[str, Any]) -> str:
    """Infer cloud provider from extra fields."""
    if extra.get("s3_bucket"):
        return "s3"
    if extra.get("gcs_bucket"):
        return "gcs"
    if extra.get("adls_container") or extra.get("storage_account_name"):
        return "adls"
    return ""


def _create_s3_store(creds: dict[str, Any], extra: dict[str, Any]) -> ObjectStore:
    """Create an S3 store from credentials."""
    from obstore.store import S3Store

    bucket = extra.get("s3_bucket", "")
    if not bucket:
        raise ValueError("S3 bucket is required (extra.s3_bucket)")

    config: dict[str, str] = {}
    region = extra.get("region", "")
    if region:
        config["aws_region"] = region

    access_key = creds.get("username") or ""
    secret_key = creds.get("password") or ""
    if access_key and secret_key:
        config["aws_access_key_id"] = access_key
        config["aws_secret_access_key"] = secret_key

    role_arn = extra.get("aws_role_arn", "")
    if role_arn:
        config["aws_role_arn"] = role_arn
        config["aws_role_session_name"] = "cloud-store-session"
        logger.info("S3 role-based auth role_arn=%s", role_arn)

    return S3Store(bucket=bucket, config=config)


def _create_gcs_store(creds: dict[str, Any], extra: dict[str, Any]) -> ObjectStore:
    """Create a GCS store from credentials."""
    from obstore.store import GCSStore

    bucket = extra.get("gcs_bucket", "")
    if not bucket:
        raise ValueError("GCS bucket is required (extra.gcs_bucket)")

    gcs_config: dict[str, str] = {}
    sa_json = creds.get("password") or ""
    if sa_json:
        gcs_config["google_service_account_key"] = sa_json

    return GCSStore(bucket=bucket, config=gcs_config if gcs_config else None)


def _create_azure_store(creds: dict[str, Any], extra: dict[str, Any]) -> ObjectStore:
    """Create an Azure (ADLS) store from credentials."""
    from obstore.store import AzureStore

    storage_account = extra.get("storage_account_name", "")
    container = extra.get("adls_container", "objectstore")
    if not storage_account:
        raise ValueError(
            "Azure storage account is required (extra.storage_account_name)"
        )

    az_config: dict[str, str] = {
        "azure_storage_account_name": storage_account,
    }

    access_key = creds.get("password") or ""
    client_id = creds.get("username") or ""
    tenant_id = extra.get("azure_tenant_id") or ""

    if access_key and not tenant_id:
        # Account key auth
        az_config["azure_storage_account_key"] = access_key
    elif tenant_id and client_id:
        # Service principal auth
        az_config["azure_storage_client_id"] = client_id
        az_config["azure_storage_tenant_id"] = tenant_id
        if access_key:
            az_config["azure_storage_client_secret"] = access_key

    return AzureStore(container_name=container, config=az_config)
