"""Event handler decorator for defining event-triggered entry points.

The @on_event decorator marks async App methods as event-triggered entry points.
Like @task, event handlers follow the single-dataclass contract pattern:
- One Input parameter (extending Input base class)
- One Output return type (extending Output base class)

Event handlers are registered with a topic, event name, and version, and can
optionally specify pre-filters to narrow the events they respond to.

Usage::

    class MyApp(App):

        @on_event(topic="atlas_events", event_name="entity_update")
        async def handle_entity_update(self, input: MyInput) -> MyOutput:
            ...
"""

import inspect
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, TypeVar, get_type_hints, overload

from application_sdk.contracts.base import Input, Output
from application_sdk.contracts.events import EventFilter
from application_sdk.errors import CONTRACT_VALIDATION, ErrorCode

F = TypeVar("F", bound=Callable[..., Any])


class EventContractError(Exception):
    """Raised when an event handler's contract is invalid."""

    def __init__(self, message: str, *, error_code: ErrorCode | None = None) -> None:
        super().__init__(message)
        self.error_code = error_code or CONTRACT_VALIDATION

    def __str__(self) -> str:
        return f"[{self.error_code}] {super().__str__()}"


@dataclass(frozen=True)
class EventHandlerMetadata:
    """Metadata about a registered event handler.

    Stores all configuration for an event-triggered entry point,
    including routing information (topic, event_name, version),
    filtering (pre_filters), and execution parameters (retry, timeout).
    """

    topic: str
    """The pub/sub topic this handler subscribes to."""

    event_name: str
    """The event name this handler responds to."""

    version: str
    """Version of the event schema (default '1.0')."""

    func: Callable[..., Any]
    """The original function/method."""

    input_type: type[Input]
    """The Input subclass type for this handler."""

    output_type: type[Output]
    """The Output subclass type for this handler."""

    description: str = ""
    """Human-readable description."""

    pre_filters: list[EventFilter] = field(default_factory=list)
    """Filters applied before the handler is invoked."""

    retry_max_attempts: int = 3
    """Maximum retry attempts for this handler."""

    timeout_seconds: int = 600
    """Default timeout for this handler (10 minutes)."""

    @property
    def event_id(self) -> str:
        """Derive event ID from event_name and version.

        Format: {event_name}__{version_with_dots_replaced_by_underscores}

        Example: 'entity_update' version '1.0' -> 'entity_update__1_0'
        """
        version_part = self.version.replace(".", "_")
        return f"{self.event_name}__{version_part}"


def _validate_event_handler_signature(
    fn: Callable[..., Any],
) -> tuple[type[Input], type[Output]]:
    """Validate and extract Input/Output types from an event handler method.

    Event handlers must:
    - Be async functions
    - Have exactly one parameter (besides self) extending Input
    - Have a return type extending Output

    Args:
        fn: The event handler function to validate.

    Returns:
        Tuple of (input_type, output_type).

    Raises:
        EventContractError: If the signature is invalid.
    """
    fn_name = getattr(fn, "__name__", repr(fn))

    # Must be async
    if not inspect.iscoroutinefunction(fn):
        raise EventContractError(
            f"Event handler '{fn_name}' must be an async function (defined with 'async def')."
        )

    # Get function signature
    sig = inspect.signature(fn)
    params = list(sig.parameters.values())

    # Remove 'self' parameter if present (method)
    if params and params[0].name == "self":
        params = params[1:]

    # Must have exactly one parameter
    if len(params) != 1:
        raise EventContractError(
            f"Event handler '{fn_name}' must have exactly one parameter (extending Input), "
            f"got {len(params)} parameters. "
            f"Wrap multiple values in a single Input dataclass."
        )

    # Get type hints
    try:
        hints = get_type_hints(fn)
    except Exception:
        raw: dict[str, Any] = getattr(fn, "__annotations__", {})
        unresolvable = [k for k, v in raw.items() if isinstance(v, str)]
        if unresolvable:
            raise EventContractError(
                f"Event handler '{fn_name}' has unresolvable annotations for {unresolvable}."
            ) from None
        hints = raw

    # Validate input type
    param = params[0]
    input_type = hints.get(param.name)
    if input_type is None:
        raise EventContractError(
            f"Event handler '{fn_name}' parameter '{param.name}' must have a type annotation "
            f"extending Input."
        )

    if not (isinstance(input_type, type) and issubclass(input_type, Input)):
        raise EventContractError(
            f"Event handler '{fn_name}' parameter '{param.name}' must extend Input base class, "
            f"got {input_type}. Define a dataclass that extends Input."
        )

    # Validate return type
    output_type = hints.get("return")
    if output_type is None:
        raise EventContractError(
            f"Event handler '{fn_name}' must have a return type annotation extending Output."
        )

    if not (isinstance(output_type, type) and issubclass(output_type, Output)):
        raise EventContractError(
            f"Event handler '{fn_name}' return type must extend Output base class, "
            f"got {output_type}. Define a dataclass that extends Output."
        )

    return input_type, output_type


