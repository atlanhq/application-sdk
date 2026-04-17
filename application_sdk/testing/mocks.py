"""Mock wrappers with call-tracking for unit tests."""

import json
import uuid
from typing import Any

from application_sdk.credentials.ref import (
    CredentialRef,
    api_key_ref,
    atlan_api_token_ref,
    atlan_oauth_client_ref,
    basic_ref,
    bearer_token_ref,
    certificate_ref,
    git_ssh_ref,
    git_token_ref,
    oauth_client_ref,
)
from application_sdk.execution.heartbeat import NoopHeartbeatController
from application_sdk.infrastructure.bindings import BindingResponse
from application_sdk.infrastructure.pubsub import Message
from application_sdk.infrastructure.secrets import SecretNotFoundError


class MockStateStore:
    """In-memory state store with call-tracking for unit tests."""

    def __init__(self) -> None:
        self._data: dict[str, dict[str, Any]] = {}
        self._save_calls: list[tuple[str, dict[str, Any]]] = []
        self._load_calls: list[str] = []
        self._delete_calls: list[str] = []
        self._list_keys_calls: list[str] = []

    async def save(self, key: str, value: dict[str, Any]) -> None:
        self._save_calls.append((key, value))
        self._data[key] = value

    async def load(self, key: str) -> dict[str, Any] | None:
        self._load_calls.append(key)
        return self._data.get(key)

    async def delete(self, key: str) -> bool:
        self._delete_calls.append(key)
        if key in self._data:
            del self._data[key]
            return True
        return False

    async def list_keys(self, prefix: str = "") -> list[str]:
        self._list_keys_calls.append(prefix)
        if not prefix:
            return list(self._data.keys())
        return [k for k in self._data if k.startswith(prefix)]

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
        self._data.clear()

    def clear(self) -> None:
        """Clear all stored data."""
        self._data.clear()


class MockSecretStore:
    """In-memory secret store with call-tracking for unit tests."""

    def __init__(self, secrets: dict[str, str] | None = None) -> None:
        self._secrets: dict[str, str] = secrets or {}
        self._get_calls: list[str] = []
        self._get_optional_calls: list[str] = []

    async def get(self, name: str) -> str:
        self._get_calls.append(name)
        if name not in self._secrets:
            raise SecretNotFoundError(name)
        return self._secrets[name]

    async def get_optional(self, name: str) -> str | None:
        self._get_optional_calls.append(name)
        return self._secrets.get(name)

    async def get_bulk(self, names: list[str]) -> dict[str, str]:
        """Get multiple secrets."""
        return {n: self._secrets[n] for n in names if n in self._secrets}

    async def list_names(self) -> list[str]:
        """List available secret names."""
        return list(self._secrets.keys())

    def set(self, name: str, value: str) -> None:
        """Set a secret (for test setup)."""
        self._secrets[name] = value

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
        self._secrets.clear()

    def clear(self) -> None:
        """Clear all secrets."""
        self._secrets.clear()


class _MockSubscription:
    """In-memory subscription for MockPubSub."""

    def __init__(self, topic: str, pubsub: "MockPubSub") -> None:
        self._topic = topic
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
        self._pubsub._remove_subscription(self._topic, self)


