"""BLDX-1619: invoke_binding must not corrupt a non-UTF-8 payload.

Dapr's HTTP binding API carries the payload inside a JSON body, so there is no
raw-bytes channel.  The old code decoded with ``errors="replace"``, which turns
every invalid byte of a parquet file into U+FFFD and writes the damaged result
without a word.  A loud typed failure is the only honest option here: the caller
must base64-encode and set ``decodeBase64`` on the component, or use
``App.upload`` (obstore), which has a real binary path.
"""

from __future__ import annotations

from unittest import mock

import httpx
import orjson
import pytest

from application_sdk.infrastructure._dapr.http import AsyncDaprClient

PARQUET_BYTES = b"PAR1\x00\x01\xff\xfe\x89PNGPAR1"


def _client_capturing(captured: dict) -> AsyncDaprClient:
    async def _handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = orjson.loads(request.content)
        return httpx.Response(200, content=b"")

    client = AsyncDaprClient()
    client._client = httpx.AsyncClient(
        transport=httpx.MockTransport(_handler), base_url="http://localhost:3500"
    )
    return client


class TestBinaryPayloadIsRejected:
    async def test_non_utf8_bytes_raise_instead_of_being_mangled(self) -> None:
        from application_sdk.infrastructure._dapr._dapr_errors import (
            DaprBinaryPayloadError,
        )

        captured: dict = {}
        client = _client_capturing(captured)
        try:
            with pytest.raises(DaprBinaryPayloadError) as exc:
                await client.invoke_binding("objectstore", "create", data=PARQUET_BYTES)
        finally:
            await client._client.aclose()

        assert captured == {}, "must fail before any request is sent"
        assert exc.value.binding_name == "objectstore"
        assert exc.value.effective_retryable is False
        # The remedy has to be in the message — this fires at a caller's write site.
        assert "base64" in str(exc.value).lower()

    async def test_the_payload_bytes_never_appear_in_the_error(self) -> None:
        """Binding payloads can hold customer data — keep them out of logs."""
        from application_sdk.infrastructure._dapr._dapr_errors import (
            DaprBinaryPayloadError,
        )

        client = _client_capturing({})
        try:
            with pytest.raises(DaprBinaryPayloadError) as exc:
                await client.invoke_binding(
                    "objectstore", "create", data=b"secret\xffvalue"
                )
        finally:
            await client._client.aclose()

        assert "secret" not in str(exc.value)


class TestTextAndJsonPayloadsAreUnchanged:
    async def test_json_object_is_still_embedded_as_a_parsed_object(self) -> None:
        captured: dict = {}
        client = _client_capturing(captured)
        try:
            await client.invoke_binding(
                "eventstore", "create", data=orjson.dumps({"a": 1})
            )
        finally:
            await client._client.aclose()

        assert captured["body"]["data"] == {"a": 1}

    async def test_plain_utf8_text_is_still_sent_as_a_string(self) -> None:
        captured: dict = {}
        client = _client_capturing(captured)
        try:
            await client.invoke_binding("eventstore", "create", data="héllo".encode())
        finally:
            await client._client.aclose()

        assert captured["body"]["data"] == "héllo"

    async def test_a_bare_json_scalar_is_still_sent_as_text(self) -> None:
        captured: dict = {}
        client = _client_capturing(captured)
        try:
            await client.invoke_binding("eventstore", "create", data=b"42")
        finally:
            await client._client.aclose()

        assert captured["body"]["data"] == "42"

    async def test_empty_payload_still_omits_the_data_field(self) -> None:
        captured: dict = {}
        client = _client_capturing(captured)
        try:
            await client.invoke_binding("eventstore", "create", data=b"")
        finally:
            await client._client.aclose()

        assert "data" not in captured["body"]


class TestExistingBindingCallersAreUnaffected:
    async def test_dapr_binding_wrapper_propagates_the_typed_error(self) -> None:
        """DaprBinding.invoke wraps errors — the payload error must stay recognisable."""
        from application_sdk.infrastructure._dapr._dapr_errors import (
            DaprBinaryPayloadError,
        )
        from application_sdk.infrastructure._dapr.client import DaprBinding

        client = mock.Mock()
        client.invoke_binding = mock.AsyncMock(
            side_effect=DaprBinaryPayloadError(binding_name="objectstore")
        )

        with pytest.raises(DaprBinaryPayloadError):
            await DaprBinding(client, "objectstore").invoke(
                "create", data=PARQUET_BYTES
            )
