"""Event handler registry for @on_event handlers.

The EventRegistry is a singleton that tracks all registered event handlers,
keyed by event_id. It follows the same singleton pattern as AppRegistry
and TaskRegistry in registry.py.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from application_sdk.app.event import EventHandlerMetadata
from application_sdk.contracts.events import EventFilter


@dataclass(frozen=True)
class RegisteredEventHandler:
    """A fully registered event handler entry.

    Combines the parent App name and Temporal workflow name with the
    underlying EventHandlerMetadata from the @on_event decorator.
    """

    app_name: str
    """Parent App name."""

    workflow_name: str
    """Derived Temporal workflow name (e.g., 'qualytics-app.handle-anomaly')."""

    handler_metadata: EventHandlerMetadata
    """The underlying event handler metadata from @on_event."""

    @property
    def event_id(self) -> str:
        """Delegate to handler_metadata.event_id."""
        return self.handler_metadata.event_id

    @property
    def topic(self) -> str:
        """Delegate to handler_metadata.topic."""
        return self.handler_metadata.topic

    @property
    def event_name(self) -> str:
        """Delegate to handler_metadata.event_name."""
        return self.handler_metadata.event_name

    @property
    def version(self) -> str:
        """Delegate to handler_metadata.version."""
        return self.handler_metadata.version

    @property
    def pre_filters(self) -> list[EventFilter]:
        """Delegate to handler_metadata.pre_filters."""
        return self.handler_metadata.pre_filters


class EventRegistry:
    """Registry for event handler discovery and registration.

    The registry is a singleton that holds all registered event handlers.
    Handlers are keyed by event_id (derived from event_name + version).
    """

    _instance: EventRegistry | None = None
    _handlers: dict[str, RegisteredEventHandler]  # event_id -> handler

    def __new__(cls) -> EventRegistry:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._handlers = {}
        return cls._instance

    @classmethod
    def get_instance(cls) -> EventRegistry:
        """Get the singleton registry instance."""
        return cls()

    @classmethod
    def reset(cls) -> None:
        """Reset the registry (primarily for testing)."""
        if cls._instance is not None:
            cls._instance._handlers = {}

    def register(
        self,
        app_name: str,
        handler_metadata: EventHandlerMetadata,
        workflow_name: str,
    ) -> RegisteredEventHandler:
        """Register an event handler.

        Args:
            app_name: The parent App name.
            handler_metadata: The event handler metadata from @on_event.
            workflow_name: The derived Temporal workflow name.

        Returns:
            The registered RegisteredEventHandler.

        Raises:
            ValueError: If event_id is already registered.
        """
        event_id = handler_metadata.event_id

        if event_id in self._handlers:
            existing = self._handlers[event_id]
            raise ValueError(
                f"Event handler for event_id '{event_id}' is already registered "
                f"by app '{existing.app_name}' (workflow '{existing.workflow_name}'). "
                f"Cannot register duplicate from app '{app_name}'."
            )

        entry = RegisteredEventHandler(
            app_name=app_name,
            workflow_name=workflow_name,
            handler_metadata=handler_metadata,
        )

        self._handlers[event_id] = entry
        return entry

    def get(self, event_id: str) -> RegisteredEventHandler | None:
        """Get a registered event handler by event_id.

        Args:
            event_id: The event identifier.

        Returns:
            The RegisteredEventHandler, or None if not found.
        """
        return self._handlers.get(event_id)

    def get_by_app(self, app_name: str) -> list[RegisteredEventHandler]:
        """Get all event handlers registered by a specific app.

        Args:
            app_name: The app name to filter by.

        Returns:
            List of RegisteredEventHandler entries for the app.
        """
        return [h for h in self._handlers.values() if h.app_name == app_name]

    def list_all(self) -> list[RegisteredEventHandler]:
        """List all registered event handlers."""
        return list(self._handlers.values())

    def generate_dapr_subscriptions(self) -> list[dict]:
        """Generate Dapr subscription configs grouped by topic.

        Returns a list of Dapr subscription dicts, one per unique topic,
        with CEL match expressions for routing events to the correct handler.

        Returns:
            List of Dapr subscription configuration dicts.
        """
        # Group handlers by topic
        by_topic: dict[str, list[RegisteredEventHandler]] = defaultdict(list)
        for handler in self._handlers.values():
            by_topic[handler.topic].append(handler)

        subscriptions: list[dict] = []
        for topic, handlers in by_topic.items():
            rules: list[dict] = []
            for handler in handlers:
                # Build the match expression
                conditions: list[str] = [
                    f"event.data.event_name == '{handler.event_name}'",
                    f"event.data.version == '{handler.version}'",
                ]

                # Append pre_filters as additional conditions
                for f in handler.pre_filters:
                    conditions.append(f"event.data.{f.path} {f.operator} '{f.value}'")

                match_expr = " && ".join(conditions)
                path = f"/events/v1/event/{handler.event_id}"

                rules.append({"match": match_expr, "path": path})

            subscriptions.append(
                {
                    "pubsubname": "eventstore",
                    "topic": topic,
                    "routes": {
                        "rules": rules,
                        "default": "/events/v1/drop",
                    },
                }
            )

        return subscriptions