@overload
def on_event(func: F) -> F: ...


@overload
def on_event(
    func: None = None,
    *,
    topic: str = "",
    event_name: str | None = None,
    version: str = "1.0",
    pre_filters: list[EventFilter] | None = None,
    description: str = "",
    retry_max_attempts: int = 3,
    timeout_seconds: int = 600,
) -> Callable[[F], F]: ...


def on_event(
    func: F | None = None,
    *,
    topic: str = "",
    event_name: str | None = None,
    version: str = "1.0",
    pre_filters: list[EventFilter] | None = None,
    description: str = "",
    retry_max_attempts: int = 3,
    timeout_seconds: int = 600,
) -> F | Callable[[F], F]:
    """Decorator to mark a method as an event-triggered entry point.

    Event handlers follow the single-dataclass contract pattern (like Apps and tasks):
    - Exactly one Input parameter (dataclass extending Input)
    - Exactly one Output return type (dataclass extending Output)
    - Must be an async function

    Example::

        class MyApp(App):

            @on_event(topic="atlas_events", event_name="entity_update")
            async def handle_update(self, input: UpdateInput) -> UpdateOutput:
                ...

            @on_event  # bare decorator — event_name defaults to method name
            async def handle_delete(self, input: DeleteInput) -> DeleteOutput:
                ...

    Args:
        func: The function to decorate (when used without parentheses).
        topic: The pub/sub topic to subscribe to.
        event_name: The event name to respond to (defaults to function name).
        version: Event schema version (default '1.0').
        pre_filters: Filters applied before handler invocation.
        description: Human-readable description.
        retry_max_attempts: Maximum retry attempts (default 3).
        timeout_seconds: Handler timeout (default 10 minutes).

    Returns:
        The decorated function with event metadata attached.

    Raises:
        EventContractError: If the method doesn't follow the contract pattern.
    """

    def decorator(fn: F) -> F:
        resolved_event_name = event_name or getattr(fn, "__name__", repr(fn))

        # Validate signature and extract types
        input_type, output_type = _validate_event_handler_signature(fn)

        # Store metadata on the function
        fn._event_metadata = EventHandlerMetadata(  # type: ignore[attr-defined]
            topic=topic,
            event_name=resolved_event_name,
            version=version,
            func=fn,
            input_type=input_type,
            output_type=output_type,
            description=description or fn.__doc__ or "",
            pre_filters=pre_filters or [],
            retry_max_attempts=retry_max_attempts,
            timeout_seconds=timeout_seconds,
        )

        return fn

    # Support both @on_event and @on_event() syntax
    if func is not None:
        return decorator(func)
    return decorator


def is_event_handler(obj: Any) -> bool:
    """Check if an object is decorated with @on_event."""
    return hasattr(obj, "_event_metadata")


def get_event_metadata(obj: Any) -> EventHandlerMetadata | None:
    """Get event handler metadata from a decorated function."""
    return getattr(obj, "_event_metadata", None)
