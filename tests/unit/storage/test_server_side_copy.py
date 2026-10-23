"""Store-to-store copies must not require object-sized temporary disk space."""

from __future__ import annotations

import hashlib
from unittest.mock import AsyncMock, patch

import boto3
import obstore
import pytest
from botocore.stub import Stubber
from obstore.store import MemoryStore, S3Store

from application_sdk.storage.integrity import read_expected_digest, write_digest_sidecar
from application_sdk.storage.transfer import _upload_from_store


@pytest.mark.parametrize("with_digest", [True, False])
async def test_same_store_copy_without_staging(with_digest: bool) -> None:
    """Copy actual content and carry its digest without allocating a temp file."""
    store = MemoryStore()
    content = b'{"column": "example"}\n'
    digest = hashlib.sha256(content).hexdigest()
    await obstore.put_async(store, "run/data.json", content)
    if with_digest:
        await write_digest_sidecar(store, "run/data.json", digest)
    # An old target sidecar must not survive a copy from an unverified source.
    await obstore.put_async(store, "publish/data.json", b"old")
    await write_digest_sidecar(store, "publish/data.json", "a" * 64)
    with patch(
        "application_sdk.storage.transfer.tempfile.mkstemp",
        side_effect=AssertionError("object-sized local staging is forbidden"),
    ):
        transferred, reason, actual_digest = await _upload_from_store(
            store, "run/data.json", store, "publish/data.json"
        )
    assert transferred and reason == "uploaded"
    assert actual_digest == (digest if with_digest else None)
    result = await obstore.get_async(store, "publish/data.json")
    assert bytes(await result.bytes_async()) == content
    assert await read_expected_digest(store, "publish/data.json") == actual_digest


async def test_large_s3_copy_uses_multipart_without_staging() -> None:
    """Replay a 26.6 GiB object through real boto3 request validation, no payload."""
    size = int(26.6 * 1024**3)
    store = S3Store("copy-test", region="us-east-1", skip_signature=True)
    client = boto3.client(
        "s3",
        region_name="us-east-1",
        aws_access_key_id="testing",
        aws_secret_access_key="testing",
    )
    source = {"Bucket": "copy-test", "Key": "run/data.json"}
    common = {"Bucket": "copy-test", "Key": "publish/data.json", "UploadId": "upload"}
    part_size = 512 * 1024**2
    parts = []
    with Stubber(client) as stub:
        stub.add_response(
            "create_multipart_upload",
            {"UploadId": "upload"},
            {"Bucket": "copy-test", "Key": "publish/data.json"},
        )
        for number, start in enumerate(range(0, size, part_size), 1):
            etag = f"part-{number}"
            parts.append({"PartNumber": number, "ETag": etag})
            stub.add_response(
                "upload_part_copy",
                {"CopyPartResult": {"ETag": etag}},
                {
                    **common,
                    "PartNumber": number,
                    "CopySource": source,
                    "CopySourceRange": f"bytes={start}-{min(start + part_size, size) - 1}",
                    "CopySourceIfMatch": '"source-version"',
                },
            )
        stub.add_response(
            "complete_multipart_upload",
            {},
            {**common, "MultipartUpload": {"Parts": parts}},
        )
        head = AsyncMock(
            return_value={"size": size, "e_tag": '"source-version"', "attributes": {}}
        )
        digest = "b" * 64
        with (
            patch("obstore.head_async", head),
            patch("boto3.client", return_value=client),
            patch("obstore.delete_async", new_callable=AsyncMock),
            patch(
                "application_sdk.storage.transfer._cross_store_sha256_match",
                return_value=None,
            ),
            patch(
                "application_sdk.storage.transfer.read_expected_digest",
                return_value=digest,
            ),
            patch(
                "application_sdk.storage.integrity.write_digest_sidecar",
                new_callable=AsyncMock,
            ) as write,
            patch(
                "application_sdk.storage.transfer.tempfile.mkstemp",
                side_effect=AssertionError("26.6 GiB cannot fit on 12.5 GiB disk"),
            ),
        ):
            result = await _upload_from_store(
                store, "run/data.json", store, "publish/data.json"
            )
        assert result == (True, "uploaded", digest)
        write.assert_awaited_once_with(store, "publish/data.json", digest)
        stub.assert_no_pending_responses()


async def test_failed_multipart_copy_aborts_and_does_not_write_sidecar() -> None:
    """A failed range copy must clean up its upload rather than claim success."""
    store = S3Store("copy-test", region="us-east-1", skip_signature=True)
    client = boto3.client(
        "s3",
        region_name="us-east-1",
        aws_access_key_id="testing",
        aws_secret_access_key="testing",
    )
    common = {"Bucket": "copy-test", "Key": "publish/data.json", "UploadId": "upload"}
    with Stubber(client) as stub:
        stub.add_response(
            "create_multipart_upload",
            {"UploadId": "upload"},
            {"Bucket": "copy-test", "Key": "publish/data.json"},
        )
        stub.add_client_error("upload_part_copy", "AccessDenied", "denied")
        stub.add_response("abort_multipart_upload", {}, common)
        with (
            patch(
                "obstore.head_async",
                new=AsyncMock(
                    return_value={
                        "size": 6 * 1024**3,
                        "e_tag": "version",
                        "attributes": {},
                    }
                ),
            ),
            patch("boto3.client", return_value=client),
            patch("obstore.delete_async", new_callable=AsyncMock),
            patch(
                "application_sdk.storage.transfer._cross_store_sha256_match",
                return_value=None,
            ),
            patch(
                "application_sdk.storage.transfer.read_expected_digest",
                return_value="c" * 64,
            ),
            patch(
                "application_sdk.storage.integrity.write_digest_sidecar",
                new_callable=AsyncMock,
            ) as write,
            patch(
                "application_sdk.storage.transfer.tempfile.mkstemp",
                side_effect=AssertionError("no staging"),
            ),
        ):
            from application_sdk.storage.errors import StorageError

            with pytest.raises(StorageError, match="AccessDenied"):
                await _upload_from_store(
                    store, "run/data.json", store, "publish/data.json"
                )
        write.assert_not_awaited()
        stub.assert_no_pending_responses()
