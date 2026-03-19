"""Mock wrappers with call-tracking for unit tests."""

from typing import Any

from application_sdk.execution.heartbeat import NoopHeartbeatController
from application_sdk.infrastructure.bindings import BindingResponse, InMemoryBinding
from application_sdk.infrastructure.pubsub import InMemoryPubSub, Message
from application_sdk.infrastructure.secrets import InMemorySecretStore
from application_sdk.infrastructure.state import InMemoryStateStore


class MockStateStore(InMemoryStateStore):
    """InMemoryStateStore with call-tracking."""

    def __init__(self) -> None:
        super().__init__()
        self._save_calls: list[tuple[str, dict[str, Any]]] = []
        self._load_calls: list[str] = []
        self._delete_calls: list[str] = []
        self._list_keys_calls: list[str] = []

    async def save(self, key: str, value: dict[str, Any]) -> None:
        self._save_calls.append((key, value))
        await super().save(key, value)

    async def load(self, key: str) -> dict[str, Any] | None:
        self._load_calls.append(key)
        return await super().load(key)

    async def delete(self, key: str) -> bool:
        self._delete_calls.append(key)
        return await super().delete(key)

    async def list_keys(self, prefix: str = "") -> list[str]:
        self._list_keys_calls.append(prefix)
        return await super().list_keys(prefix)

    def get_save_calls(self) -> list[tuple[str, dict[str, Any]]]:
        """Return all recorded save calls as (key, value) tuples."""
        return list(self._save_calls)

    def get_load_calls(self) -> list[str]:
        """Return all recorded load calls (keys)."""
        return list(self._load_calls)

    def get_delete_calls(self) -> list[str]:
        """Return all recorded delete calls (keys)."""
        return list(self._delete_calls)

    def get_list_keys_calls(self) -> list[str]:
        """Return all recorded list_keys calls (prefixes)."""
        return list(self._list_keys_calls)

    def reset_calls(self) -> None:
        """Clear call logs only (data is preserved)."""
        self._save_calls.clear()
        self._load_calls.clear()
        self._delete_calls.clear()
        self._list_keys_calls.clear()

    def reset(self) -> None:
        """Clear call logs and all stored data."""
        self.reset_calls()
        self.clear()


class MockSecretStore(InMemorySecretStore):
    """InMemorySecretStore with call-tracking."""

    def __init__(self, secrets: dict[str, str] | None = None) -> None:
        super().__init__(secrets)
        self._get_calls: list[str] = []
        self._get_optional_calls: list[str] = []

    async def get(self, name: str) -> str:
        self._get_calls.append(name)
        return await super().get(name)

    async def get_optional(self, name: str) -> str | None:
        self._get_optional_calls.append(name)
        return await super().get_optional(name)

    def get_get_calls(self) -> list[str]:
        """Return all recorded get calls (secret names)."""
        return list(self._get_calls)

    def get_get_optional_calls(self) -> list[str]:
        """Return all recorded get_optional calls (secret names)."""
        return list(self._get_optional_calls)

    def reset_calls(self) -> None:
        """Clear call logs only (secrets are preserved)."""
        self._get_calls.clear()
        self._get_optional_calls.clear()

    def reset(self) -> None:
        """Clear call logs and all stored secrets."""
        self.reset_calls()
        self.clear()


class MockPubSub(InMemoryPubSub):
    """InMemoryPubSub with call-tracking."""

    def __init__(self) -> None:
        super().__init__()
        self._publish_calls: list[tuple[str, dict[str, Any], dict[str, str]]] = []

    async def publish(
        self,
        topic: str,
        data: dict[str, Any],
        *,
        metadata: dict[str, str] | None = None,
    ) -> None:
        self._publish_calls.append((topic, data, metadata or {}))
        await super().publish(topic, data, metadata=metadata)

    def get_publish_calls(
        self, topic: str | None = None
    ) -> list[tuple[str, dict[str, Any], dict[str, str]]]:
        """Return recorded publish calls, optionally filtered by topic.

        Args:
            topic: Optional topic name to filter by.

        Returns:
            List of (topic, data, metadata) tuples.
        """
        if topic is not None:
            return [(t, d, m) for t, d, m in self._publish_calls if t == topic]
        return list(self._publish_calls)

    def get_published_messages(self, topic: str | None = None) -> list[Message]:
        """Return published Message objects, optionally filtered by topic."""
        return self.get_published(topic)

    def reset_calls(self) -> None:
        """Clear call logs only (subscriptions and published messages preserved)."""
        self._publish_calls.clear()

    def reset(self) -> None:
        """Clear call logs, subscriptions, and published messages."""
        self.reset_calls()
        self.clear()


class MockBinding(InMemoryBinding):
    """InMemoryBinding with call-tracking."""

    def __init__(self, name: str = "mock") -> None:
        super().__init__(name)
        self._invoke_calls: list[tuple[str, bytes | None, dict[str, str]]] = []

    async def invoke(
        self,
        operation: str,
        data: bytes | None = None,
        metadata: dict[str, str] | None = None,
    ) -> BindingResponse:
        self._invoke_calls.append((operation, data, metadata or {}))
        return await super().invoke(operation, data, metadata)

    def get_invoke_calls(
        self, operation: str | None = None
    ) -> list[tuple[str, bytes | None, dict[str, str]]]:
        """Return recorded invoke calls, optionally filtered by operation.

        Args:
            operation: Optional operation name to filter by.

        Returns:
            List of (operation, data, metadata) tuples.
        """
        if operation is not None:
            return [(op, d, m) for op, d, m in self._invoke_calls if op == operation]
        return list(self._invoke_calls)

    def reset_calls(self) -> None:
        """Clear call logs only (configured responses preserved)."""
        self._invoke_calls.clear()

    def reset(self) -> None:
        """Clear call logs and configured responses."""
        self.reset_calls()
        self.clear()


class MockHeartbeatController(NoopHeartbeatController):
    """NoopHeartbeatController with a public call-tracking API."""

    def get_heartbeat_calls(self) -> list[tuple[Any, ...]]:
        """Return all recorded heartbeat calls."""
        return list(self._heartbeat_calls)

    def reset_calls(self) -> None:
        """Clear recorded heartbeat calls."""
        self._heartbeat_calls.clear()
        self._details = ()
