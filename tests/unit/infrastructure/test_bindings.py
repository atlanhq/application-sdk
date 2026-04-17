"""Unit tests for I/O bindings abstraction."""

from __future__ import annotations

import pytest

from application_sdk.infrastructure.bindings import (
    BindingError,
    BindingRequest,
    BindingResponse,
)
from application_sdk.testing.mocks import MockBinding


class TestBindingDataclasses:
    """Tests for BindingRequest / BindingResponse dataclasses."""

    def test_binding_request_defaults(self) -> None:
        req = BindingRequest(operation="get")
        assert req.operation == "get"
        assert req.data is None
        assert req.metadata == {}

    def test_binding_response_defaults(self) -> None:
        resp = BindingResponse()
        assert resp.data is None
        assert resp.metadata == {}

    def test_binding_response_with_data(self) -> None:
        resp = BindingResponse(data=b"hello", metadata={"key": "val"})
        assert resp.data == b"hello"
        assert resp.metadata == {"key": "val"}


class TestBindingError:
    """Tests for BindingError."""

    def test_str_includes_code(self) -> None:
        err = BindingError("something failed", binding_name="events")
        assert "AAF-INF-003" in str(err)
        assert "something failed" in str(err)

    def test_str_includes_binding_name(self) -> None:
        err = BindingError("failed", binding_name="my-binding", operation="invoke")
        assert "my-binding" in str(err)
        assert "invoke" in str(err)

    def test_custom_error_code(self) -> None:
        from application_sdk.errors import STATE_STORE_ERROR

        err = BindingError("bad", error_code=STATE_STORE_ERROR)
        assert err.error_code == STATE_STORE_ERROR
        assert "AAF-INF-001" in str(err)


class TestMockBinding:
    """Tests for MockBinding."""

    @pytest.mark.asyncio
    async def test_invoke_records_invocation(self) -> None:
        binding = MockBinding("test")
        await binding.invoke("get", b"data", {"k": "v"})
        invocations = binding.get_invocations()
        assert len(invocations) == 1
        assert invocations[0] == ("get", b"data", {"k": "v"})

    @pytest.mark.asyncio
    async def test_invoke_default_response(self) -> None:
        binding = MockBinding()
        resp = await binding.invoke("get")
        assert resp.data is None
        assert resp.metadata == {}

    @pytest.mark.asyncio
    async def test_invoke_configured_response(self) -> None:
        binding = MockBinding()
        binding.set_response(
            "get", BindingResponse(data=b"result", metadata={"x": "1"})
        )
        resp = await binding.invoke("get")
        assert resp.data == b"result"
        assert resp.metadata == {"x": "1"}

    @pytest.mark.asyncio
    async def test_get_invocations_filtered_by_operation(self) -> None:
        binding = MockBinding()
        await binding.invoke("get")
        await binding.invoke("put", b"x")
        await binding.invoke("get", b"y")
        get_calls = binding.get_invocations("get")
        assert len(get_calls) == 2
        put_calls = binding.get_invocations("put")
        assert len(put_calls) == 1

    @pytest.mark.asyncio
    async def test_clear_resets_invocations_and_responses(self) -> None:
        binding = MockBinding()
        binding.set_response("get", BindingResponse(data=b"x"))
        await binding.invoke("get")
        binding.clear()
        assert binding.get_invocations() == []
        resp = await binding.invoke("get")
        assert resp.data is None

    def test_name_property(self) -> None:
        binding = MockBinding("my-binding")
        assert binding.name == "my-binding"

    def test_default_name(self) -> None:
        binding = MockBinding()
        assert binding.name == "mock"

    @pytest.mark.asyncio
    async def test_metadata_defaults_to_empty_dict(self) -> None:
        binding = MockBinding()
        await binding.invoke("op", b"data")
        op, data, metadata = binding.get_invocations()[0]
        assert metadata == {}
