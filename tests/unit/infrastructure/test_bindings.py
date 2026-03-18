"""Unit tests for I/O bindings abstraction (event/generic bindings only).

StorageBinding and InMemoryBinding have been removed; object storage is now
handled by the ``application_sdk.storage`` module backed by obstore.
"""

from __future__ import annotations

from application_sdk.infrastructure.bindings import (
    BindingError,
    BindingRequest,
    BindingResponse,
)


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
