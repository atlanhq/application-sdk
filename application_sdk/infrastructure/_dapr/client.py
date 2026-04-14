"""Dapr client implementations."""

import collections.abc
import copy
import json
from enum import Enum
from typing import Any

from dapr.clients import DaprClient

from application_sdk.infrastructure.bindings import BindingError, BindingResponse
from application_sdk.infrastructure.pubsub import MessageHandler, PubSubError
from application_sdk.infrastructure.secrets import SecretNotFoundError, SecretStoreError
from application_sdk.infrastructure.state import StateStoreError
from application_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Private helpers for DaprCredentialVault
# ---------------------------------------------------------------------------


class _CredentialSource(str, Enum):
    DIRECT = "direct"
    AGENT = "agent"


class _SecretMode(Enum):
    MULTI_KEY = "multi-key"
    SINGLE_KEY = "single-key"


def _handle_single_key_secret(key: str, value: Any) -> dict[str, Any]:
    """Handle single-key secret — attempt JSON parse, fall back to {key: value}."""
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            pass
    return {key: value}


def _process_secret_data(secret_data: Any) -> dict[str, Any]:
    """Process raw Dapr secret data into a standardised dictionary.

    Converts ``ScalarMapContainer`` to ``dict`` and unwraps single-key secrets
    that contain a JSON-encoded dict.
    """
    if isinstance(secret_data, collections.abc.Mapping):
        secret_data = dict(secret_data)
    if len(secret_data) == 1:
        k, v = next(iter(secret_data.items()))
        return _handle_single_key_secret(k, v)
    return secret_data


def _resolve_credentials(
    credential_config: dict[str, Any], secret_data: dict[str, Any]
) -> dict[str, Any]:
    """Substitute secret references in *credential_config* with values from *secret_data*."""
    credentials = copy.deepcopy(credential_config)
    for key, value in list(credentials.items()):
        if isinstance(value, str) and value in secret_data:
            credentials[key] = secret_data[value]
    if "extra" in credentials and isinstance(credentials["extra"], dict):
        for key, value in list(credentials["extra"].items()):
            if isinstance(value, str) and value in secret_data:
                credentials["extra"][key] = secret_data[value]
    return credentials


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
                metadata=dict(result.binding_metadata)
                if result.binding_metadata
                else {},
            )
        except Exception as e:
            raise BindingError(
                f"Failed to invoke binding: {e}",
                binding_name=self._binding_name,
                operation=operation,
                cause=e,
            ) from e


# ---------------------------------------------------------------------------
# Dapr component registration check
# ---------------------------------------------------------------------------


def is_dapr_component_registered(component_name: str) -> bool:
    """Return ``True`` if a Dapr component with *component_name* is registered.

    Uses the Dapr metadata API.  Returns ``False`` (conservatively) when the
    metadata call fails.
    """
    try:
        with DaprClient() as client:
            metadata = client.get_metadata()
            return any(
                c.name == component_name
                for c in getattr(metadata, "registered_components", [])
            )
    except Exception:
        logger.warning(
            "Failed to read Dapr metadata for component %s; treating as unavailable",
            component_name,
            exc_info=True,
        )
        return False


# ---------------------------------------------------------------------------
# DaprCredentialVault
# ---------------------------------------------------------------------------


