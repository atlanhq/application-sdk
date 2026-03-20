"""Dapr client implementations."""

import json
from typing import Any

from dapr.clients import DaprClient

from application_sdk.infrastructure.bindings import BindingError, BindingResponse
from application_sdk.infrastructure.pubsub import MessageHandler, PubSubError
from application_sdk.infrastructure.secrets import SecretNotFoundError, SecretStoreError
from application_sdk.infrastructure.state import StateStoreError


def create_dapr_client() -> DaprClient:
    """Create a Dapr client.

    Returns:
        Configured Dapr client.
    """
    return DaprClient()


def create_dapr_secret_store(
    client: DaprClient,
    store_name: str = "secretstore",
) -> "DaprSecretStore":
    """Create a Dapr-backed secret store.

    Args:
        client: Dapr client instance.
        store_name: Name of the Dapr secret store component.

    Returns:
        Configured DaprSecretStore.
    """
    return DaprSecretStore(client, store_name=store_name)


class DaprStateStore:
    """Dapr-backed state store implementation."""

    def __init__(
        self,
        client: DaprClient,
        store_name: str = "statestore",
    ) -> None:
        """Initialize the Dapr state store.

        Args:
            client: Dapr client instance.
            store_name: Name of the Dapr state store component.
        """
        self._client = client
        self._store_name = store_name

    async def save(self, key: str, value: dict[str, Any]) -> None:
        """Save state via Dapr."""
        try:
            self._client.save_state(
                store_name=self._store_name,
                key=key,
                value=json.dumps(value),
            )
        except Exception as e:
            raise StateStoreError(
                f"Failed to save state: {e}",
                key=key,
                operation="save",
                cause=e,
            ) from e

    async def load(self, key: str) -> dict[str, Any] | None:
        """Load state via Dapr."""
        try:
            result = self._client.get_state(
                store_name=self._store_name,
                key=key,
            )
            if result.data:
                return json.loads(result.data)
            return None
        except Exception as e:
            raise StateStoreError(
                f"Failed to load state: {e}",
                key=key,
                operation="load",
                cause=e,
            ) from e

    async def delete(self, key: str) -> bool:
        """Delete state via Dapr."""
        try:
            self._client.delete_state(
                store_name=self._store_name,
                key=key,
            )
            return True
        except Exception as e:
            raise StateStoreError(
                f"Failed to delete state: {e}",
                key=key,
                operation="delete",
                cause=e,
            ) from e

    async def list_keys(self, prefix: str = "") -> list[str]:
        """List keys via Dapr.

        Note: Not all Dapr state stores support listing keys.
        This may raise NotImplementedError for some backends.
        """
        # Dapr doesn't have a native list operation for all stores
        # This would need to be implemented per-backend
        raise NotImplementedError(
            "Key listing is not supported by all Dapr state stores"
        )


class DaprSecretStore:
    """Dapr-backed secret store implementation."""

    def __init__(
        self,
        client: DaprClient,
        store_name: str = "secretstore",
    ) -> None:
        """Initialize the Dapr secret store.

        Args:
            client: Dapr client instance.
            store_name: Name of the Dapr secret store component.
        """
        self._client = client
        self._store_name = store_name

    async def get(self, name: str) -> str:
        """Get a secret via Dapr."""
        try:
            result = self._client.get_secret(
                store_name=self._store_name,
                key=name,
            )
            if name in result.secret:
                return result.secret[name]
            raise SecretNotFoundError(name)
        except SecretNotFoundError:
            raise
        except Exception as e:
            raise SecretStoreError(
                f"Failed to get secret: {e}",
                secret_name=name,
                cause=e,
            ) from e

    async def get_optional(self, name: str) -> str | None:
        """Get a secret via Dapr, returning None if not found."""
        try:
            return await self.get(name)
        except SecretNotFoundError:
            return None

    async def get_bulk(self, names: list[str]) -> dict[str, str]:
        """Get multiple secrets via Dapr."""
        try:
            result = self._client.get_bulk_secret(store_name=self._store_name)
            return {
                name: result.secrets.get(name, {}).get(name, "")
                for name in names
                if name in result.secrets
            }
        except Exception as e:
            raise SecretStoreError(
                f"Failed to get bulk secrets: {e}",
                cause=e,
            ) from e

    async def list_names(self) -> list[str]:
        """List secret names via Dapr."""
        try:
            result = self._client.get_bulk_secret(store_name=self._store_name)
            return list(result.secrets.keys())
        except Exception as e:
            raise SecretStoreError(
                f"Failed to list secrets: {e}",
                cause=e,
            ) from e


class DaprPubSub:
    """Dapr-backed pub/sub implementation."""

    def __init__(
        self,
        client: DaprClient,
        pubsub_name: str = "pubsub",
    ) -> None:
        """Initialize the Dapr pub/sub.

        Args:
            client: Dapr client instance.
            pubsub_name: Name of the Dapr pub/sub component.
        """
        self._client = client
        self._pubsub_name = pubsub_name

    async def publish(
        self,
        topic: str,
        data: dict[str, Any],
        *,
        metadata: dict[str, str] | None = None,
    ) -> None:
        """Publish a message via Dapr."""
        try:
            self._client.publish_event(
                pubsub_name=self._pubsub_name,
                topic_name=topic,
                data=json.dumps(data),
                data_content_type="application/json",
                publish_metadata=metadata or {},
            )
        except Exception as e:
            raise PubSubError(
                f"Failed to publish message: {e}",
                topic=topic,
                operation="publish",
                cause=e,
            ) from e

    async def subscribe(
        self,
        topic: str,
        handler: MessageHandler,
    ) -> "DaprSubscription":
        """Subscribe to a topic via Dapr.

        Note: Dapr subscriptions are typically configured declaratively
        or via the Dapr app callback. This method registers the handler
        but actual subscription setup depends on Dapr configuration.
        """
        # In a real implementation, this would integrate with Dapr's
        # subscription mechanism (either programmatic or declarative)
        return DaprSubscription(topic, handler, self)


class DaprSubscription:
    """Dapr subscription handle."""

    def __init__(
        self,
        topic: str,
        handler: MessageHandler,
        pubsub: DaprPubSub,
    ) -> None:
        self._topic = topic
        self._handler = handler
        self._pubsub = pubsub
        self._active = True

    @property
    def topic(self) -> str:
        return self._topic

    @property
    def is_active(self) -> bool:
        return self._active

    async def unsubscribe(self) -> None:
        """Unsubscribe from the topic."""
        self._active = False


class DaprBinding:
    """Dapr-backed binding implementation."""

    def __init__(
        self,
        client: DaprClient,
        binding_name: str,
    ) -> None:
        """Initialize the Dapr binding.

        Args:
            client: Dapr client instance.
            binding_name: Name of the Dapr binding component.
        """
        self._client = client
        self._binding_name = binding_name

    @property
    def name(self) -> str:
        return self._binding_name

    async def invoke(
        self,
        operation: str,
        data: bytes | None = None,
        metadata: dict[str, str] | None = None,
    ) -> BindingResponse:
        """Invoke the binding via Dapr."""
        try:
            result = self._client.invoke_binding(
                binding_name=self._binding_name,
                operation=operation,
                data=data or b"",
                binding_metadata=metadata or {},
            )
            return BindingResponse(
                data=result.data if result.data else None,
                metadata=dict(result.binding_metadata) if result.binding_metadata else {},
            )
        except Exception as e:
            raise BindingError(
                f"Failed to invoke binding: {e}",
                binding_name=self._binding_name,
                operation=operation,
                cause=e,
            ) from e
