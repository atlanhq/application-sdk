"""Pub/sub abstraction.

.. deprecated:: 3.57
    The subscribe half — ``PubSub.subscribe``, ``Subscription`` and
    ``MessageHandler`` — is deprecated and will be removed in v4.0.0. The only
    production implementation, ``DaprPubSub.subscribe``, never delivered a
    message: it stored the handler and nothing called it. Declare a
    ``SubscriptionConfig`` (``application_sdk.handler.contracts``) and pass it
    to ``create_app_handler_service(subscriptions=...)``; Dapr then delivers
    to the handler through the route the SDK serves. ``publish`` is unchanged.
"""

import warnings
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, ClassVar, Protocol

from typing_extensions import deprecated

from application_sdk.errors import PUBSUB_ERROR, ErrorCode
from application_sdk.errors.leaves import DependencyUnavailableError


class PubSubError(DependencyUnavailableError):
    """Deprecated: use ``application_sdk.errors.DependencyUnavailableError`` — removed in v4.0."""

    DEFAULT_ERROR_CODE: ClassVar[ErrorCode] = PUBSUB_ERROR
    code: ClassVar[str] = "DEPENDENCY_UNAVAILABLE_PUBSUB"

    def __init__(
        self,
        message: str,
        *,
        topic: str | None = None,
        operation: str | None = None,
        cause: Exception | None = None,
        error_code: ErrorCode | None = None,
    ) -> None:
        warnings.warn(
            "PubSubError is deprecated; use application_sdk.errors.DependencyUnavailableError "
            "— will be removed in v4.0",
            DeprecationWarning,
            stacklevel=2,
        )
        DependencyUnavailableError.__init__(self, message=message, cause=cause)
        self.topic = topic
        self.operation = operation
        self._error_code = error_code

    @property
    def error_code(self) -> ErrorCode:
        return (
            self._error_code
            if self._error_code is not None
            else self.DEFAULT_ERROR_CODE
        )

    def __str__(self) -> str:
        parts = [f"[{self.error_code.code}] {self.message}"]
        if self.topic:
            parts.append(f"topic={self.topic}")
        if self.operation:
            parts.append(f"operation={self.operation}")
        if self.cause:
            parts.append(f"caused_by={type(self.cause).__name__}: {self.cause}")
        return " | ".join(parts)


@dataclass(frozen=True)
class Message:
    """A pub/sub message."""

    data: dict[str, Any]
    metadata: dict[str, str] = field(default_factory=dict)
    id: str | None = None
    topic: str | None = None


# Private: the public name ``MessageHandler`` is deprecated and served by
# ``__getattr__`` below, so it warns on access.
_MessageHandler = Callable[[Message], Awaitable[None]]

#: name -> (replacement, why). Served by ``__getattr__`` below with a warning
#: on every access (FND-4892).
_DEPRECATED_CONSTANTS: dict[str, tuple[str, str]] = {
    "MessageHandler": (
        "SubscriptionConfig.handler in application_sdk.handler.contracts",
        "it typed PubSub.subscribe handlers, which DaprPubSub never called",
    ),
}


def __getattr__(name: str) -> object:
    """Serve the deprecated ``MessageHandler`` alias with a warning (PEP 562)."""
    entry = _DEPRECATED_CONSTANTS.get(name)
    if entry is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    replacement, note = entry
    warnings.warn(
        f"{name} is deprecated; use {replacement} instead — {note}. "
        "Will be removed in v4.0.0.",
        DeprecationWarning,
        stacklevel=2,
    )
    return _MessageHandler


@deprecated(
    "Subscription is deprecated and will be removed in v4.0.0. "
    "DaprPubSub.subscribe never delivered a message to its handler; declare a "
    "SubscriptionConfig (application_sdk.handler.contracts) and pass it to "
    "create_app_handler_service(subscriptions=...) instead."
)
class Subscription(Protocol):
    """Deprecated: protocol for pub/sub subscriptions — removed in v4.0.0.

    No production implementation ever delivered a message. Use
    ``SubscriptionConfig`` from ``application_sdk.handler.contracts``.
    """

    @property
    def topic(self) -> str:
        """Topic this subscription is for."""
        ...

    @property
    def is_active(self) -> bool:
        """Whether the subscription is active."""
        ...

    async def unsubscribe(self) -> None:
        """Unsubscribe and stop receiving messages."""
        ...


class PubSub(Protocol):
    """Protocol for pub/sub messaging.

    Provides topic-based publish/subscribe messaging.
    The underlying implementation (DAPR, Kafka, etc.) is hidden.
    """

    async def publish(
        self,
        topic: str,
        data: dict[str, Any],
        *,
        metadata: dict[str, str] | None = None,
    ) -> None:
        """Publish a message to a topic.

        Args:
            topic: Topic name.
            data: Message data (must be JSON-serializable).
            metadata: Optional message metadata.

        Raises:
            PubSubError: If publish fails.
        """
        ...

    @deprecated(
        "PubSub.subscribe is deprecated and will be removed in v4.0.0. "
        "DaprPubSub.subscribe never delivered a message to its handler; declare a "
        "SubscriptionConfig (application_sdk.handler.contracts) and pass it to "
        "create_app_handler_service(subscriptions=...) instead."
    )
    async def subscribe(
        self,
        topic: str,
        handler: _MessageHandler,
    ) -> Subscription:
        """Deprecated: subscribe to a topic — removed in v4.0.0.

        ``DaprPubSub`` never delivered to the handler. Declare a
        ``SubscriptionConfig`` and pass it to
        ``create_app_handler_service(subscriptions=...)`` instead.

        Args:
            topic: Topic name.
            handler: Async function to handle messages.

        Returns:
            Subscription handle for unsubscribing.

        Raises:
            PubSubError: If subscription fails.
        """
        ...
