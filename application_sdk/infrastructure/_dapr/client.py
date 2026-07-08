"""Dapr client implementations."""

from typing import Any

import httpx
import orjson

from application_sdk.infrastructure._dapr.http import AsyncDaprClient
from application_sdk.infrastructure.bindings import BindingError, BindingResponse
from application_sdk.infrastructure.pubsub import MessageHandler, PubSubError
from application_sdk.infrastructure.secrets import (
    SecretNotFoundError,
    SecretStoreError,
    SecretStoreUnavailableError,
)
from application_sdk.infrastructure.state import StateStoreError
from application_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)


def create_dapr_secret_store(
    client: AsyncDaprClient,
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
        client: AsyncDaprClient,
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
            await self._client.save_state(
                store_name=self._store_name,
                key=key,
                value=value,
            )
        # conformance: ignore[E004] re-raises as typed StateStoreError with cause chain; traceback preserved
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
            data = await self._client.get_state(
                store_name=self._store_name,
                key=key,
            )
            if data:
                return orjson.loads(data)
            return None
        # conformance: ignore[E004] re-raises as typed StateStoreError with cause chain; traceback preserved
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
            await self._client.delete_state(
                store_name=self._store_name,
                key=key,
            )
            return True
        # conformance: ignore[E004] re-raises as typed StateStoreError with cause chain; traceback preserved
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
        from application_sdk.infrastructure._dapr._dapr_errors import (  # noqa: PLC0415 — avoid circular import on module load
            DaprListKeysUnsupportedError,
        )

        raise DaprListKeysUnsupportedError()


def is_dapr_transport_unavailable(exc: BaseException) -> bool:
    """True if *exc* is cold-start-shaped: a transport failure (connection
    refused, reset mid-handshake, read/write/close error, timeout) or a 5xx
    that survived the transport's own retries — as opposed to a 4xx or any
    other definitive, answered-and-rejected failure.

    Used by
    :meth:`~application_sdk.infrastructure._dapr.credential_vault.DaprCredentialVault._fetch_credential_config`
    (Dapr binding/object-store fetches), where a missing blob is signalled by
    a *successful* response with empty data rather than an error status, so a
    5xx reaching this predicate is unambiguously a transport/availability
    problem. The Dapr *secrets* API doesn't have that luxury — see
    :func:`classify_secret_fetch_error`, which does NOT use this predicate,
    for why a bare 5xx isn't a safe "unreachable" signal there.
    """
    if isinstance(exc, httpx.TransportError):
        return True
    return isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code >= 500


#: Dapr secrets-API JSON error body ``errorCode`` values that unambiguously
#: mean "not ready yet" — as opposed to :data:`_ERR_SECRET_GET`, which Dapr
#: also returns for a *genuinely missing* key. Verified against a live Dapr
#: sidecar (v1.17, ``secretstores.local.file``): a key that is absent from an
#: already-initialized, correctly-configured store returns HTTP 500 with
#: ``errorCode=ERR_SECRET_GET`` — the exact same status+code a still-cold
#: component can return. Only "no secret store registered at all" gets its
#: own distinct code. See ``pkg/messages/errorcodes/errorcodes.go`` and
#: ``pkg/api/http/http_test.go`` in ``dapr/dapr`` for the canonical mapping.
_ERR_SECRET_STORES_NOT_CONFIGURED = "ERR_SECRET_STORES_NOT_CONFIGURED"

#: Dapr's "failed to get the secret" code for an already-ready, correctly
#: configured store — the shape it uses for a genuinely-missing key. NOT a
#: safe signal on its own (see :data:`_ERR_SECRET_STORES_NOT_CONFIGURED`'s
#: docstring for why a bare 5xx isn't enough), but combined with a 5xx status
#: and the "not configured" code having already been ruled out, this is the
#: narrowest available "probably absent, not a real rejection" signal. A 4xx
#: (bad auth/binding/path) never carries this code and always escalates.
_ERR_SECRET_GET = "ERR_SECRET_GET"


def _dapr_secrets_error_code(exc: httpx.HTTPStatusError) -> str | None:
    """Best-effort extraction of the Dapr secrets-API error body's ``errorCode``
    (e.g. ``{"errorCode": "ERR_SECRET_GET", "message": "..."}``).

    Returns ``None`` if the response isn't that JSON shape — callers must
    treat that as "unknown", not as a positive signal either way.
    """
    try:
        body = exc.response.json()
    # conformance: ignore[E004] best-effort error-body parse; logged at DEBUG since a non-JSON body is an expected "unknown errorCode" outcome, not a failure worth warning on
    except Exception as e:
        logger.debug(
            "Could not parse Dapr secrets error body as JSON: %s", e, exc_info=True
        )
        return None
    return body.get("errorCode") if isinstance(body, dict) else None


def classify_secret_fetch_error(name: str, exc: Exception) -> SecretStoreError:
    """Classify a raw Dapr secret-fetch exception as unreachable / not-found /
    rejected.

    Raising a distinct type for the unreachable case lets callers retry a
    cold-start race without duck-typing exception text; raising
    :class:`SecretNotFoundError` for the genuinely-missing-key case (rather
    than a generic :class:`SecretStoreError`) lets every existing
    ``except SecretNotFoundError`` call site — which predates this
    function and is written against a backend-opaque contract (Dapr, AWS
    Secrets Manager, or a test double) — keep working without needing to
    know Dapr's specific error codes. Classifying here, once, instead of at
    each call site, is deliberate: those call sites (``credentials/agent.py``,
    ``credentials/resolver.py``) must stay backend-opaque per their own
    module contracts, and must not import from this private ``_dapr``
    adapter to make this distinction themselves.

    Deliberately narrower than :func:`is_dapr_transport_unavailable` for the
    5xx case: a bare 5xx status is NOT enough on its own to infer "still
    cold-starting" for the *secrets* API specifically, because Dapr also
    returns a plain 5xx (``ERR_SECRET_GET``) for a secret key that simply
    doesn't exist in an already-ready store (confirmed against a live
    sidecar — see :data:`_ERR_SECRET_STORES_NOT_CONFIGURED`'s docstring).
    Treating that as retryable would retry a permanently-missing secret for
    the full cold-start budget and then misreport it as a platform outage
    instead of "not found". A 4xx, or a 5xx that carries neither the
    "not configured" nor the "get failed" code, is a definitive rejection
    (bad auth/binding/path) — never collapsed to "not found", since that
    would silently hide a real misconfiguration.
    """
    if isinstance(exc, httpx.TransportError):
        return SecretStoreUnavailableError(name, cause=exc)
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        if status >= 500:
            code = _dapr_secrets_error_code(exc)
            if code == _ERR_SECRET_STORES_NOT_CONFIGURED:
                return SecretStoreUnavailableError(name, cause=exc)
            if code == _ERR_SECRET_GET:
                return SecretNotFoundError(name)
        # A 4xx, or a 5xx that isn't either recognized code, is a definitive
        # rejection (bad auth/binding/path) — retrying the identical request
        # would fail identically every time, so mark it explicitly
        # non-retryable rather than inheriting the optimistic
        # DependencyUnavailableError default. This is a *wire-level* retry
        # hint (Temporal/observability), independent of
        # retry_past_dapr_cold_start — which dispatches on ColdStartRaceError
        # type, not this flag.
        return SecretStoreError(
            f"Failed to get secret: {exc}",
            secret_name=name,
            cause=exc,
            retryable=False,
        )
    return SecretStoreError(f"Failed to get secret: {exc}", secret_name=name, cause=exc)


class DaprSecretStore:
    """Dapr-backed secret store implementation."""

    def __init__(
        self,
        client: AsyncDaprClient,
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
        """Get a secret via Dapr.

        Handles both Dapr response formats:

        * **Single-key** (default): ``{"secret-name": "{\"user\":\"u\",...}"}``
          — returns the JSON string value directly.
        * **Multi-key** (``multiValued: true`` in Dapr component config):
          ``{"user": "u", "pass": "p"}`` — serialises the dict as a JSON
          string so callers can ``json.loads`` it uniformly.

        This mirrors v2's ``SecretStore._process_secret_data`` which
        handled both formats transparently.
        """
        try:
            result = await self._client.get_secret(
                store_name=self._store_name,
                key=name,
            )
            if not result:
                raise SecretNotFoundError(name)
            # Single-key: the secret name is a key in the response dict.
            # The value is the raw secret string (often a JSON blob).
            if name in result:
                return result[name]
            # Multi-key: Dapr returned individual key-value pairs
            # (e.g. {"username": "real", "password": "pw"}).
            # Serialise as JSON so the caller can parse uniformly.
            return orjson.dumps(result).decode()
        except SecretNotFoundError:
            raise
        # conformance: ignore[E004] re-raises as typed SecretStore(Unavailable)Error via the shared classifier; cause chain preserved
        except Exception as e:
            raise classify_secret_fetch_error(name, e) from e

    async def get_optional(self, name: str) -> str | None:
        """Get a secret via Dapr, returning None if not found."""
        try:
            return await self.get(name)
        # conformance: ignore[E004] intentional probe: SecretNotFoundError → None is the API contract
        except SecretNotFoundError:
            # conformance: ignore[E007] intentional probe; SecretNotFoundError → None is the documented API contract for get_optional
            return None

    async def get_bulk(self, names: list[str]) -> dict[str, str]:
        """Get multiple secrets via Dapr.

        Note:
            Assumes single-key-per-secret-name convention. If a Dapr secret
            contains multiple keys (e.g. ``{"db": {"user": "u", "pass": "p"}}``),
            only the first value is returned. Use ``get()`` for multi-key secrets.
        """
        try:
            result = await self._client.get_bulk_secret(store_name=self._store_name)
            bulk: dict[str, str] = {}
            for name in names:
                if name not in result:
                    continue
                inner = result[name]
                if len(inner) > 1:
                    logger.warning(
                        "Secret '%s' has %d keys; get_bulk() returns only the first value. "
                        "Use get() for multi-key secrets.",
                        name,
                        len(inner),
                    )
                bulk[name] = next(iter(inner.values()), "")
            return bulk
        # conformance: ignore[E004] re-raises as typed SecretStoreError with cause chain; traceback preserved
        except Exception as e:
            raise SecretStoreError(
                f"Failed to get bulk secrets: {e}",
                cause=e,
            ) from e

    async def list_names(self) -> list[str]:
        """List secret names via Dapr."""
        try:
            result = await self._client.get_bulk_secret(store_name=self._store_name)
            return list(result.keys())
        # conformance: ignore[E004] re-raises as typed SecretStoreError with cause chain; traceback preserved
        except Exception as e:
            raise SecretStoreError(
                f"Failed to list secrets: {e}",
                cause=e,
            ) from e


class DaprPubSub:
    """Dapr-backed pub/sub implementation."""

    def __init__(
        self,
        client: AsyncDaprClient,
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
            await self._client.publish_event(
                pubsub_name=self._pubsub_name,
                topic=topic,
                data=orjson.dumps(data).decode(),
                metadata=metadata or {},
            )
        # conformance: ignore[E007] re-raises as typed PubSubError with cause chain; no silent swallow
        # conformance: ignore[E004] re-raises immediately as typed PubSubError; no information is discarded
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
        client: AsyncDaprClient,
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
            result = await self._client.invoke_binding(
                binding_name=self._binding_name,
                operation=operation,
                data=data or b"",
                metadata=metadata or {},
            )
            return BindingResponse(
                data=result.data if result.data else None,
                metadata=dict(result.metadata) if result.metadata else {},
            )
        # conformance: ignore[E004] re-raises as typed BindingError with cause chain; traceback preserved
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


async def is_dapr_component_registered(component_name: str) -> bool:
    """Return ``True`` if a Dapr component with *component_name* is registered.

    Uses the Dapr metadata API.  Returns ``False`` (conservatively) when the
    metadata call fails.
    """
    async with AsyncDaprClient() as client:
        try:
            metadata = await client.get_metadata()
            components = metadata.get(
                "components", metadata.get("registeredComponents", [])
            )
            return any(c.get("name") == component_name for c in components)
        except Exception:
            logger.warning(
                "Failed to read Dapr metadata for component %s; treating as unavailable",
                component_name,
                exc_info=True,
            )
            return False
