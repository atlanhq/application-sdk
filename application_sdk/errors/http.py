"""Map HTTP statuses and httpx failures onto the categorical leaves."""

from collections.abc import Iterator, Mapping

import httpx

from application_sdk.errors.base import AppError
from application_sdk.errors.leaves import (
    AppPermissionDeniedError,
    AuthError,
    NotFoundError,
    RateLimitedError,
    SourceUnavailableError,
)

_DEFAULT_STATUS_LEAVES: Mapping[int, type[AppError]] = {
    401: AuthError,
    403: AppPermissionDeniedError,
    404: NotFoundError,
    429: RateLimitedError,
}
_SOURCE_TRANSPORT_FAILURES = (
    httpx.NetworkError,
    httpx.TimeoutException,
    httpx.RemoteProtocolError,
)


def classify_http_status(
    status: int, *, overrides: Mapping[int, type[AppError]] | None = None
) -> type[AppError] | None:
    """Return the leaf class for an HTTP status, or ``None`` when unmapped.

    Returns the class so the caller supplies ``message``,
    ``suggested_action`` and ``cause``. ``overrides`` wins over the default
    table, including the 5xx range, for sources whose statuses mean
    something else (a 402 licence failure, a 503 that is really a rate limit).
    """
    if overrides and status in overrides:
        return overrides[status]
    if status in _DEFAULT_STATUS_LEAVES:
        return _DEFAULT_STATUS_LEAVES[status]
    if 500 <= status <= 599:
        return SourceUnavailableError
    return None


def classify_http_exception(
    exc: BaseException, *, overrides: Mapping[int, type[AppError]] | None = None
) -> type[AppError] | None:
    """Return the leaf class for the first httpx failure in ``exc``'s chain.

    Walks ``exc`` and its ``__cause__`` / ``__context__`` links, honouring
    ``raise ... from None``. A network failure, a timeout or a dropped
    connection is ``SourceUnavailableError``; an ``httpx.HTTPStatusError``
    goes through :func:`classify_http_status`. ``httpx.PoolTimeout`` is the
    client's own connection pool running dry, not the source, so it is skipped.
    """
    for frame in _walk_chain(exc):
        if isinstance(frame, httpx.PoolTimeout):
            continue
        if isinstance(frame, _SOURCE_TRANSPORT_FAILURES):
            return SourceUnavailableError
        if isinstance(frame, httpx.HTTPStatusError):
            leaf = classify_http_status(frame.response.status_code, overrides=overrides)
            if leaf is not None:
                return leaf
    return None


def _walk_chain(exc: BaseException) -> Iterator[BaseException]:
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        yield current
        if current.__cause__ is not None or current.__suppress_context__:
            current = current.__cause__
        else:
            current = current.__context__
