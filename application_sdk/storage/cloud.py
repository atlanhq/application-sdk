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
import os
from pathlib import Path
from typing import Any

import obstore as obs
import orjson
from obstore.store import ObjectStore

from application_sdk.storage.errors import (
    StorageConfigError,
    StorageError,
    StorageNotFoundError,
)
from application_sdk.storage.ops import (
    _azure_container_not_found_message,
    _compute_part_size,
    _is_azure_container_not_found,
    _list_items,
    _safe_join_under,
    download_file_chunked,
)

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

    def __init__(
        self,
        store: ObjectStore,
        *,
        provider: str = "unknown",
        put_attributes: dict[str, str] | None = None,
    ) -> None:
        self._store = store
        self._provider = provider
        self._put_attributes: dict[str, str] | None = put_attributes

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
                extra = orjson.loads(extra) if extra else {}
            except orjson.JSONDecodeError as exc:
                raise StorageConfigError(
                    f"Invalid JSON in 'extra' field: {exc}"
                ) from exc

        auth_type = (
            credentials.get("authType")
            or credentials.get("auth_type")
            or _infer_auth_type(extra)
        )

        if auth_type == "s3":
            store, put_attrs = _create_s3_store(credentials, extra)
        elif auth_type == "gcs":
            store, put_attrs = _create_gcs_store(credentials, extra)
        elif auth_type == "adls":
            store, put_attrs = _create_azure_store(credentials, extra)
        else:
            raise StorageConfigError(
                f"Cannot determine cloud provider from credentials. "
                f"Set 'authType' to 's3', 'gcs', or 'adls'. Got: {auth_type!r}"
            )

        _log().debug("Created CloudStore provider=%s", auth_type)
        return cls(store, provider=auth_type, put_attributes=put_attrs)

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
        # conformance: ignore[E004] re-raise only; discriminates NotFoundError by name then wraps in StorageError/StorageNotFoundError
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
        """Download a single file, chunking large objects into parallel range GETs."""
        local_path = output / Path(key).name
        # Chunked path streams small objects in a single GET and fetches large
        # ones via bounded parallel range GETs (each with its own timeout /
        # retry budget) so a slow-egress GB-class file doesn't die on one long
        # request. No hash — external stores skip the integrity sidecar
        # protocol. (BLDX-1513)
        await download_file_chunked(key, local_path, store=self._store, normalize=False)
        _log().info("Downloaded key=%s local_path=%s", key, str(local_path))
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

        items = await self._list_keys_with_meta(list_prefix, suffix_filter)

        if not items:
            raise StorageError(
                f"No files found under prefix: {list_prefix!r}"
                + (f" (filter: {suffix_filter})" if suffix_filter else "")
            )

        sem = asyncio.Semaphore(max_concurrency)

        async def _dl(obj_key: str, size: int, etag: str | None) -> Path:
            async with sem:
                rel = (
                    obj_key[len(list_prefix) :]
                    if list_prefix and obj_key.startswith(list_prefix)
                    else Path(obj_key).name
                )
                # Reject keys whose resolved path escapes output (e.g. via ".." segments).
                local_path = _safe_join_under(output, rel)
                # Pass the listing's size + etag so large objects chunk (with
                # version-pinned range GETs) and small ones stream — no per-file
                # HEAD (metadata already known). (BLDX-1513 / BLDX-1523)
                await download_file_chunked(
                    obj_key,
                    local_path,
                    store=self._store,
                    normalize=False,
                    file_size=size,
                    etag=etag,
                )
                return local_path

        results = await asyncio.gather(*[_dl(k, s, e) for k, s, e in items])
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
                for path, _, _ in items
                if not lfilter or any(path.lower().endswith(s) for s in lfilter)
            )
        # conformance: ignore[E004] re-raise only; wraps obstore listing failure into StorageError
        except Exception as exc:
            raise StorageError(
                f"Failed to list keys with prefix: {list_prefix!r}", cause=exc
            ) from exc

    async def _list_keys_with_meta(
        self, list_prefix: str, suffix_filter: set[str] | None = None
    ) -> list[tuple[str, int, str | None]]:
        """Like :meth:`_list_keys` but return ``(key, size_bytes, e_tag)``
        tuples so a prefix download can decide per-file whether to chunk —
        and version-pin the range GETs — without a HEAD."""
        lfilter = {s.lower() for s in suffix_filter} if suffix_filter else None
        try:
            items = await _list_items(self._store, list_prefix or None)
            return sorted(
                (path, size, etag)
                for path, size, etag in items
                if not lfilter or any(path.lower().endswith(s) for s in lfilter)
            )
        # conformance: ignore[E004] re-raise only; wraps obstore listing failure into StorageError
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
            chunk = _compute_part_size(size, 8 * 1024 * 1024)
            async with obs.open_writer_async(
                self._store, key, buffer_size=chunk, attributes=self._put_attributes
            ) as writer:
                with path.open("rb") as fh:
                    while True:
                        buf = fh.read(chunk)
                        if not buf:
                            break
                        await writer.write(buf)
        except StorageError:
            raise
        # conformance: ignore[E004] re-raise only; checks azure container-not-found then wraps in StorageConfigError/StorageError
        except Exception as exc:
            if _is_azure_container_not_found(exc):
                raise StorageConfigError(
                    _azure_container_not_found_message(key)
                ) from exc
            raise StorageError(f"Failed to upload key: {key}", cause=exc) from exc
        _log().info("Uploaded key=%s bytes=%d", key, size)
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
            await obs.put_async(self._store, key, data, attributes=self._put_attributes)
        # conformance: ignore[E004] re-raise only; wraps obstore put failure into StorageError
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