class DaprCredentialVault:
    """Dapr-backed credential vault for GUID-based runtime credential resolution.

    Fetches a credential config record from the upstream object store (S3 via
    Dapr binding), then resolves secret references within it via the Dapr
    secret store.
    """

    def __init__(
        self,
        client: DaprClient,
        *,
        upstream_binding_name: str | None = None,
        secret_store_name: str | None = None,
    ) -> None:
        from application_sdk.constants import (
            SECRET_STORE_NAME,
            UPSTREAM_OBJECT_STORE_NAME,
        )

        self._client = client
        self._upstream = DaprBinding(
            client, upstream_binding_name or UPSTREAM_OBJECT_STORE_NAME
        )
        self._secret_store_name: str = secret_store_name or SECRET_STORE_NAME

    async def get_credentials(self, credential_guid: str) -> dict[str, Any]:
        """Resolve the full credential dict for *credential_guid*.

        1. Fetches the credential config JSON from the upstream S3 store
           (path: ``persistent-artifacts/apps/{app}/credentials/{guid}/config.json``).
        2. Determines the secret resolution mode (multi-key or single-key).
        3. Fetches secrets from the Dapr secret store.
        4. Returns the merged credential dict.

        Raises:
            CredentialVaultError: If any step fails.
        """
        from application_sdk.infrastructure.credential_vault import CredentialVaultError

        try:
            credential_config = await self._fetch_credential_config(credential_guid)

            credential_source_str = credential_config.get(
                "credentialSource", _CredentialSource.DIRECT.value
            )
            try:
                credential_source = _CredentialSource(credential_source_str)
            except ValueError:
                credential_source = _CredentialSource.DIRECT

            secret_path = credential_config.get("secret-path")

            if credential_source == _CredentialSource.DIRECT or secret_path:
                mode = _SecretMode.MULTI_KEY
            else:
                mode = _SecretMode.SINGLE_KEY

            secret_data: dict[str, Any] = {}

            if mode == _SecretMode.MULTI_KEY:
                key_to_fetch = (
                    secret_path
                    if credential_source == _CredentialSource.AGENT
                    else credential_guid
                )
                try:
                    logger.debug("Fetching multi-key secret: %s", key_to_fetch)
                    secret_data = self._get_secret(key_to_fetch)
                except Exception:
                    logger.warning(
                        "Failed to fetch secret bundle: %s", key_to_fetch, exc_info=True
                    )
            else:
                secret_data = self._fetch_single_key_secrets(credential_config)

            if credential_source == _CredentialSource.DIRECT:
                credential_config.update(secret_data)
                return credential_config
            else:
                return _resolve_credentials(credential_config, secret_data)

        except CredentialVaultError:
            raise
        except Exception as e:
            raise CredentialVaultError(
                f"Failed to resolve credentials for {credential_guid}: {e}",
                credential_guid=credential_guid,
                cause=e,
            ) from e

    async def _fetch_credential_config(self, credential_guid: str) -> dict[str, Any]:
        """Fetch the credential config JSON from the upstream object store."""
        import os

        from application_sdk.constants import (
            APPLICATION_NAME,
            STATE_STORE_PATH_TEMPLATE,
            TEMPORARY_PATH,
        )
        from application_sdk.storage.ops import normalize_key

        raw_path = os.path.join(
            TEMPORARY_PATH,
            STATE_STORE_PATH_TEMPLATE.format(
                application_name=APPLICATION_NAME,
                state_type="credentials",
                id=credential_guid,
            ),
        )
        normalized_key = normalize_key(raw_path)

        data = json.dumps({"key": normalized_key}).encode("utf-8")
        metadata = {
            "key": normalized_key,
            "fileName": normalized_key,
            "blobName": normalized_key,
        }

        response = await self._upstream.invoke("get", data=data, metadata=metadata)

        if response.data is None:
            logger.warning(
                "No credential config found for GUID %s in upstream store",
                credential_guid,
            )
            return {}

        return json.loads(response.data)

    def _get_secret(
        self, secret_key: str, component_name: str | None = None
    ) -> dict[str, Any]:
        """Fetch and process a secret from the Dapr secret store.

        Returns ``{}`` in local-environment deployments to avoid secret store
        dependency during development.
        """
        from application_sdk.common.exc_utils import rewrap
        from application_sdk.constants import DEPLOYMENT_NAME, LOCAL_ENVIRONMENT

        if DEPLOYMENT_NAME == LOCAL_ENVIRONMENT:
            return {}

        store = component_name or self._secret_store_name
        try:
            result = self._client.get_secret(store_name=store, key=secret_key)
            return _process_secret_data(result.secret)
        except Exception as e:
            raise rewrap(e, "Failed to fetch secret (component=%s)" % store) from e

    def _fetch_single_key_secrets(
        self, credential_config: dict[str, Any]
    ) -> dict[str, Any]:
        """Fetch secrets in single-key mode — one lookup per string field value."""
        logger.debug("Single-key mode: fetching secrets per field")
        collected: dict[str, Any] = {}
        failed_lookups: list[str] = []

        def _try_fetch(label: str, value: str) -> None:
            if not value.strip():
                return
            try:
                single_secret = self._get_secret(value)
                if single_secret:
                    for k, v in single_secret.items():
                        if v is None or v == "":
                            continue
                        collected[k] = v
            except Exception as e:
                failed_lookups.append("  '%s' → '%s': %s" % (label, value, e))

        for field, value in credential_config.items():
            if isinstance(value, str):
                _try_fetch(field, value)
            elif field == "extra" and isinstance(value, dict):
                for extra_key, extra_value in value.items():
                    if isinstance(extra_value, str):
                        _try_fetch(f"extra.{extra_key}", extra_value)

        if not collected and failed_lookups:
            logger.error(
                "Single-key secret resolution failed: no secrets resolved. "
                "%d attempted, all failed:\n%s",
                len(failed_lookups),
                "\n".join(failed_lookups),
            )
        elif failed_lookups:
            logger.debug(
                "Single-key mode: resolved %d secrets, skipped %d non-secret fields",
                len(collected),
                len(failed_lookups),
            )

        return collected


def create_dapr_credential_vault(
    client: DaprClient,
    *,
    upstream_binding_name: str | None = None,
    secret_store_name: str | None = None,
) -> "DaprCredentialVault":
    """Create a Dapr-backed credential vault.

    Args:
        client: Dapr client instance.
        upstream_binding_name: Dapr binding component for the upstream S3 store.
            Defaults to the ``UPSTREAM_OBJECT_STORE_NAME`` constant.
        secret_store_name: Dapr secret store component name.
            Defaults to the ``SECRET_STORE_NAME`` constant.

    Returns:
        Configured DaprCredentialVault.
    """
    return DaprCredentialVault(
        client,
        upstream_binding_name=upstream_binding_name,
        secret_store_name=secret_store_name,
    )
