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
          token?=session_token (temporary STS credentials),
          extra={s3_bucket, region?, aws_role_arn?, aws_external_id?,
                 endpoint?, force_path_style?, disable_tagging?, conditional_put?,
                 copy_if_not_exists?, checksum_algorithm?,
                 server_side_encryption?, sse_kms_key_id?}
          (a blank region is looked up from the bucket before the first
          call, unless a non-AWS endpoint is set; a non-AWS endpoint
          disables object tagging unless disable_tagging says otherwise;
          the endpoint must be https:// — CloudStore always uses TLS with
          certificate verification)
    GCS:  authType="gcs",  username=project_id,  password=service_account_json,
          extra={gcs_bucket}
          (no password: Application Default Credentials, including a
          Workload Identity Federation file)
    ADLS: authType="adls", username=client_id,   password=client_secret,
          extra={storage_account_name, adls_container, azure_tenant_id,
                 endpoint?}

Every front-end that builds a store (this module, the Dapr binding, the API
host) translates its input into the settings dataclasses of
:mod:`application_sdk._obstore_config`, so the same credentials produce the
same store whichever path read them.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any, TypeVar
from urllib.parse import quote

import httpx
import orjson
from obstore.exceptions import (  # noqa: F401 — importable from here in past releases
    PermissionDeniedError,
    UnauthenticatedError,
)
from obstore.store import ObjectStore

from application_sdk import _deprecation
from application_sdk.storage._classify import (  # noqa: F401 — _DENIED_* importable from here in past releases
    _DENIED_ERROR_STATUS,
    _DENIED_STATUSES,
)
from application_sdk.storage._classify import denied_evidence as _denied_evidence
from application_sdk.storage.batch import DataObject, _download_listed, _upload_tree
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
    _get_bytes,
    _list_items,
    _store_target,
    download_file_chunked,
    upload_bytes,
    upload_file,
)

__all__ = [
    "CloudStore",
]


_T = TypeVar("_T")


#: name -> (replacement, why). Importable from here only as a side effect of
#: an import that is gone; served once more, with a warning naming their home.
_DEPRECATED_CONSTANTS: dict[str, tuple[str, str]] = {
    "prune_internal_dirs": (
        "application_sdk.common._listing.prune_internal_dirs",
        "it was only ever re-exported here by an import",
    ),
    "run_in_thread": (
        "application_sdk._runtime.offload.run_in_thread",
        "it was only ever re-exported here by an import",
    ),
}

