"""Same-store copies without object-sized local staging.

obstore's S3 copy uses CopyObject, which is limited to 5 GiB. Larger objects
need UploadPartCopy ranges. boto3 is already a core dependency; its blocking
requests are offloaded and use the source store's identity, not another bucket.
"""

from __future__ import annotations

import contextlib
import inspect
from typing import TYPE_CHECKING, Any

import boto3
import obstore
from botocore import UNSIGNED
from botocore.config import Config
from obstore.store import S3Store

from application_sdk._runtime.offload import run_in_thread
from application_sdk._runtime.progress import current_progress_tracker
from application_sdk.storage.integrity import sidecar_key
from application_sdk.storage.ops import (
    _resolve_put_attributes,
    _storage_error_for,
    delete,
)

if TYPE_CHECKING:
    from obstore.store import ObjectStore

_SINGLE_COPY_LIMIT = 5 * 1024**3
_COPY_PART_SIZE = 512 * 1024**2


async def _s3_client(store: S3Store) -> Any:
    """Build a bounded-time client using this store's endpoint and credentials.

    Refresh the provider before each request so a long copy does not outlive
    a temporary session. The ambient chain is used only when the store itself
    has no explicit identity, matching obstore's default credential discovery.
    """
    config = dict(store.config)
    kwargs: dict[str, Any] = {
        "region_name": config.get("region") or config.get("default_region"),
        "endpoint_url": config.get("aws_endpoint_url_s3") or config.get("endpoint"),
        "verify": str(
            (store.client_options or {}).get("allow_invalid_certificates", "false")
        ).lower()
        != "true",
    }
    unsigned = str(config.get("skip_signature", "false")).lower() == "true"
    provider = store.credential_provider
    if not unsigned and provider is not None:
        credentials = await run_in_thread(provider)
        if inspect.isawaitable(credentials):
            credentials = await credentials
        kwargs.update(
            aws_access_key_id=credentials["access_key_id"],
            aws_secret_access_key=credentials["secret_access_key"],
            aws_session_token=credentials.get("token"),
        )
    elif not unsigned and config.get("access_key_id"):
        kwargs.update(
            aws_access_key_id=config["access_key_id"],
            aws_secret_access_key=config.get("secret_access_key"),
            aws_session_token=config.get("session_token"),
        )
    elif not unsigned and (
        config.get("role_arn") or config.get("web_identity_token_file")
    ):
        # Never silently fall back to the worker's identity for a custom role.
        raise ValueError(
            "Multipart copy requires a credential provider for an explicit S3 role"
        )
    kwargs["config"] = Config(
        signature_version=UNSIGNED if unsigned else "s3v4",
        connect_timeout=10,
        read_timeout=120,
        retries={"mode": "standard", "max_attempts": 3},
        s3={
            "addressing_style": "virtual"
            if str(config.get("virtual_hosted_style_request", "false")).lower()
            == "true"
            else "path"
        },
    )
    return await run_in_thread(boto3.client, "s3", **kwargs)


async def _s3_request(store: S3Store, operation: str, **kwargs: Any) -> Any:
    """Make one offloaded request and close its connection pool afterwards."""
    client = await _s3_client(store)
    try:
        return await run_in_thread(getattr(client, operation), **kwargs)
    finally:
        await run_in_thread(client.close)


async def _multipart_copy(
    store: S3Store, source: str, target: str, meta: dict[str, Any]
) -> None:
    """Copy bounded ranges, pinning every part to the source HEAD's ETag."""
    prefix = store.prefix
    source_key = f"{prefix.rstrip('/')}/{source}" if prefix else source
    target_key = f"{prefix.rstrip('/')}/{target}" if prefix else target
    config = dict(store.config)
    common = {"Bucket": config["bucket"], "Key": target_key}
    requester = {}
    if str(config.get("request_payer", "false")).lower() in {"true", "requester"}:
        requester["RequestPayer"] = "requester"
    create: dict[str, Any] = dict(common, **requester)
    # Preserve object metadata, and honour the binding's target storage class.
    attributes = dict(meta.get("attributes") or {})
    attributes.update(_resolve_put_attributes(store) or {})
    names = {
        "Content-Type": "ContentType",
        "Content-Encoding": "ContentEncoding",
        "Content-Language": "ContentLanguage",
        "Content-Disposition": "ContentDisposition",
        "Cache-Control": "CacheControl",
        "Storage-Class": "StorageClass",
    }
    for key, value in attributes.items():
        if key in names:
            create[names[key]] = value
    if config.get("server_side_encryption"):
        create["ServerSideEncryption"] = config["server_side_encryption"]
    if config.get("sse_kms_key_id"):
        create["SSEKMSKeyId"] = config["sse_kms_key_id"]
    size = meta["size"]
    # Round upward to preserve S3's 10,000-part limit for every supported size.
    part_size = max(_COPY_PART_SIZE, (size + 9999) // 10000)
    etag = meta.get("e_tag")
    if not etag:
        raise ValueError("Multipart copy requires the source ETag to pin its ranges")
    upload = await _s3_request(store, "create_multipart_upload", **create)
    upload_id = upload["UploadId"]
    try:
        parts = []
        for number, start in enumerate(range(0, size, part_size), 1):
            result = await _s3_request(
                store,
                "upload_part_copy",
                **common,
                **requester,
                UploadId=upload_id,
                PartNumber=number,
                CopySource={"Bucket": config["bucket"], "Key": source_key},
                CopySourceRange=f"bytes={start}-{min(start + part_size, size) - 1}",
                CopySourceIfMatch=etag,
            )
            parts.append(
                {"PartNumber": number, "ETag": result["CopyPartResult"]["ETag"]}
            )
            current_progress_tracker().mark_progress("storage.copy_file")
        await _s3_request(
            store,
            "complete_multipart_upload",
            **common,
            **requester,
            UploadId=upload_id,
            MultipartUpload={"Parts": parts},
        )
    except BaseException:
        # Cancellation also needs cleanup. Never mask the original copy failure.
        with contextlib.suppress(Exception):
            await _s3_request(
                store,
                "abort_multipart_upload",
                **common,
                **requester,
                UploadId=upload_id,
            )
        raise


async def _copy_same_store(store: ObjectStore, source: str, target: str) -> None:
    """Copy an object within its store and invalidate the old target digest."""
    try:
        meta = await obstore.head_async(store, source)
        # Invalidate first: if copying succeeds but writing the new sidecar fails,
        # a digest for the overwritten target must not remain authoritative.
        await delete(sidecar_key(target), store, normalize=False)
        if isinstance(store, S3Store) and meta["size"] > _SINGLE_COPY_LIMIT:
            await _multipart_copy(store, source, target, dict(meta))
        else:
            await obstore.copy_async(store, source, target)
    except Exception as exc:
        raise _storage_error_for(
            exc, target, "Server-side object copy failed", store
        ) from exc
