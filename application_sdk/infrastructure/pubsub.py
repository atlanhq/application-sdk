"""Pub/sub abstraction."""

from collections.abc import Awaitable
from dataclasses import dataclass, field
from typing import Any, Callable, ClassVar, Protocol

from application_sdk.errors import PUBSUB_ERROR, ErrorCode


class PubSubError(Exception):
    """Raised when pub/sub operations fail."""

    DEFAULT_ERROR_CODE: ClassVar[ErrorCode] = PUBSUB_ERROR

    def __init__(
        self,
        message: str,
        *,
        topic: str | None = None,
        operation: str | None = None,
        cause: Exception | None = None,
        error_code: ErrorCode | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.topic = topic
        self.operation = operation
        self.cause = cause
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


# Type alias for message handlers
MessageHandler = Callable[[Message], Awaitable[None]]


class Subscription(Protocol):
    """Protocol for pub/sub subscriptions."""

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

    async def subscribe(
        self,
        topic: str,
        handler: MessageHandler,
    ) -> Subscription:
        """Subscribe to a topic.

        Args:
            topic: Topic name.
            handler: Async function to handle messages.

        Returns:
            Subscription handle for unsubscribing.

        Raises:
            PubSubError: If subscription fails.
        """
        ...


def __getattr__(name: str):
    if name in ("InMemoryPubSub", "InMemorySubscription"):
        import warnings

        warnings.warn(
            f"{name} is removed in v3. Use application_sdk.testing.mocks.MockPubSub.",
            DeprecationWarning,
            stacklevel=2,
        )
        from application_sdk.testing.mocks import MockPubSub

        return MockPubSub
    raise AttributeError(name)
