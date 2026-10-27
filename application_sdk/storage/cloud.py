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
          extra={s3_bucket, region?, aws_role_arn?, aws_external_id?}
          (a blank region is looked up from the bucket before the first call)
    GCS:  authType="gcs",  username=project_id,  password=service_account_json,
          extra={gcs_bucket}
    ADLS: authType="adls", username=client_id,   password=client_secret,
          extra={storage_account_name, adls_container, azure_tenant_id}
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import quote

import aiohttp
import obstore as obs
import orjson
from obstore.exceptions import PermissionDeniedError, UnauthenticatedError
from obstore.store import ObjectStore

from application_sdk._runtime.offload import run_in_thread
from application_sdk.common._listing import prune_internal_dirs
from application_sdk.storage.errors import (
    StorageAuthError,
    StorageConfigError,
    StorageError,
    StorageGatewayAuthUnavailableError,
    StorageNotFoundError,
    StoragePermissionError,
)
from application_sdk.storage.ops import (
    GATEWAY_INVALID_CLIENT_CODE,
    BoundStore,
    _list_items,
    _obstore_http_evidence,
    _safe_join_under,
    _store_target,
    download_file_chunked,
    upload_file,
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


# Statuses that mean the caller's credential or the bucket's policy refused the
# request. obstore maps some of these to a typed error and leaves the rest as
# text, so both are checked.
_DENIED_STATUSES = frozenset({401, 403})
_DENIED_ERROR_STATUS: tuple[tuple[type[Exception], int], ...] = (
    (PermissionDeniedError, 403),
    (UnauthenticatedError, 401),
)


def _denied_evidence(exc: BaseException) -> tuple[int, str | None] | None:
    """Return ``(http_status, provider_code)`` when *exc* is a 401/403.

    Walks explicit ``__cause__`` links so a ``StorageError`` the shared
    primitives already wrapped is judged by the driver error inside it.
    """
    current: BaseException | None = exc
    while current is not None:
        http_status, provider_code = _obstore_http_evidence(current)
        if http_status is None:
            http_status = next(
                (s for t, s in _DENIED_ERROR_STATUS if isinstance(current, t)), None
            )
        if http_status in _DENIED_STATUSES:
            return http_status, provider_code
        current = current.__cause__
    return None


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
        self._region_lookup: tuple[str, Callable[[str], ObjectStore]] | None = None
        self._region_lock = asyncio.Lock()

    @property
    def provider(self) -> str:
        """Cloud provider name (``s3``, ``gcs``, ``adls``)."""
        return self._provider

    @property
    def store(self) -> ObjectStore:
        """Underlying obstore instance (for advanced use).

        For S3 with a blank region, this is the store as built until the
        first async call resolves the bucket's region.
        """
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

        region_lookup: tuple[str, Callable[[str], ObjectStore]] | None = None
        if auth_type == "s3":
            store, put_attrs = _create_s3_store(credentials, extra)
            if _needs_region_lookup(extra):
                creds = {
                    k: credentials[k]
                    for k in ("username", "password", "token")
                    if k in credentials
                }
                base_extra = dict(extra)
                region_lookup = (
                    base_extra["s3_bucket"],
                    lambda region: _create_s3_store(
                        creds, {**base_extra, "region": region}
                    )[0],
                )
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
        cloud_store = cls(store, provider=auth_type, put_attributes=put_attrs)
        cloud_store._region_lookup = region_lookup
        return cloud_store

    async def _ensure_region(self) -> None:
        """Rebuild an S3 store that has no region with the bucket's own region.

        Runs once, before the first request. obstore does not follow S3
        region redirects, so a store without a region fails on a bucket in
        any other region. A failed lookup keeps the store as built.
        """
        if self._region_lookup is None:
            return
        async with self._region_lock:
            if self._region_lookup is None:
                return
            bucket, rebuild = self._region_lookup
            region = await _lookup_bucket_region(bucket)
            if region:
                self._store = rebuild(region)
                _log().info(
                    "Resolved S3 bucket region bucket=%s region=%s", bucket, region
                )
            self._region_lookup = None

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
        await self._ensure_region()
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
            self._raise_if_denied(exc, "reading", key)
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
        await self._ensure_region()

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
        # protocol, so sidecar_present=False saves a guaranteed-miss probe per
        # file; the byte-count check against the declared size still runs.
        # (BLDX-1513 / FND-306)
        try:
            await download_file_chunked(
                key,
                local_path,
                store=self._store,
                normalize=False,
                sidecar_present=False,
            )
        except StorageError as exc:
            self._raise_if_denied(exc, "reading", key)
            raise
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
                # No `list_prefix and` guard here: when list_prefix == "" (a
                # whole-bucket download), startswith("") is always True, so the
                # full key is kept instead of falling through to the basename-only
                # branch below, which would clobber same-named files from
                # different subdirectories onto one path.
                rel = (
                    obj_key[len(list_prefix) :]
                    if obj_key.startswith(list_prefix)
                    else Path(obj_key).name
                )
                # Reject keys whose resolved path escapes output (e.g. via ".." segments).
                local_path = _safe_join_under(output, rel)
                # Pass the listing's size + etag so large objects chunk (with
                # version-pinned range GETs) and small ones stream — no per-file
                # HEAD (metadata already known). (BLDX-1513 / BLDX-1523)
                try:
                    await download_file_chunked(
                        obj_key,
                        local_path,
                        store=self._store,
                        normalize=False,
                        file_size=size,
                        etag=etag,
                        # External bucket: the SDK never wrote sidecars here, so
                        # skip the per-file probe that could only ever miss.
                        sidecar_present=False,
                    )
                except StorageError as exc:
                    self._raise_if_denied(exc, "reading", obj_key)
                    raise
                return local_path

        results = await asyncio.gather(*[_dl(k, s, e) for k, s, e in items])
        downloaded = list(results)
        _log().info("Downloaded %d files from prefix=%s", len(downloaded), list_prefix)
        return downloaded

    def _raise_if_denied(self, exc: Exception, action: str, key: str) -> None:
        """Raise a typed USER error from *exc* when it is a 401/403.

        A 401/403 is a credential or bucket-policy problem (IAM, a VPC Service
        Controls perimeter) that no retry fixes. Every store this class talks
        to belongs to the customer, so it is a non-retryable USER error rather
        than the retryable platform outage the shared primitives report (they
        cannot tell a customer bucket from the deployment's own store).

        The two statuses get different leaves because they have different
        fixes: a 401 is the credential itself being rejected
        (:class:`StorageAuthError`, category AUTH) — for Azure that includes a
        failed token request against the identity provider — while a 403 is a
        valid credential without access (:class:`StoragePermissionError`).
        """
        # Already typed. The gateway leaf is a 401 too, but it is the
        # blobstorage proxy failing its own identity lookup — a retryable
        # platform condition — and relabelling it here would tell the customer
        # to fix a credential that is fine.
        if isinstance(exc, StoragePermissionError | StorageGatewayAuthUnavailableError):
            return
        denied = _denied_evidence(exc)
        if denied is None:
            return
        http_status, provider_code = denied
        # The same gateway verdict, still as raw driver text on the paths that
        # call obstore directly rather than through the shared primitives.
        if http_status == 401 and provider_code == GATEWAY_INVALID_CLIENT_CODE:
            return
        target = _store_target(self._store, key)
        detail = f"{http_status} {provider_code}" if provider_code else str(http_status)
        if http_status == 401:
            raise StorageAuthError(
                f"Authentication failed {action} {target or key} ({detail}): the "
                "store rejected the credential. Check that the credential's "
                "secret, key or token is current and, for an Azure client "
                "secret, that it is the secret value rather than the secret ID.",
                key=key,
                cause=exc,
                target=target,
                http_status=http_status,
                provider_code=provider_code,
            ) from exc
        raise StoragePermissionError(
            f"Permission denied {action} {target or key} ({detail}). Check the "
            "credential's IAM access and any VPC Service Controls perimeter "
            "on the bucket.",
            key=key,
            cause=exc,
            target=target,
            http_status=http_status,
            provider_code=provider_code,
        ) from exc

    async def _list_keys(
        self, list_prefix: str, suffix_filter: set[str] | None = None
    ) -> list[str]:
        # Lowercase once so endswith checks are case-insensitive (e.g. ".JSON" matches ".json").
        lfilter = {s.lower() for s in suffix_filter} if suffix_filter else None
        try:
            items = await _list_items(
                self._store, list_prefix or None, suffixes=suffix_filter or ()
            )
            return sorted(
                path
                for path, _, _ in items
                if not lfilter or any(path.lower().endswith(s) for s in lfilter)
            )
        # conformance: ignore[E004] re-raise only; wraps obstore listing failure into StorageError
        except Exception as exc:
            self._raise_if_denied(exc, "listing", list_prefix)
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
            items = await _list_items(
                self._store, list_prefix or None, suffixes=suffix_filter or ()
            )
            return sorted(
                (path, size, etag)
                for path, size, etag in items
                if not lfilter or any(path.lower().endswith(s) for s in lfilter)
            )
        # conformance: ignore[E004] re-raise only; wraps obstore listing failure into StorageError
        except Exception as exc:
            self._raise_if_denied(exc, "listing", list_prefix)
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
            under them) are excluded, and so are S3 ``folder/`` markers with
            no children; zero-byte files with no children are returned
            normally.  For raw access including markers, use the
            underlying :attr:`store` property directly.
        """
        await self._ensure_region()
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

        Routes through the shared :func:`~application_sdk.storage.ops.upload_file`
        primitive so the FND-306 write-side validations hold on external stores
        exactly as they do on the tenant store: a file truncated under the
        reader raises ``StorageIntegrityError`` rather than landing a partial
        artifact, and a post-write HEAD confirms the store recorded the bytes
        that were sent (a silently-dropped object becomes a ``StorageError``).
        The key is used exactly as supplied (no normalisation) and no
        ``.sha256`` sidecar is written — external customer buckets do not
        participate in the SDK sidecar protocol (mirrors the download path's
        ``sidecar_present=False``).

        Transport policy is the primitive's, not this class's: that includes the
        deployment part-size override (``ATLAN_STORAGE_UPLOAD_PART_SIZE_BYTES``,
        DISTR-899), which now reaches external buckets too. The default is
        unchanged at 8 MiB, so only a deployment that has explicitly tuned the
        part size sees a difference — and a deployment that had to tune it for
        its egress path almost certainly wants it applied uniformly.

        Args:
            local_path: Path to the local file.
            key: Destination object key.

        Returns:
            Size of the local file when the upload began. The truncation guard
            makes that the exact byte count in every case but one: a file being
            appended to concurrently streams to EOF, so *more* bytes may reach
            the store than are reported here. ``upload_file`` owns the true
            count and does not surface it; no caller needs the exact figure, so
            this does not spend a second HEAD to recover it.
        """
        await self._ensure_region()
        path = Path(local_path)
        try:
            size = path.stat().st_size
        # conformance: ignore[E004] re-raise only; a missing/unreadable source file is wrapped in StorageError, matching the pre-FND-306 contract
        except OSError as exc:
            raise StorageError(f"Failed to upload key: {key}", cause=exc) from exc
        # BoundStore carries the credential-level put attributes (e.g. an S3
        # storageClass) into the primitive; compute_hash=False skips hashing
        # and implies write_sidecar=False.
        #
        # The per-part ``mark_progress`` hook FND-288 added to this method's own
        # write loop is not lost with the loop: ``upload_file`` marks
        # ``storage.upload_part`` per part, so the stall watchdog still sees a
        # long external upload making progress. Only the label changed.
        try:
            await upload_file(
                key,
                path,
                BoundStore(self._store, self._put_attributes),
                normalize=False,
                compute_hash=False,
            )
        except StorageError as exc:
            self._raise_if_denied(exc, "writing", key)
            raise
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
        await self._ensure_region()
        try:
            await obs.put_async(self._store, key, data, attributes=self._put_attributes)
        # conformance: ignore[E004] re-raise only; wraps obstore put failure into StorageError
        except Exception as exc:
            self._raise_if_denied(exc, "writing", key)
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
        store without SHA-256 sidecars or key normalization — those features
        are specific to the tenant's internal storage layer.  The byte-level
        validations (truncation, post-write readback) still apply, via
        :meth:`upload`.

        Args:
            local_dir: Local directory to upload from.
            prefix: Destination key prefix.
            max_concurrency: Maximum parallel uploads (default 4).

        Returns:
            List of uploaded object keys.
        """
        await self._ensure_region()
        local = Path(local_dir)

        def _collect_files() -> list[tuple[str, Path]]:
            collected: list[tuple[str, Path]] = []
            for root, dirs, filenames in os.walk(local, followlinks=False):
                # SDK working directories (.sdk-partial staging, in-flight
                # download part files) must never ship to a customer bucket.
                prune_internal_dirs(dirs)
                for fname in filenames:
                    file_path = Path(root) / fname
                    if file_path.is_symlink():
                        continue
                    rel = file_path.relative_to(local)
                    key = f"{prefix}/{rel}" if prefix else str(rel)
                    collected.append((key, file_path))
            return collected

        # Offloaded: the walk is one stat per entry over the whole directory,
        # thousands of syscalls with no await between them on a large upload.
        # Inline it holds the event loop — and the enclosing activity's
        # auto-heartbeat — for the entire traversal (ADR-0010).
        files: list[tuple[str, Path]] = await run_in_thread(_collect_files)

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


_S3_REGION_LOOKUP_URL = "https://{host}/{bucket}"
_S3_REGION_LOOKUP_TIMEOUT_SECONDS = 10.0


def _needs_region_lookup(extra: dict[str, Any]) -> bool:
    """True when an S3 store has no region and talks to AWS S3 itself."""
    return not extra.get("region") and not (
        os.environ.get("AWS_ENDPOINT_URL") or os.environ.get("AWS_ENDPOINT")
    )


_S3_GLOBAL_LOOKUP_HOST = "s3.amazonaws.com"
_S3_CHINA_LOOKUP_HOST = "s3.cn-north-1.amazonaws.com.cn"


def _region_lookup_hosts() -> list[str]:
    """S3 endpoint hosts for the region lookup, in probe order.

    The ambient region's endpoint (else the global one) comes first, then the
    other partition's: a partition's endpoint cannot see the other partition's
    buckets, and a blank-region store has no region to pick the partition from.
    """
    region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")
    if not region:
        return [_S3_GLOBAL_LOOKUP_HOST, _S3_CHINA_LOOKUP_HOST]
    if region.startswith("cn-"):
        return [f"s3.{region}.amazonaws.com.cn", _S3_GLOBAL_LOOKUP_HOST]
    return [f"s3.{region}.amazonaws.com", _S3_CHINA_LOOKUP_HOST]


async def _lookup_bucket_region(bucket: str) -> str:
    """Return the bucket's region from ``x-amz-bucket-region``, or ``""``.

    An unsigned path-style HEAD on the bucket returns the header on 200, 301
    and 403, so no credentials are needed and dotted bucket names work. The
    pod's own regional endpoint is used when ``AWS_REGION`` is set, so a
    network that reaches only its own region still answers. A response without
    the header means the bucket is not in that partition, so the other
    partition is probed next; a failed request stops the lookup rather than
    spend a second timeout on an unreachable network.
    """
    timeout = aiohttp.ClientTimeout(total=_S3_REGION_LOOKUP_TIMEOUT_SECONDS)
    try:
        from application_sdk.clients.ssl_utils import get_ssl_context  # noqa: PLC0415

        async with aiohttp.ClientSession(timeout=timeout, trust_env=True) as session:
            for host in _region_lookup_hosts():
                url = _S3_REGION_LOOKUP_URL.format(
                    host=host, bucket=quote(bucket, safe="")
                )
                async with session.head(
                    url, allow_redirects=False, ssl=get_ssl_context()
                ) as response:
                    region = response.headers.get("x-amz-bucket-region", "")
                if region:
                    return region
            return ""
    # conformance: ignore[E004] best-effort lookup; any failure keeps the store as built and is logged
    except Exception as exc:
        _log().warning("S3 bucket region lookup failed bucket=%s error=%s", bucket, exc)
        return ""


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
            # No region: scoping the STS session to the bucket's region breaks
            # AssumeRole for opt-in regions (e.g. me-central-1); region belongs
            # on the S3 store's own config below, not the STS call.
            base_access_key=base_access_key,
            base_secret_key=base_secret_key,
            base_session_token=(creds.get("token") or None)
            if base_access_key
            else None,
            # Required when the role's trust policy has an sts:ExternalId
            # condition; the provider drops a blank one.
            external_id=extra.get("aws_external_id"),
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