__getattr__ = _deprecation.deprecated_module_getattr(
    __name__, _DEPRECATED_CONSTANTS, remove_in="4.0.0"
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
        # The shared primitive, so this read rides out the gateway's
        # "no Route matched" 404 and fails with the same typed leaves as
        # every other read; ``_mapping_denied`` then makes a 401/403 the
        # customer's. No sidecar: external buckets do not carry one.
        data = await self._mapping_denied(
            "reading", key, _get_bytes(key, self._store, normalize=False)
        )
        if data is None:
            raise StorageNotFoundError(
                f"Key not found: {key}",
                key=key,
                target=_store_target(self._store, key),
            )
        return data

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
            StorageError: If no files found under the prefix, or the first
                download that failed. In prefix mode the downloads still in
                flight are cancelled and have unwound before this raises, so
                nothing is written into *output_dir* afterwards; files already
                downloaded stay.
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

        # The listing is CloudStore's own (multi-suffix, and ``.sha256`` keys
        # are customer data here); the download is batch's engine, with every
        # object marked sidecar-less. Prefix stripped: the tree under the
        # prefix lands directly in *output*, and a whole-bucket download
        # (list_prefix "") keeps each full key rather than a basename that
        # same-named files in different subdirectories would clobber.
        objects = [
            DataObject(key=k, size=n, etag=e, has_sidecar=False) for k, n, e in items
        ]
        # The first failure cancels the remaining downloads and drains them
        # before it propagates, so nothing writes into *output* afterwards.
        downloaded = await self._mapping_denied(
            "reading",
            list_prefix,
            _download_listed(
                objects,
                list_prefix,
                output,
                self._store,
                suffix="",
                normalize=False,
                strip_prefix=True,
                max_concurrency=max_concurrency,
                sync=False,
                unwrap_lone_failure=True,
            ),
        )
        _log().info("Downloaded %d files from prefix=%s", len(downloaded), list_prefix)
        return [Path(p) for p in downloaded]

    async def _mapping_denied(
        self, action: str, key: str, op: Coroutine[Any, Any, _T]
    ) -> _T:
        """Await *op*, turning a 401/403 ``StorageError`` into a typed USER error.

        The key named is the failing object's when the error carries one, else
        *key*. A fan-out whose failures mix a ``StorageError`` with another
        type surfaces as an exception group (see ``_run_bounded``); each
        ``StorageError`` leaf in it is mapped the same way and the group is
        re-raised with the mapped leaves.
        """
        try:
            return await op
        except StorageError as exc:
            self._raise_if_denied(exc, action, exc.key or key)
            raise
        except BaseExceptionGroup as group:
            mapped = self._map_denied_leaves(group, action, key)
            if mapped is group:
                raise
            raise mapped from group

    def _map_denied_leaves(
        self, group: BaseExceptionGroup, action: str, key: str
    ) -> BaseExceptionGroup:
        """Return *group* with each 401/403 ``StorageError`` leaf typed.

        Returns *group* itself when no leaf changed.
        """
        leaves: list[BaseException] = []
        changed = False
        for leaf in group.exceptions:
            new: BaseException = leaf
            if isinstance(leaf, BaseExceptionGroup):
                new = self._map_denied_leaves(leaf, action, key)
            elif isinstance(leaf, StorageError):
                try:
                    self._raise_if_denied(leaf, action, leaf.key or key)
                except StorageError as typed:
                    new = typed
            changed = changed or new is not leaf
            leaves.append(new)
        if not changed:
            return group
        return group.derive(leaves)

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

    async def upload_bytes(
        self, key: str, data: bytes, *, verify: bool | None = None
    ) -> int:
        """Upload raw bytes to the cloud store.

        Routes through the shared
        :func:`~application_sdk.storage.ops.upload_bytes` primitive, so the
        post-write HEAD that :meth:`upload` runs also confirms the bytes landed
        here: a write the store accepted but did not keep, or kept at the wrong
        size, raises instead of surfacing later at a reader. The key is used
        exactly as supplied and no ``.sha256`` sidecar is written.

        The readback needs read-metadata (HEAD) access to *key* as well as
        PUT, as :meth:`upload` and :meth:`upload_dir` already do. Pass
        ``verify=False`` for a bucket whose policy grants PUT only.

        Args:
            key: Destination object key.
            data: Bytes to upload.
            verify: Run the post-write readback. ``None`` (default) follows
                ``ATLAN_STORAGE_VERIFY_TRANSFERS``, which is on unless the
                deployment turned it off.

        Returns:
            Number of bytes uploaded.

        Raises:
            StorageError: If the write fails, or the readback finds the object
                absent or the wrong size.
        """
        await self._ensure_region()
        await self._mapping_denied(
            "writing",
            key,
            upload_bytes(
                key,
                data,
                BoundStore(self._store, self._put_attributes),
                normalize=False,
                compute_hash=False,
                verify=verify,
            ),
        )
        return len(data)

    async def upload_dir(
        self,
        local_dir: str | Path,
        prefix: str = "",
        *,
        max_concurrency: int = 4,
    ) -> list[str]:
        """Upload all files in a local directory to the cloud store.

        Note: Runs on the same engine as ``batch.upload_prefix`` but without
        SHA-256 sidecars or key normalization — those features are specific
        to the tenant's internal storage layer.  The byte-level validations
        (truncation, post-write readback) still apply.  Symlinks and SDK
        working directories are skipped.

        Args:
            local_dir: Local directory to upload from.
            prefix: Destination key prefix.
            max_concurrency: Maximum parallel uploads (default 4).

        Returns:
            List of uploaded object keys.

        Raises:
            StorageError: The first upload that failed. The uploads still in
                flight are cancelled and have unwound before this raises, so
                no object lands in the bucket afterwards; those already
                finished stay. The set written is partial: retry the whole
                directory.
        """
        await self._ensure_region()

        async def _upload_one(key: str, path: Path) -> int:
            # Per file, so each failure is typed before it can group with a
            # sibling's: a 401/403 becomes a USER error, and a file that
            # vanished or became unreadable after the walk a keyed StorageError.
            return await self.upload(path, key)

        # batch's tree engine with the sidecar protocol off: no sidecars and no
        # key normalisation, and a local ``.sha256`` file uploads as data. The
        # first failure cancels and drains the remaining uploads before it
        # propagates, so none lands in the bucket after the caller saw it fail.
        uploaded = await _upload_tree(
            Path(local_dir),
            prefix,
            _upload_one,
            max_concurrency=max_concurrency,
            sidecars=False,
            unwrap_lone_failure=True,
        )
        _log().info("Uploaded %d files to prefix=%s", len(uploaded), prefix)
        return uploaded


# ---------------------------------------------------------------------------
# Store creation helpers
# ---------------------------------------------------------------------------


_S3_REGION_LOOKUP_URL = "https://{host}/{bucket}"
_S3_REGION_LOOKUP_TIMEOUT_SECONDS = 10.0


def _needs_region_lookup(extra: dict[str, Any]) -> bool:
    """True when an S3 store has no region and talks to AWS S3 itself.

    A non-AWS ``extra["endpoint"]`` (MinIO, R2, ...) has no bucket region to
    look up, so it gets no lookup; an AWS endpoint in ``extra["endpoint"]``
    still does, since S3 needs the bucket's own region to sign for it. The
    ``AWS_ENDPOINT_URL`` / ``AWS_ENDPOINT`` env vars skip the lookup as before.
    """
    from application_sdk._obstore_config import endpoint_is_aws  # noqa: PLC0415

    return not extra.get("region") and not (
        not endpoint_is_aws((extra.get("endpoint") or "").strip())
        or os.environ.get("AWS_ENDPOINT_URL")
        or os.environ.get("AWS_ENDPOINT")
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

    The proxy comes from :mod:`application_sdk._proxy`, the rule every SDK
    connection follows, resolved per lookup host so ``NO_PROXY`` applies.
    httpx's own ``trust_env`` handling is off: it also reads operating-system
    proxy settings, which no other SDK connection does. The TLS context is
    built here with ``trust_env=True``, so ``SSL_CERT_FILE`` and
    ``SSL_CERT_DIR`` are still trusted as httpx would trust them.
    """
    try:
        from application_sdk._proxy import (  # noqa: PLC0415
            environment_proxies,
            proxy_for,
        )
        from application_sdk.clients.ssl_utils import get_ssl_context  # noqa: PLC0415

        proxies = environment_proxies()
        # trust_env=False below would also drop httpx's SSL_CERT_FILE handling;
        # build the context as httpx would, so that CA bundle is still trusted.
        ssl_context = httpx.create_ssl_context(verify=get_ssl_context(), trust_env=True)
        for host in _region_lookup_hosts():
            url = _S3_REGION_LOOKUP_URL.format(host=host, bucket=quote(bucket, safe=""))
            async with httpx.AsyncClient(
                timeout=_S3_REGION_LOOKUP_TIMEOUT_SECONDS,
                trust_env=False,
                verify=ssl_context,
                proxy=proxy_for("https", host, proxies=proxies),
            ) as client:
                # httpx's scalar timeout bounds each read, not the request; a
                # peer dripping header bytes could hold it open, so cap the HEAD.
                response = await asyncio.wait_for(
                    client.head(url, follow_redirects=False),
                    timeout=_S3_REGION_LOOKUP_TIMEOUT_SECONDS,
                )
                region = response.headers.get("x-amz-bucket-region", "")
                if region:
                    return region
        return ""
    except Exception as exc:  # noqa: BLE001 — best-effort lookup; any failure keeps the store as built and is logged
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


def _extra_bool(extra: dict[str, Any], key: str) -> bool | None:
    """``extra[key]`` as a bool, or ``None`` when absent or blank.

    The credential form stores booleans as JSON booleans or as strings.
    """
    value = extra.get(key)
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"true", "1", "yes"}


def _extra_str(extra: dict[str, Any], key: str) -> str:
    """``extra[key]`` as a stripped string, ``""`` when absent or blank."""
    return str(extra.get(key) or "").strip()


def _tls_endpoint(extra: dict[str, Any]) -> str:
    """``extra["endpoint"]``, refusing a plain-HTTP one.

    CloudStore's credential is entered by a customer and reaches a bucket
    over networks the SDK does not control, so it always uses TLS with
    certificate verification: there is no ``allow_http`` or
    ``allow_invalid_certificates`` here, and an ``http://`` endpoint (which
    the shared translation would turn into plain HTTP) is a config error.
    The Dapr binding's ``disableSSL`` / ``insecureSSL`` are operator-set in
    the component YAML and stay as they are.
    """
    endpoint = _extra_str(extra, "endpoint")
    if endpoint.lower().startswith("http://"):
        raise StorageConfigError(
            "CloudStore requires an https:// endpoint (extra.endpoint); "
            "plain HTTP would send credentials and data unencrypted"
        )
    return endpoint


def _create_s3_store(
    creds: dict[str, Any], extra: dict[str, Any]
) -> tuple[ObjectStore, dict[str, str] | None]:
    """Create an S3 store from credentials.

    Returns *(store, put_attributes)* where *put_attributes* is
    ``{"Storage-Class": value}`` when ``extra["storageClass"]`` is set,
    or ``None`` otherwise.
    """
    from application_sdk._obstore_config import (  # noqa: PLC0415
        S3StoreSettings,
        build_s3_store,
    )

    bucket = extra.get("s3_bucket", "")
    if not bucket:
        raise StorageConfigError("S3 bucket is required (extra.s3_bucket)")

    access_key = creds.get("username") or ""
    secret_key = creds.get("password") or ""
    # Temporary (STS) credentials carry a session token alongside the keys.
    session_token = creds.get("token") or ""
    if bool(access_key) != bool(secret_key):
        _log().warning(
            "S3: both username and password are required as credentials; only "
            "one was set — dropping both and falling back to the ambient "
            "credential chain."
        )
        access_key = secret_key = session_token = ""

    credential_provider = None
    role_arn = extra.get("aws_role_arn", "")
    if role_arn:
        # Cross-account / IRSA assume-role. obstore has no static role_arn
        # config key (setting one is a silent no-op that leaves the store on
        # the ambient identity), so wire an STS credential provider — the same
        # path binding.py uses. Base creds are optional: when omitted, the STS
        # call uses the pod's ambient chain (IRSA / instance profile) as the
        # caller identity.
        from application_sdk.storage._credential_providers import (  # noqa: PLC0415
            make_s3_assume_role_provider,
        )

        credential_provider = make_s3_assume_role_provider(
            role_arn=role_arn,
            # Distinct from binding.py's "atlan-application-sdk" default so the
            # two S3 auth paths are distinguishable in CloudTrail AssumeRole logs
            # (and preserves CloudStore's historical session name).
            session_name=extra.get("aws_role_session_name") or "cloud-store-session",
            # No region: scoping the STS session to the bucket's region breaks
            # AssumeRole for opt-in regions (e.g. me-central-1); region belongs
            # on the S3 store's own config, not the STS call.
            base_access_key=access_key or None,
            base_secret_key=secret_key or None,
            base_session_token=session_token or None,
            # Required when the role's trust policy has an sts:ExternalId
            # condition; the provider drops a blank one.
            external_id=extra.get("aws_external_id"),
        )
        _log().debug("S3 cross-account assume-role auth configured")

    settings = S3StoreSettings(
        bucket=bucket,
        region=extra.get("region") or "",
        endpoint=_tls_endpoint(extra),
        force_path_style=bool(_extra_bool(extra, "force_path_style")),
        disable_tagging=_extra_bool(extra, "disable_tagging"),
        access_key_id=access_key,
        secret_access_key=secret_key,
        session_token=session_token,
        conditional_put=_extra_str(extra, "conditional_put"),
        copy_if_not_exists=_extra_str(extra, "copy_if_not_exists"),
        checksum_algorithm=_extra_str(extra, "checksum_algorithm"),
        server_side_encryption=_extra_str(extra, "server_side_encryption"),
        sse_kms_key_id=_extra_str(extra, "sse_kms_key_id"),
        credential_provider=credential_provider,
    )

    storage_class = (extra.get("storageClass") or "").strip()
    put_attrs: dict[str, str] | None = (
        {"Storage-Class": storage_class} if storage_class else None
    )

    return build_s3_store(settings, label="cloud-s3"), put_attrs


def _create_gcs_store(
    creds: dict[str, Any], extra: dict[str, Any]
) -> tuple[ObjectStore, dict[str, str] | None]:
    """Create a GCS store from credentials."""
    from application_sdk._obstore_config import (  # noqa: PLC0415
        GCSStoreSettings,
        build_gcs_store,
    )

    bucket = extra.get("gcs_bucket", "")
    if not bucket:
        raise StorageConfigError("GCS bucket is required (extra.gcs_bucket)")

    storage_class = (extra.get("storageClass") or "").strip()
    put_attrs: dict[str, str] | None = (
        {"X-Goog-Storage-Class": storage_class} if storage_class else None
    )

    from application_sdk.storage.binding import (  # noqa: PLC0415
        _with_gcs_adc_provider,
    )

    # No key: Application Default Credentials, with the google-auth provider
    # for a Workload Identity Federation file, exactly as the binding does.
    settings = _with_gcs_adc_provider(
        GCSStoreSettings(bucket=bucket, service_account_key=creds.get("password") or "")
    )
    return build_gcs_store(settings, label="cloud-gcs"), put_attrs


def _create_azure_store(
    creds: dict[str, Any], extra: dict[str, Any]
) -> tuple[ObjectStore, dict[str, str] | None]:
    """Create an Azure (ADLS) store from credentials."""
    from application_sdk._obstore_config import (  # noqa: PLC0415
        AzureStoreSettings,
        build_azure_store,
    )

    storage_account = extra.get("storage_account_name", "")
    container = extra.get("adls_container", "objectstore")
    if not storage_account:
        raise StorageConfigError(
            "Azure storage account is required (extra.storage_account_name)"
        )

    password = creds.get("password") or ""
    client_id = creds.get("username") or ""
    tenant_id = extra.get("azure_tenant_id") or ""

    # Account key auth without a tenant; service principal with one (no
    # secret: workload identity). Anything else falls to the ambient identity.
    account_key = password if password and not tenant_id else ""
    service_principal = bool(tenant_id and client_id)
    settings = AzureStoreSettings(
        container=container,
        account_name=storage_account,
        endpoint=_tls_endpoint(extra),
        account_key=account_key,
        tenant_id=tenant_id if service_principal else "",
        client_id=client_id if service_principal else "",
        client_secret=password if service_principal else "",
    )

    storage_class = (extra.get("storageClass") or "").strip()
    put_attrs: dict[str, str] | None = (
        {"x-ms-access-tier": storage_class} if storage_class else None
    )

    return build_azure_store(settings, label="cloud-azure"), put_attrs
