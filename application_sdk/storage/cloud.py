"""Cloud store for accessing external customer-provided object stores.

Provides a high-level async API for downloading/uploading/listing files
from external S3, GCS, or Azure buckets using customer-provided credentials.

This is distinct from the tenant's own Dapr-configured store (``storage.ops``).
Use ``CloudStore`` when an app needs to access a customer's cloud bucket
using credentials they provide (e.g., cloud-sourced spec files, data imports).

File transfers (``upload`` / ``download``) use obstore's streaming APIs so
arbitrarily large objects are transferred without materialising the full payload
in memory.  Network I/O is fully async; local disk reads and writes use
synchronous chunked I/O (~8–10 MiB per chunk), which is standard Python
async-library practice.

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
import os
from pathlib import Path
from typing import Any

import obstore as obs
from obstore.store import AzureStore, GCSStore, ObjectStore, S3Store

from application_sdk.storage.errors import (
    StorageConfigError,
    StorageError,
    StorageNotFoundError,
)
from application_sdk.storage.ops import _list_items, download_file, upload_file

# Lazy import: direct get_logger() at module load would create a circular
# dependency (observability -> storage -> cloud -> observability).
# Deferred to first log call so all modules finish loading first.
_logger = None


def _log():
    global _logger
    if _logger is None:
        from application_sdk.observability.logger_adaptor import (  # noqa: PLC0415 — deferred to break circular import (observability ↔ storage)
            get_logger,
        )

        _logger = get_logger(__name__)
    return _logger


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
            StorageConfigError: If auth type cannot be determined or required
                fields are missing.
        """
        extra = credentials.get("extra")
        if extra is None:
            extra = credentials.get("extras")
        if extra is None:
            extra = {}
        if isinstance(extra, str):
            try:
                extra = json.loads(extra) if extra else {}
            except json.JSONDecodeError as exc:
                raise StorageConfigError(
                    f"Invalid JSON in 'extra' field: {exc}"
                ) from exc

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
            raise StorageConfigError(
                f"Cannot determine cloud provider from credentials. "
                f"Set 'authType' to 's3', 'gcs', or 'adls'. Got: {auth_type!r}"
            )

        _log().debug("Created CloudStore provider=%s", auth_type)
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
            StorageNotFoundError: If the key does not exist.
        """
        try:
            result = await obs.get_async(self._store, key)
            return bytes(await result.bytes_async())
        except FileNotFoundError as exc:
            raise StorageNotFoundError(f"Key not found: {key}", key=key) from exc
        except Exception as exc:
            # obstore backends raise different exception types for not-found:
            # - LocalStore: FileNotFoundError (caught above)
            # - S3/GCS/Azure: obstore.exceptions.NotFoundError
            if type(exc).__name__ == "NotFoundError":
                raise StorageNotFoundError(f"Key not found: {key}", key=key) from exc
            raise StorageError(f"Failed to get key: {key}", cause=exc) from exc

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
            suffix_filter: Only download files whose key ends with one of
                these strings (e.g. ``{".json", ".yaml"}``).  Matches are
                case-insensitive and use ``endswith`` — handles multi-part
                extensions (e.g. ``".tar.gz"``) correctly.  Ignored in
                single-file mode.
            max_concurrency: Maximum parallel downloads (default 4).

        Returns:
            List of local file paths that were downloaded.

        Raises:
            StorageError: If no files found under the prefix.
        """
        if key and prefix:
            raise StorageConfigError("Provide either 'key' or 'prefix', not both.")

        output = Path(output_dir)
        output.mkdir(parents=True, exist_ok=True)

        if key:
            return await self._download_single(key, output)

        return await self._download_prefix(
            prefix, output, suffix_filter, max_concurrency
        )

    async def _download_single(self, key: str, output: Path) -> list[Path]:
        """Download a single file by streaming to disk without buffering."""
        local_path = output / Path(key).name
        await download_file(key, local_path, store=self._store, normalize=False)
        _log().info("Downloaded key=%s local=%s", key, local_path)
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
        _log().info("Listing objects under prefix=%s", list_prefix)

        keys = await self._list_keys(list_prefix, suffix_filter)

        if not keys:
            raise StorageError(
                f"No files found under prefix: {list_prefix!r}"
                + (f" (filter: {suffix_filter})" if suffix_filter else "")
            )

        resolved_output = output.resolve()
        sem = asyncio.Semaphore(max_concurrency)

        async def _dl(obj_key: str) -> Path:
            async with sem:
                rel = (
                    obj_key[len(list_prefix) :]
                    if list_prefix and obj_key.startswith(list_prefix)
                    else Path(obj_key).name
                )
                local_path = (output / rel).resolve()
                # Prevent path traversal from malicious remote keys
                if not local_path.is_relative_to(resolved_output):
                    raise StorageError(f"Path traversal detected in key: {obj_key!r}")
                await download_file(
                    obj_key, local_path, store=self._store, normalize=False
                )
                return local_path

        results = await asyncio.gather(*[_dl(k) for k in keys])
        downloaded = list(results)
        _log().info("Downloaded %d files from prefix=%s", len(downloaded), list_prefix)
        return downloaded

    async def _list_keys(
        self, list_prefix: str, suffix_filter: set[str] | None = None
    ) -> list[str]:
        # Lowercase once so endswith checks are case-insensitive (e.g. ".JSON" matches ".json").
        lfilter = {s.lower() for s in suffix_filter} if suffix_filter else None
        try:
            items = await _list_items(self._store, list_prefix or None)
            return sorted(
                path
                for path, _ in items
                if not lfilter or any(path.lower().endswith(s) for s in lfilter)
            )
        except Exception as exc:
            raise StorageError(
                f"Failed to list keys with prefix: {list_prefix!r}", cause=exc
            ) from exc

    async def list(self, prefix: str = "", *, suffix: str = "") -> list[str]:
        """List object keys under a prefix.

        Args:
            prefix: Key prefix to filter by.
            suffix: Optional extension or tail filter (e.g. ``".json"``).
                Matched case-insensitively against the full key using
                ``endswith`` — handles multi-part extensions
                (e.g. ``".tar.gz"``) correctly.

        Returns:
            Sorted list of matching object keys.  Zero-byte objects that act as
            GCS-style directory markers (i.e. they have at least one child key
            under them) are excluded; zero-byte files with no children are
            returned normally.  For raw access including markers, use the
            underlying :attr:`store` property directly.
        """
        list_prefix = f"{prefix.strip('/')}/" if prefix else ""
        suffix_filter = {suffix} if suffix else None
        return await self._list_keys(list_prefix, suffix_filter)

    # ------------------------------------------------------------------
    # Write operations
    # ------------------------------------------------------------------

    async def upload(
        self,
        local_path: str | Path,
        key: str,
    ) -> int:
        """Upload a local file to the cloud store by streaming without buffering.

        Args:
            local_path: Path to the local file.
            key: Destination object key.

        Returns:
            Number of bytes uploaded.
        """
        path = Path(local_path)
        try:
            size = path.stat().st_size
            await upload_file(
                key,
                path,
                store=self._store,
                normalize=False,
                retain_local_copy=True,
                compute_hash=False,
            )
        except StorageError:
            raise
        except Exception as exc:
            raise StorageError(f"Failed to upload key: {key}", cause=exc) from exc
        _log().info("Uploaded key=%s size=%d", key, size)
        return size

    async def upload_bytes(self, key: str, data: bytes) -> int:
        """Upload raw bytes to the cloud store.

        Args:
            key: Destination object key.
            data: Bytes to upload.

        Returns:
            Number of bytes uploaded.
        """
        try:
            await obs.put_async(self._store, key, data)
        except Exception as exc:
            raise StorageError(f"Failed to upload key: {key}", cause=exc) from exc
        return len(data)

    async def upload_dir(
        self,
        local_dir: str | Path,
        prefix: str = "",
        *,
        max_concurrency: int = 4,
    ) -> list[str]:
        """Upload all files in a local directory to the cloud store.

        Note: Unlike ``batch.upload_prefix``, this uploads to an *external*
        store without SHA-256 hashing or key normalization — those features
        are specific to the tenant's internal storage layer.

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

        async def _up(key: str, path: Path) -> str:
            async with sem:
                await self.upload(path, key)
                return key

        results = await asyncio.gather(*[_up(k, p) for k, p in files])
        return list(results)


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
    """Create an S3 store from credentials.

    BLDX-1155: customer-facing buckets traverse the public internet — exactly
    the path most likely to time out on large extracts.  Plumb the SDK
    defaults for ``client_options`` + ``retry_config`` so every CloudStore
    inherits the same 30-minute request budget as the in-tenant Dapr store.
    """
    from application_sdk.storage._obstore_config import (
        log_obstore_config,
        obstore_client_options,
        obstore_retry_config,
    )

    bucket = extra.get("s3_bucket", "")
    if not bucket:
        raise StorageConfigError("S3 bucket is required (extra.s3_bucket)")

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
        _log().debug("S3 role-based auth configured")

    client_options = obstore_client_options()
    retry_config = obstore_retry_config()
    log_obstore_config(
        "cloud-s3", client_options=client_options, retry_config=retry_config
    )
    return S3Store(
        bucket=bucket,
        config=config,
        client_options=client_options,
        retry_config=retry_config,
    )


def _create_gcs_store(creds: dict[str, Any], extra: dict[str, Any]) -> ObjectStore:
    """Create a GCS store from credentials.

    BLDX-1155: see :func:`_create_s3_store`; same plumbing applies.
    """
    from application_sdk.storage._obstore_config import (
        log_obstore_config,
        obstore_client_options,
        obstore_retry_config,
    )

    bucket = extra.get("gcs_bucket", "")
    if not bucket:
        raise StorageConfigError("GCS bucket is required (extra.gcs_bucket)")

    gcs_config: dict[str, str] = {}
    sa_json = creds.get("password") or ""
    if sa_json:
        gcs_config["google_service_account_key"] = sa_json

    client_options = obstore_client_options()
    retry_config = obstore_retry_config()
    log_obstore_config(
        "cloud-gcs", client_options=client_options, retry_config=retry_config
    )
    return GCSStore(
        bucket=bucket,
        config=gcs_config if gcs_config else None,
        client_options=client_options,
        retry_config=retry_config,
    )


def _create_azure_store(creds: dict[str, Any], extra: dict[str, Any]) -> ObjectStore:
    """Create an Azure (ADLS) store from credentials.

    BLDX-1155: see :func:`_create_s3_store`; same plumbing applies.
    """
    from application_sdk.storage._obstore_config import (
        log_obstore_config,
        obstore_client_options,
        obstore_retry_config,
    )

    storage_account = extra.get("storage_account_name", "")
    container = extra.get("adls_container", "objectstore")
    if not storage_account:
        raise StorageConfigError(
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

    client_options = obstore_client_options()
    retry_config = obstore_retry_config()
    log_obstore_config(
        "cloud-azure", client_options=client_options, retry_config=retry_config
    )
    return AzureStore(
        container_name=container,
        config=az_config,
        client_options=client_options,
        retry_config=retry_config,
    )
