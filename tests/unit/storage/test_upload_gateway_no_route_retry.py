"""``upload_file`` retries a gateway "no Route matched" 404, and only that 404.

Atlan's ``/api/blobstorage`` route is served through a Kong gateway. A gateway
instance that joins the load balancer before its route table has synced
answers every request with ``404 {"message":"no Route matched with those
values"}`` for a few seconds. The Rust object_store client never retries a 404,
so without a Python-level retry that single blip fails the whole upload.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from application_sdk.storage.errors import StorageError
from application_sdk.storage.ops import upload_file

# The shape object_store renders a backend HTTP failure in, with the gateway's
# own JSON body. Host and key are synthetic.
_GATEWAY_NO_ROUTE = (
    "Generic S3 error: Error performing PUT "
    "https://gateway.example.invalid/api/blobstorage/artifacts/k in 8ms - "
    "Server returned non-2xx status code: 404 Not Found: "
    '{"message":"no Route matched with those values"}'
)
_PLAIN_NOT_FOUND = (
    "Generic S3 error: Error performing PUT "
    "https://bucket.example.invalid/artifacts/k in 8ms - "
    "Server returned non-2xx status code: 404 Not Found: "
    "<Error><Code>NoSuchBucket</Code><Message>missing</Message></Error>"
)


class _FailingWriterCM:
    """A writer whose close (the multipart completion) raises *message*."""

    def __init__(self, message: str | None) -> None:
        self._message = message

    async def __aenter__(self) -> MagicMock:
        writer = MagicMock()
        writer.write = AsyncMock()
        return writer

    async def __aexit__(self, *exc: object) -> None:
        if self._message is not None:
            raise RuntimeError(self._message)


def _writer_factory(messages: list[str | None]) -> MagicMock:
    """``open_writer_async`` stand-in: the n-th call closes with ``messages[n]``."""
    seq = iter(messages)
    return MagicMock(side_effect=lambda *a, **kw: _FailingWriterCM(next(seq)))


@pytest.fixture
def src(tmp_path):
    path = tmp_path / "data.bin"
    path.write_bytes(b"payload")
    return path


@pytest.fixture
def sleeps(monkeypatch) -> AsyncMock:
    """Record the backoff delays instead of sleeping through them."""
    fake = AsyncMock()
    monkeypatch.setattr("application_sdk.storage.ops._retry_sleep", fake, raising=False)
    return fake


async def test_upload_succeeds_after_transient_gateway_no_route(src, sleeps) -> None:
    opener = _writer_factory([_GATEWAY_NO_ROUTE, _GATEWAY_NO_ROUTE, None])
    with patch("application_sdk.storage.ops.obstore.open_writer_async", opener):
        digest = await upload_file("k", src, MagicMock(), normalize=False, verify=False)

    assert opener.call_count == 3
    assert isinstance(digest, str)
    assert [c.args[0] for c in sleeps.await_args_list] == [5.0, 10.0]


async def test_other_404_raises_without_retry(src, sleeps) -> None:
    opener = _writer_factory([_PLAIN_NOT_FOUND, None])
    with (
        patch("application_sdk.storage.ops.obstore.open_writer_async", opener),
        pytest.raises(StorageError),
    ):
        await upload_file("k", src, MagicMock(), normalize=False, verify=False)

    assert opener.call_count == 1
    sleeps.assert_not_awaited()


async def test_persistent_gateway_no_route_raises_after_retry_budget(
    src, sleeps
) -> None:
    opener = _writer_factory([_GATEWAY_NO_ROUTE] * 5)
    with (
        patch("application_sdk.storage.ops.obstore.open_writer_async", opener),
        pytest.raises(StorageError) as excinfo,
    ):
        await upload_file("k", src, MagicMock(), normalize=False, verify=False)

    assert opener.call_count == 4
    assert [c.args[0] for c in sleeps.await_args_list] == [5.0, 10.0, 20.0]
    assert excinfo.value.http_status == 404
