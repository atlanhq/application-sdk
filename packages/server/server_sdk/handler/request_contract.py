"""Ingress validation that answers 422, not 500.

The routes validate their own body (they normalise v2/v3 wire shapes first),
so FastAPI's own ``RequestValidationError`` path never sees it. A bare
``model_validate`` therefore raises a pydantic ``ValidationError`` inside the
route, which escapes to ``ServerErrorMiddleware`` and reaches the caller as an
opaque 500 with no hint of which field was wrong -- and on a consolidated host
a 500 is indistinguishable from a host fault, so it gets triaged as one.

Ported from ``application_sdk.handler.service`` so both surfaces agree.
"""

from __future__ import annotations

from typing import Any, TypeVar

from pydantic import BaseModel as PydanticBaseModel
from pydantic import ValidationError

ModelT = TypeVar("ModelT", bound=PydanticBaseModel)


class RequestContractError(Exception):
    """A request body did not fit its typed contract at ingress -> 422.

    The marker *is* the enforcement. The 422 handler is registered on this
    type, not on pydantic's ``ValidationError``, so an endpoint gets a
    contract-shaped answer only by validating through :func:`validate_request`
    -- which is also the only place that decides a failure is the caller's
    fault. A ``ValidationError`` raised anywhere else (inside a route's error
    boundary, in handler business logic, in an output contract) keeps that
    route's own 500 and can never be mistaken for a malformed request.

    So the default for a new endpoint is the safe one: a bare
    ``model_validate`` still yields a 500.
    """

    def __init__(self, cause: ValidationError) -> None:
        super().__init__(str(cause))
        #: The pydantic failure, kept so the handler can name the fields.
        self.cause = cause


def validate_request(model: type[ModelT], body: dict[str, Any]) -> ModelT:
    """Validate an already-normalised request body into its typed contract.

    Raises :class:`RequestContractError` so the app-level handler answers 422
    naming the offending field. Use this for anything a *caller* controls; use
    ``model_validate`` directly where a failure would be an internal bug.
    """
    try:
        return model.model_validate(body)
    except ValidationError as exc:
        raise RequestContractError(exc) from exc
