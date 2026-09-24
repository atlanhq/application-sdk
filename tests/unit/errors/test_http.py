"""Tests for the HTTP status and httpx exception to leaf classifiers."""

import httpx
import pytest

import application_sdk.errors as errors
from application_sdk.errors import (
    AppPermissionDeniedError,
    AuthError,
    NotFoundError,
    RateLimitedError,
    SourceUnavailableError,
    classify_http_exception,
    classify_http_status,
)

_REQUEST = httpx.Request("GET", "https://source.example.com/api")


def _status_error(status: int) -> httpx.HTTPStatusError:
    return httpx.HTTPStatusError(
        f"HTTP {status}",
        request=_REQUEST,
        response=httpx.Response(status, request=_REQUEST),
    )


def _raised_from(outer: Exception, inner: BaseException) -> Exception:
    try:
        raise outer from inner
    except Exception as exc:
        return exc


def _raised_during(outer: Exception, inner: BaseException) -> Exception:
    try:
        try:
            raise inner
        except BaseException:
            raise outer
    except Exception as exc:
        return exc


def test_both_classifiers_are_public():
    assert "classify_http_status" in errors.__all__
    assert "classify_http_exception" in errors.__all__


@pytest.mark.parametrize(
    ("status", "leaf"),
    [
        (401, AuthError),
        (403, AppPermissionDeniedError),
        (404, NotFoundError),
        (429, RateLimitedError),
        (500, SourceUnavailableError),
        (502, SourceUnavailableError),
        (503, SourceUnavailableError),
        (599, SourceUnavailableError),
    ],
)
def test_default_table(status, leaf):
    assert classify_http_status(status) is leaf


@pytest.mark.parametrize("status", [200, 302, 400, 402, 409, 418, 600])
def test_unmapped_status_returns_none(status):
    assert classify_http_status(status) is None


def test_override_replaces_a_default():
    assert classify_http_status(403, overrides={403: AuthError}) is AuthError


def test_override_adds_a_status():
    assert classify_http_status(402, overrides={402: AuthError}) is AuthError


def test_override_wins_over_the_5xx_range():
    overrides = {503: RateLimitedError}
    assert classify_http_status(503, overrides=overrides) is RateLimitedError
    assert classify_http_status(502, overrides=overrides) is SourceUnavailableError


def test_override_can_name_an_app_subclass():
    class SourceLicenceError(AuthError):
        code = "SOURCE_LICENCE"

    assert classify_http_status(402, overrides={402: SourceLicenceError}) is (
        SourceLicenceError
    )


def test_exception_status_error():
    assert classify_http_exception(_status_error(403)) is AppPermissionDeniedError


@pytest.mark.parametrize(
    "transport_error",
    [
        httpx.ConnectError("refused", request=_REQUEST),
        httpx.ConnectTimeout("slow", request=_REQUEST),
        httpx.ReadTimeout("slow", request=_REQUEST),
        httpx.ReadError("reset", request=_REQUEST),
        httpx.WriteError("broken pipe", request=_REQUEST),
        httpx.RemoteProtocolError("server disconnected", request=_REQUEST),
    ],
)
def test_exception_transport_failure_is_source_unavailable(transport_error):
    assert classify_http_exception(transport_error) is SourceUnavailableError


def test_exception_pool_timeout_is_not_blamed_on_the_source():
    assert classify_http_exception(httpx.PoolTimeout("pool full")) is None


def test_exception_does_not_walk_a_suppressed_context():
    try:
        try:
            raise _status_error(403)
        except httpx.HTTPStatusError:
            raise RuntimeError("replaced") from None
    except RuntimeError as exc:
        assert classify_http_exception(exc) is None


def test_exception_walks_cause():
    exc = _raised_from(RuntimeError("client failed"), _status_error(401))
    assert classify_http_exception(exc) is AuthError


def test_exception_walks_context():
    exc = _raised_during(ValueError("while parsing"), _status_error(429))
    assert classify_http_exception(exc) is RateLimitedError


def test_exception_skips_unmapped_status_and_keeps_walking():
    exc = _raised_from(_status_error(400), httpx.ConnectError("refused"))
    assert classify_http_exception(exc) is SourceUnavailableError


def test_exception_uses_overrides():
    exc = _raised_from(RuntimeError("licence"), _status_error(402))
    assert classify_http_exception(exc, overrides={402: AuthError}) is AuthError


def test_exception_with_no_http_frame_returns_none():
    exc = _raised_from(RuntimeError("outer"), KeyError("inner"))
    assert classify_http_exception(exc) is None


def test_exception_stops_on_a_cause_cycle():
    first = RuntimeError("first")
    second = RuntimeError("second")
    first.__cause__ = second
    second.__cause__ = first
    assert classify_http_exception(first) is None
