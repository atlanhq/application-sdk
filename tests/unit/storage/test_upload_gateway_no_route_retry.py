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

from application_sdk.storage import ops
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
def sleeps(monkeypatch) -> list[float]:
    """Record the backoff delays instead of sleeping through them.

    Jitter is pinned to 1.0 so the recorded delays are the base delays.
    """
    recorded: list[float] = []

    async def fake_sleep(delay: float) -> None:
        recorded.append(delay)

    monkeypatch.setattr("application_sdk.storage.ops._retry_sleep", fake_sleep)
    monkeypatch.setattr("application_sdk.storage.ops._retry_jitter", lambda: 1.0)
    return recorded


@pytest.fixture
def storage_events():
    """Spy on ``_log_storage_event`` while still running it."""
    with patch(
        "application_sdk.storage.ops._log_storage_event",
        wraps=ops._log_storage_event,
    ) as spy:
        yield spy


def _failure_events(spy) -> list:
    return [c for c in spy.call_args_list if c.kwargs.get("outcome") == "failure"]


async def test_upload_succeeds_after_transient_gateway_no_route(
    src, sleeps, storage_events
) -> None:
    opener = _writer_factory([_GATEWAY_NO_ROUTE, _GATEWAY_NO_ROUTE, None])
    with patch("application_sdk.storage.ops.obstore.open_writer_async", opener):
        digest = await upload_file("k", src, MagicMock(), normalize=False, verify=False)

    assert opener.call_count == 3
    assert isinstance(digest, str)
    assert sleeps == [5.0, 10.0]
    assert _failure_events(storage_events) == []


async def test_other_404_raises_without_retry(src, sleeps) -> None:
    opener = _writer_factory([_PLAIN_NOT_FOUND, None])
    with (
        patch("application_sdk.storage.ops.obstore.open_writer_async", opener),
        pytest.raises(StorageError),
    ):
        await upload_file("k", src, MagicMock(), normalize=False, verify=False)

    assert opener.call_count == 1
    assert sleeps == []


async def test_persistent_gateway_no_route_raises_after_retry_budget(
    src, sleeps, storage_events
) -> None:
    opener = _writer_factory([_GATEWAY_NO_ROUTE] * 5)
    with (
        patch("application_sdk.storage.ops.obstore.open_writer_async", opener),
        pytest.raises(StorageError) as excinfo,
    ):
        await upload_file("k", src, MagicMock(), normalize=False, verify=False)

    assert opener.call_count == len(ops._GATEWAY_NO_ROUTE_RETRY_DELAYS) + 1
    assert sleeps == list(ops._GATEWAY_NO_ROUTE_RETRY_DELAYS)
    assert excinfo.value.http_status == 404
    assert len(_failure_events(storage_events)) == 1


@pytest.mark.parametrize("factor", [0.5, 1.5])
async def test_retry_delays_are_scaled_by_jitter(src, monkeypatch, factor) -> None:
    recorded: list[float] = []

    async def fake_sleep(delay: float) -> None:
        recorded.append(delay)

    monkeypatch.setattr("application_sdk.storage.ops._retry_sleep", fake_sleep)
    monkeypatch.setattr("application_sdk.storage.ops._retry_jitter", lambda: factor)
    opener = _writer_factory([_GATEWAY_NO_ROUTE, _GATEWAY_NO_ROUTE, None])
    with patch("application_sdk.storage.ops.obstore.open_writer_async", opener):
        await upload_file("k", src, MagicMock(), normalize=False, verify=False)

    assert recorded == [5.0 * factor, 10.0 * factor]
