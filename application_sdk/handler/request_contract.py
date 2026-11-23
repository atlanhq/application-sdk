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

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar, TypeVar

from pydantic import BaseModel as PydanticBaseModel
from pydantic import ValidationError

from application_sdk.errors.leaves import InvalidInputError

if TYPE_CHECKING:
    from fastapi import Request

ModelT = TypeVar("ModelT", bound=PydanticBaseModel)


def _contract_failure_summary(cause: ValidationError) -> str:
    """Name the fields that failed, never the values: a body is routinely a credential."""
    fields = ", ".join(
        ".".join(str(part) for part in error["loc"]) or "body"
        for error in cause.errors(include_url=False, include_input=False)
    )
    return f"Request body does not fit {cause.title}: invalid {fields}."


@dataclass(kw_only=True)
class RequestContractError(InvalidInputError):
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

    An :class:`InvalidInputError` (FND-4298), so a caller that catches
    ``AppError`` sees it as the caller's fault. ``cause`` is the pydantic
    failure, kept so the handler can name the fields; ``None`` when the body
    never reached pydantic -- unparseable JSON, or a valid JSON scalar/array
    where an object was required. ``message`` names the failing fields and
    never their values.
    """

    code: ClassVar[str] = "INVALID_INPUT_REQUEST_CONTRACT"

    # Intentional: dataclass fields define the wire-evidence schema; custom __init__ keeps the positional-cause signature callers use.
    def __init__(
        self, cause: ValidationError | None = None, *, message: str | None = None
    ) -> None:
        if cause is not None:
            message = _contract_failure_summary(cause)
        InvalidInputError.__init__(
            self,
            message=message or "Request body does not fit its contract.",
            cause=cause,
        )

    @property
    def validation_error(self) -> ValidationError | None:
        """The pydantic failure this error carries, or ``None``."""
        return self.cause if isinstance(self.cause, ValidationError) else None


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


async def read_json_object(request: Request) -> dict[str, Any]:
    """Read a request body that must be a JSON object, answering 422 if not.

    The routes normalise the body before validating, so they have to read it
    themselves -- which put ``await request.json()`` OUTSIDE the route's error
    boundary. Unparseable JSON raised a decode error and a top-level array or
    scalar raised a TypeError inside the normaliser, both escaping as an opaque
    500. That is precisely what this module exists to prevent, so the read
    belongs behind the same 422 as the validation it feeds.

    The message never echoes the body: it is caller-controlled and, on these
    routes, routinely a credential.
    """
    try:
        body = await request.json()
    except ValueError as exc:
        # ValueError, not Exception: json.JSONDecodeError is a ValueError, while
        # the body-size cap (object_routes.RequestBodyLimitMiddleware) signals
        # with RequestBodyTooLargeError, which is not a ValueError, from inside
        # the ASGI receive. It must propagate: the middleware answers 413.
        raise RequestContractError(message="Request body is not valid JSON.") from exc
    if not isinstance(body, dict):
        raise RequestContractError(
            message=(f"Request body must be a JSON object, got {type(body).__name__}.")
        )
    return body