def _create_s3_store(
    creds: dict[str, Any], extra: dict[str, Any]
) -> tuple[ObjectStore, dict[str, str] | None]:
    """Create an S3 store from credentials.

    Returns *(store, put_attributes)* where *put_attributes* is
    ``{"Storage-Class": value}`` when ``extra["storageClass"]`` is set,
    or ``None`` otherwise.
    """
    from application_sdk.storage._obstore_config import make_s3_store  # noqa: PLC0415

    bucket = extra.get("s3_bucket", "")
    if not bucket:
        raise StorageConfigError("S3 bucket is required (extra.s3_bucket)")

    config: dict[str, str] = {}
    region = extra.get("region", "")
    if region:
        config["aws_region"] = region

    access_key = creds.get("username") or ""
    secret_key = creds.get("password") or ""

    credential_provider = None
    role_arn = extra.get("aws_role_arn", "")
    if role_arn:
        # Cross-account / IRSA assume-role. obstore has no static role_arn
        # config key (setting one is a silent no-op that leaves the store on
        # the ambient identity), so wire an STS credential provider — the same
        # path binding.py uses. Base creds are optional: when omitted, the STS
        # call uses the pod's ambient chain (IRSA / instance profile) as the
        # caller identity. Partial base creds (only one of access/secret) are
        # dropped so we don't hand obstore a half-configured session.
        from application_sdk.storage._credential_providers import (  # noqa: PLC0415
            make_s3_assume_role_provider,
        )

        base_access_key = access_key or None
        base_secret_key = secret_key or None
        if bool(base_access_key) != bool(base_secret_key):
            _log().warning(
                "S3 assume-role: both username and password are required as base "
                "credentials; only one was set — dropping both and falling back to "
                "the ambient credential chain as the STS caller identity."
            )
            base_access_key = None
            base_secret_key = None
        credential_provider = make_s3_assume_role_provider(
            role_arn=role_arn,
            # Distinct from binding.py's "atlan-application-sdk" default so the
            # two S3 auth paths are distinguishable in CloudTrail AssumeRole logs
            # (and preserves CloudStore's historical session name).
            session_name=extra.get("aws_role_session_name") or "cloud-store-session",
            # No region: the bucket's region belongs on the S3 store's own
            # config (below) to avoid obstore's us-east-1 default/redirect —
            # it has nothing to do with which STS endpoint assumes the role,
            # and forcing it onto the STS session breaks AssumeRole for
            # opt-in AWS regions (e.g. me-central-1), where ambient/IRSA
            # credentials aren't valid against that region's STS endpoint.
            base_access_key=base_access_key,
            base_secret_key=base_secret_key,
            base_session_token=(creds.get("token") or None)
            if base_access_key
            else None,
        )
        _log().debug("S3 cross-account assume-role auth configured")
    elif access_key and secret_key:
        config["aws_access_key_id"] = access_key
        config["aws_secret_access_key"] = secret_key

    storage_class = (extra.get("storageClass") or "").strip()
    put_attrs: dict[str, str] | None = (
        {"Storage-Class": storage_class} if storage_class else None
    )

    return (
        make_s3_store(
            bucket,
            config or None,
            label="cloud-s3",
            credential_provider=credential_provider,
        ),
        put_attrs,
    )


def _create_gcs_store(
    creds: dict[str, Any], extra: dict[str, Any]
) -> tuple[ObjectStore, dict[str, str] | None]:
    """Create a GCS store from credentials."""
    from application_sdk.storage._obstore_config import make_gcs_store  # noqa: PLC0415

    bucket = extra.get("gcs_bucket", "")
    if not bucket:
        raise StorageConfigError("GCS bucket is required (extra.gcs_bucket)")

    gcs_config: dict[str, str] = {}
    sa_json = creds.get("password") or ""
    if sa_json:
        gcs_config["google_service_account_key"] = sa_json

    storage_class = (extra.get("storageClass") or "").strip()
    put_attrs: dict[str, str] | None = (
        {"X-Goog-Storage-Class": storage_class} if storage_class else None
    )

    return make_gcs_store(
        bucket, gcs_config if gcs_config else None, label="cloud-gcs"
    ), put_attrs


def _create_azure_store(
    creds: dict[str, Any], extra: dict[str, Any]
) -> tuple[ObjectStore, dict[str, str] | None]:
    """Create an Azure (ADLS) store from credentials."""
    from application_sdk.storage._obstore_config import (  # noqa: PLC0415
        make_azure_store,
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

    storage_class = (extra.get("storageClass") or "").strip()
    put_attrs: dict[str, str] | None = (
        {"x-ms-access-tier": storage_class} if storage_class else None
    )

    return make_azure_store(container, az_config, label="cloud-azure"), put_attrs
