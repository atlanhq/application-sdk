"""Tests for the HTTP status and httpx exception to leaf classifiers."""

import httpx
import pytest

import application_sdk.errors as errors
from application_sdk.errors import (
    AppError,
    AppPermissionDeniedError,
    AuthError,
    InternalError,
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
        (501, SourceUnavailableError),
        (502, SourceUnavailableError),
        (505, SourceUnavailableError),
        (503, SourceUnavailableError),
        (599, SourceUnavailableError),
    ],
)
def test_default_table(status: int, leaf: type[AppError]) -> None:
    assert classify_http_status(status) is leaf


@pytest.mark.parametrize("status", [200, 302, 400, 402, 409, 418, 600])
def test_unmapped_status_returns_none(status: int) -> None:
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
def test_exception_transport_failure_is_source_unavailable(
    transport_error: httpx.TransportError,
) -> None:
    assert classify_http_exception(transport_error) is SourceUnavailableError


@pytest.mark.parametrize(
    ("status", "leaf"),
    [
        (401, AuthError),
        (403, AppPermissionDeniedError),
        (404, NotFoundError),
        (429, RateLimitedError),
        (500, SourceUnavailableError),
        (599, SourceUnavailableError),
    ],
)
def test_exception_direct_status_uses_the_default_table(
    status: int, leaf: type[AppError]
) -> None:
    assert classify_http_exception(_status_error(status)) is leaf


def test_exception_unmapped_status_returns_none():
    assert classify_http_exception(_status_error(400)) is None


def test_exception_uses_overrides():
    exc = _raised_from(RuntimeError("licence"), _status_error(402))
    assert classify_http_exception(exc, overrides={402: AuthError}) is AuthError
    overridden = classify_http_exception(
        _status_error(503), overrides={503: RateLimitedError}
    )
    assert overridden is RateLimitedError


def test_exception_walks_an_explicit_cause():
    exc = _raised_from(RuntimeError("client failed"), _status_error(403))
    assert classify_http_exception(exc) is AppPermissionDeniedError


@pytest.mark.parametrize(
    ("outer", "status"),
    [(KeyError("items"), 403), (ValueError("bad json"), 429)],
)
def test_exception_does_not_inherit_a_status_from_implicit_context(
    outer: Exception, status: int
) -> None:
    exc = _raised_during(outer, _status_error(status))
    assert classify_http_exception(exc) is None


def test_exception_does_not_walk_a_suppressed_context():
    try:
        try:
            raise _status_error(403)
        except httpx.HTTPStatusError:
            raise RuntimeError("replaced") from None
    except RuntimeError as exc:
        assert classify_http_exception(exc) is None


def test_exception_follows_the_explicit_cause_not_the_implicit_context():
    try:
        try:
            raise _status_error(403)
        except httpx.HTTPStatusError:
            raise RuntimeError("wrapped") from KeyError("unrelated")
    except RuntimeError as exc:
        assert classify_http_exception(exc) is None


def test_exception_pool_timeout_is_not_blamed_on_the_source():
    assert classify_http_exception(httpx.PoolTimeout("pool full")) is None


def test_exception_pool_timeout_is_a_boundary():
    implicit = _raised_during(httpx.PoolTimeout("pool full"), _status_error(403))
    explicit = _raised_from(httpx.PoolTimeout("pool full"), _status_error(403))
    assert classify_http_exception(implicit) is None
    assert classify_http_exception(explicit) is None


@pytest.mark.parametrize(
    "frame",
    [
        httpx.DecodingError("bad gzip"),
        httpx.TooManyRedirects("loop"),
        httpx.LocalProtocolError("bad header"),
        httpx.UnsupportedProtocol("ftp"),
    ],
)
def test_exception_other_httpx_errors_are_a_boundary(frame: httpx.HTTPError) -> None:
    exc = _raised_from(frame, _status_error(403))
    assert classify_http_exception(exc) is None


def test_exception_unmapped_status_is_a_boundary():
    exc = _raised_from(_status_error(400), httpx.ConnectError("refused"))
    assert classify_http_exception(exc) is None


def test_exception_with_no_http_frame_returns_none():
    exc = _raised_from(RuntimeError("outer"), KeyError("inner"))
    assert classify_http_exception(exc) is None


def test_exception_stops_on_a_cause_cycle():
    first = RuntimeError("first")
    second = RuntimeError("second")
    first.__cause__ = second
    second.__cause__ = first
    assert classify_http_exception(first) is None


def _app_preflight_error(
    exc: Exception, overrides: dict[int, type[AppError]] | None = None
) -> AppError:
    if isinstance(exc, AppError):
        return exc
    leaf = classify_http_exception(exc, overrides=overrides)
    if leaf is None:
        leaf = InternalError
    return leaf(message=f"{leaf.__name__} from the probe.", cause=exc)


def test_documented_pattern_keeps_a_typed_error():
    typed = AuthError(message="The API key is empty.")
    assert _app_preflight_error(typed) is typed


def test_documented_pattern_falls_back_to_internal_error():
    error = _app_preflight_error(KeyError("items"))
    assert type(error) is InternalError
    assert isinstance(error.cause, KeyError)


def test_documented_pattern_applies_vendor_overrides():
    wrapped = _raised_from(RuntimeError("licence"), _status_error(402))
    assert type(_app_preflight_error(wrapped)) is InternalError
    assert type(_app_preflight_error(wrapped, {402: AuthError})) is AuthError
    assert type(_app_preflight_error(_status_error(403))) is AppPermissionDeniedError
