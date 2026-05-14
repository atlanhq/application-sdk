"""Typed error leaves for the push-gateway metrics client."""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from application_sdk.errors.leaves import InvalidInputError


@dataclass(kw_only=True)
class PushGatewayUrlRequiredError(InvalidInputError):
    """PushGatewayClient was constructed without a url."""

    code: ClassVar[str] = "INVALID_INPUT_PUSHGATEWAY_URL"
    message: str = "PushGatewayClient requires a non-empty url"
    field: str | None = "url"


@dataclass(kw_only=True)
class PushGatewayJobRequiredError(InvalidInputError):
    """PushGatewayClient was constructed without a job name."""

    code: ClassVar[str] = "INVALID_INPUT_PUSHGATEWAY_JOB"
    message: str = "PushGatewayClient requires a non-empty job"
    field: str | None = "job"
