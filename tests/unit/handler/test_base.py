"""Unit tests for the Handler ABC and DefaultHandler."""

import pytest

from application_sdk.errors import HANDLER_ERROR, ErrorCode
from application_sdk.handler.base import DefaultHandler, Handler, HandlerError
from application_sdk.handler.context import HandlerContext
from application_sdk.handler.contracts import (
    AuthInput,
    AuthOutput,
    AuthStatus,
    MetadataInput,
    MetadataOutput,
    PreflightInput,
    PreflightOutput,
    PreflightStatus,
    SqlMetadataOutput,
)


class TestHandlerError:
    def test_basic_message(self):
        err = HandlerError("something failed")
        assert "something failed" in str(err)
        # Default error code is always included
        assert str(HANDLER_ERROR) in str(err)

    def test_default_error_code(self):
        err = HandlerError("something failed")
        assert err.error_code is HANDLER_ERROR
        assert isinstance(err.error_code, ErrorCode)

    def test_with_custom_error_code(self):
        custom = ErrorCode("HDL", 99)
        err = HandlerError("auth failed", error_code=custom)
        assert str(custom) in str(err)
        assert err.error_code is custom

    def test_default_http_status(self):
        err = HandlerError("something failed")
        assert err.http_status == 500

    def test_custom_http_status(self):
        err = HandlerError("not found", http_status=404)
        assert err.http_status == 404

    def test_cause(self):
        cause = ValueError("original")
        err = HandlerError("wrapped", cause=cause)
        assert err.cause is cause

    def test_all_fields(self):
        err = HandlerError(
            "oops",
            handler_name="MyHandler",
            app_name="my-app",
        )
        assert err.handler_name == "MyHandler"
        assert err.app_name == "my-app"
        assert "MyHandler" in str(err)
        assert "my-app" in str(err)


class TestHandlerContextProperty:
    def test_context_outside_invocation_raises(self):
        handler = DefaultHandler()
        with pytest.raises(RuntimeError, match="context is not set"):
            _ = handler.context

    def test_context_set_and_cleared(self):
        handler = DefaultHandler()
        ctx = HandlerContext(app_name="test-app")
        handler._context = ctx
        assert handler.context is ctx
        handler._context = None

    def test_is_abstract(self):
        # Cannot instantiate Handler directly
        with pytest.raises(TypeError):
            Handler()  # type: ignore[abstract]


class TestDefaultHandler:
    @pytest.fixture
    def handler(self):
        return DefaultHandler()

    async def test_test_auth_returns_success(self, handler):
        inp = AuthInput()
        result = await handler.test_auth(inp)
        assert isinstance(result, AuthOutput)
        assert result.status == AuthStatus.SUCCESS

    async def test_preflight_check_returns_ready(self, handler):
        inp = PreflightInput()
        result = await handler.preflight_check(inp)
        assert isinstance(result, PreflightOutput)
        assert result.status == PreflightStatus.READY

    async def test_fetch_metadata_returns_empty(self, handler):
        inp = MetadataInput()
        result = await handler.fetch_metadata(inp)
        assert isinstance(result, MetadataOutput)
        assert isinstance(result, SqlMetadataOutput)
        assert result.objects == []