class MockPubSub:
    """In-memory pub/sub with call-tracking for unit tests."""

    def __init__(self) -> None:
        from collections.abc import Awaitable
        from typing import Callable

        self._subscriptions: dict[
            str, list[tuple[Callable[[Message], Awaitable[None]], _MockSubscription]]
        ] = {}
        self._published: list[Message] = []
        self._publish_calls: list[tuple[str, dict[str, Any], dict[str, str]]] = []

    async def publish(
        self,
        topic: str,
        data: dict[str, Any],
        *,
        metadata: dict[str, str] | None = None,
    ) -> None:
        self._publish_calls.append((topic, data, metadata or {}))
        message = Message(
            data=data,
            metadata=metadata or {},
            id=str(uuid.uuid4()),
            topic=topic,
        )
        self._published.append(message)

        # Deliver to subscribers
        if topic in self._subscriptions:
            for handler, subscription in self._subscriptions[topic]:
                if subscription.is_active:
                    await handler(message)

    async def subscribe(
        self,
        topic: str,
        handler: Any,
    ) -> _MockSubscription:
        """Subscribe to a topic."""
        if topic not in self._subscriptions:
            self._subscriptions[topic] = []

        subscription = _MockSubscription(topic, self)
        self._subscriptions[topic].append((handler, subscription))
        return subscription

    def _remove_subscription(
        self,
        topic: str,
        subscription: _MockSubscription,
    ) -> None:
        """Remove a subscription (called by _MockSubscription)."""
        if topic in self._subscriptions:
            self._subscriptions[topic] = [
                (h, s) for h, s in self._subscriptions[topic] if s != subscription
            ]

    def get_published(self, topic: str | None = None) -> list[Message]:
        """Get published messages (for testing).

        Args:
            topic: Optional topic filter.

        Returns:
            List of published messages.
        """
        if topic:
            return [m for m in self._published if m.topic == topic]
        return list(self._published)

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
        self._subscriptions.clear()
        self._published.clear()

    def clear(self) -> None:
        """Clear all subscriptions and published messages."""
        self._subscriptions.clear()
        self._published.clear()


class MockBinding:
    """In-memory binding with call-tracking for unit tests."""

    def __init__(self, name: str = "mock") -> None:
        self._name = name
        self._invocations: list[tuple[str, bytes | None, dict[str, str]]] = []
        self._responses: dict[str, BindingResponse] = {}
        self._invoke_calls: list[tuple[str, bytes | None, dict[str, str]]] = []

    @property
    def name(self) -> str:
        """Name of this binding."""
        return self._name

    def set_response(self, operation: str, response: BindingResponse) -> None:
        """Configure a response for a specific operation."""
        self._responses[operation] = response

    async def invoke(
        self,
        operation: str,
        data: bytes | None = None,
        metadata: dict[str, str] | None = None,
    ) -> BindingResponse:
        self._invocations.append((operation, data, metadata or {}))
        self._invoke_calls.append((operation, data, metadata or {}))
        return self._responses.get(operation, BindingResponse())

    def get_invocations(
        self, operation: str | None = None
    ) -> list[tuple[str, bytes | None, dict[str, str]]]:
        """Get recorded invocations, optionally filtered by operation."""
        if operation is not None:
            return [(op, d, m) for op, d, m in self._invocations if op == operation]
        return list(self._invocations)

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
        self._invocations.clear()
        self._invoke_calls.clear()

    def reset(self) -> None:
        """Clear call logs and configured responses."""
        self.reset_calls()
        self._responses.clear()

    def clear(self) -> None:
        """Clear all recorded invocations and configured responses."""
        self._invocations.clear()
        self._invoke_calls.clear()
        self._responses.clear()


class MockHeartbeatController(NoopHeartbeatController):
    """NoopHeartbeatController with a public call-tracking API."""

    def get_heartbeat_calls(self) -> list[tuple[Any, ...]]:
        """Return all recorded heartbeat calls."""
        return list(self._heartbeat_calls)

    def reset_calls(self) -> None:
        """Clear recorded heartbeat calls."""
        self._heartbeat_calls.clear()
        self._details = ()


