"""Typed error leaves for the BaseClient HTTP helpers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from application_sdk.errors.leaves import SourceUnavailableError


@dataclass(kw_only=True)
class HttpClientSourceUnavailableError(SourceUnavailableError):
    """An HTTP helper request got no response from the source.

    A network failure, a timeout or a dropped connection: the request never
    produced an HTTP status, so this is not a 4xx or 5xx from the source.
    Raised by ``BaseClient.execute_http_get_request`` and
    ``execute_http_post_request`` when called with ``raise_on_error=True``.
    """

    code: ClassVar[str] = "SOURCE_UNAVAILABLE_HTTP_CLIENT"
    message: str = "HTTP client could not reach the source"
    suggested_action: str | None = (
        "Check DNS resolution, firewall and private-link rules, TLS "
        "configuration, and that the source endpoint is up and accepting "
        "connections from the worker network."
    )
    source_type: str | None = "http"
