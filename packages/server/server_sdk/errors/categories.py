"""Failure taxonomy — the category/audience enums the typed-error wire uses.

``FailureCategory`` is the knob the HTTP boundary reads to pick a status code
(see ``_CATEGORY_TO_HTTP`` in ``server_sdk.server``); ``Audience`` rides along
on :class:`FailureDetails`.
"""

from __future__ import annotations

from server_sdk.contracts.base import SerializableEnum


class FailureCategory(SerializableEnum):
    """What kind of failure this is — drives the HTTP status mapping.

    Values are the UPPERCASE member names — this is the canonical on-the-wire
    spelling, so a serialized ``PreflightCheck.error`` stays stable for clients.
    """

    AUTH = "AUTH"
    PERMISSION = "PERMISSION"
    NOT_FOUND = "NOT_FOUND"
    ALREADY_EXISTS = "ALREADY_EXISTS"
    INVALID_INPUT = "INVALID_INPUT"
    PRECONDITION = "PRECONDITION"
    RATE_LIMITED = "RATE_LIMITED"
    TIMEOUT = "TIMEOUT"
    DEPENDENCY_UNAVAILABLE = "DEPENDENCY_UNAVAILABLE"
    SOURCE_UNAVAILABLE = "SOURCE_UNAVAILABLE"
    RESOURCE_EXHAUSTED = "RESOURCE_EXHAUSTED"
    DATA_INTEGRITY = "DATA_INTEGRITY"
    INTERNAL = "INTERNAL"
    UNIMPLEMENTED = "UNIMPLEMENTED"
    CANCELLED = "CANCELLED"


class Audience(SerializableEnum):
    """Who a failure message is written for."""

    USER = "USER"
    PLATFORM = "PLATFORM"
    APP_OWNER = "APP_OWNER"