class MockCredentialStore:
    """In-memory credential store for unit tests.

    Serializes credential data as JSON and stores it via ``MockSecretStore``.
    Each ``add_*`` method returns a ``CredentialRef`` that can be passed to a
    ``CredentialResolver`` or ``AppContext.resolve_credential()``.

    Usage::

        store = MockCredentialStore()
        ref = store.add_api_key("my-service", api_key="secret123")

        # Inject the backing store into a CredentialResolver or AppContext
        resolver = CredentialResolver(store.secret_store)
        cred = await resolver.resolve(ref)
        assert isinstance(cred, ApiKeyCredential)
    """

    def __init__(self) -> None:
        self._store = MockSecretStore()
        self._refs: dict[str, CredentialRef] = {}

    @property
    def secret_store(self) -> MockSecretStore:
        """Return the backing MockSecretStore for injection into resolvers."""
        return self._store

    def add_api_key(
        self,
        name: str,
        api_key: str,
        *,
        header_name: str = "X-API-Key",
        prefix: str = "",
    ) -> CredentialRef:
        """Store an API key credential and return its CredentialRef."""
        data = {
            "type": "api_key",
            "api_key": api_key,
            "header_name": header_name,
            "prefix": prefix,
        }
        return self._store_and_ref(name, data, api_key_ref(name))

    def add_basic(self, name: str, username: str, password: str) -> CredentialRef:
        """Store a basic (username/password) credential and return its CredentialRef."""
        data = {"type": "basic", "username": username, "password": password}
        return self._store_and_ref(name, data, basic_ref(name))

    def add_bearer_token(
        self,
        name: str,
        token: str,
        *,
        expires_at: str = "",
    ) -> CredentialRef:
        """Store a bearer token credential and return its CredentialRef."""
        data = {"type": "bearer_token", "token": token, "expires_at": expires_at}
        return self._store_and_ref(name, data, bearer_token_ref(name))

    def add_oauth_client(
        self,
        name: str,
        client_id: str,
        client_secret: str,
        token_url: str,
        *,
        scopes: list[str] | None = None,
        access_token: str = "",
        refresh_token: str = "",
        expires_at: str = "",
    ) -> CredentialRef:
        """Store an OAuth client credential and return its CredentialRef."""
        data: dict[str, Any] = {
            "type": "oauth_client",
            "client_id": client_id,
            "client_secret": client_secret,
            "token_url": token_url,
            "scopes": scopes or [],
            "access_token": access_token,
            "refresh_token": refresh_token,
            "expires_at": expires_at,
        }
        return self._store_and_ref(name, data, oauth_client_ref(name))

    def add_certificate(
        self,
        name: str,
        *,
        cert_data: str = "",
        key_data: str = "",
        ca_data: str = "",
        passphrase: str = "",
    ) -> CredentialRef:
        """Store a certificate credential and return its CredentialRef."""
        data = {
            "type": "certificate",
            "cert_data": cert_data,
            "key_data": key_data,
            "ca_data": ca_data,
            "passphrase": passphrase,
        }
        return self._store_and_ref(name, data, certificate_ref(name))

    def add_git_ssh(
        self,
        name: str,
        key_data: str,
        *,
        passphrase: str = "",
    ) -> CredentialRef:
        """Store a Git SSH credential and return its CredentialRef."""
        data = {
            "type": "git_ssh",
            "key_data": key_data,
            "passphrase": passphrase,
        }
        return self._store_and_ref(name, data, git_ssh_ref(name))

    def add_git_token(self, name: str, token: str) -> CredentialRef:
        """Store a Git token credential and return its CredentialRef."""
        data = {"type": "git_token", "token": token}
        return self._store_and_ref(name, data, git_token_ref(name))

    def add_atlan_api_token(
        self, name: str, token: str, base_url: str
    ) -> CredentialRef:
        """Store an Atlan API token credential and return its CredentialRef."""
        data = {"type": "atlan_api_token", "token": token, "base_url": base_url}
        return self._store_and_ref(name, data, atlan_api_token_ref(name))

    def add_atlan_oauth_client(
        self,
        name: str,
        client_id: str,
        client_secret: str,
        base_url: str,
        *,
        token_url: str = "",
        scopes: list[str] | None = None,
        access_token: str = "",
        refresh_token: str = "",
        expires_at: str = "",
    ) -> CredentialRef:
        """Store an Atlan OAuth client credential and return its CredentialRef."""
        data: dict[str, Any] = {
            "type": "atlan_oauth_client",
            "client_id": client_id,
            "client_secret": client_secret,
            "base_url": base_url,
            "token_url": token_url,
            "scopes": scopes or [],
            "access_token": access_token,
            "refresh_token": refresh_token,
            "expires_at": expires_at,
        }
        return self._store_and_ref(name, data, atlan_oauth_client_ref(name))

    def add_raw(
        self, name: str, credential_type: str, data: dict[str, Any]
    ) -> CredentialRef:
        """Store an arbitrary credential dict and return its CredentialRef."""
        payload = {"type": credential_type, **data}
        ref = CredentialRef(name=name, credential_type=credential_type)
        return self._store_and_ref(name, payload, ref)

    def _store_and_ref(
        self, name: str, data: dict[str, Any], ref: CredentialRef
    ) -> CredentialRef:
        self._store.set(name, json.dumps(data))
        self._refs[name] = ref
        return ref
