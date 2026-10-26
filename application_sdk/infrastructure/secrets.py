"""Secrets management abstraction."""

import os
import warnings
from dataclasses import dataclass
from typing import Any, ClassVar, Protocol

# Defined in the api distribution's light module so the handler can raise it on
# the API host; re-exported here, the same class.
from application_sdk._context_errors import SecretStoreNotConfiguredError  # noqa: F401
from application_sdk.errors import SECRET_NOT_FOUND, SECRET_STORE_ERROR, ErrorCode
from application_sdk.errors.categories import Audience
from application_sdk.errors.leaves import (  # noqa: F401 — PreconditionError re-exported for existing importers
    ColdStartRaceError,
    DaprSidecarUnreachableError,
    DependencyUnavailableError,
    NotFoundError,
    PreconditionError,
)
from application_sdk.infrastructure._secret_utils import process_secret_data
from application_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)


async def get_deployment_secret(key: str, *, raise_on_error: bool | None = None) -> Any:
    """Get a specific key from deployment configuration in the deployment secret store.

    Looks *key* up in the multi-key deployment bundle
    (``DEPLOYMENT_SECRET_PATH``) first, then as a secret of its own.

    With ``raise_on_error=True`` the fetch retries a cold Dapr sidecar via
    :func:`~application_sdk.infrastructure.retry_past_dapr_cold_start` and
    every failure raises a typed error, so a caller can tell "the key is not
    there" from "the store did not answer" — a ``None`` for both is how a
    store outage became a non-retryable "grant access" error or an empty
    password sent to the source.

    Without it the legacy contract holds: one attempt, no retry, and
    ``None`` for a local (non-Dapr) environment, a missing key, or any
    error. Omitting ``raise_on_error`` is deprecated; the default flips to
    ``True`` in v4.0. Pass ``False`` to keep ``None`` explicitly.

    Args:
        key: The key to look up inside the deployment secret.
        raise_on_error: ``True`` to raise on failure, ``False`` to return
            ``None``. ``None`` (the default) behaves as ``False`` and emits a
            ``DeprecationWarning``.

    Returns:
        The value for *key*. ``None`` on any failure unless
        ``raise_on_error`` is ``True``.

    Raises:
        SecretStoreNotConfiguredError: ``raise_on_error`` and the process
            runs in the local environment, which has no deployment store.
        SecretNotFoundError: ``raise_on_error`` and *key* is in neither the
            bundle nor the store.
        SecretStoreUnreachableError: ``raise_on_error`` and the sidecar
            stayed unreachable for the whole cold-start budget.
        SecretStoreError: ``raise_on_error`` and the store failed or
            rejected the request; ``cause`` carries the underlying error.
    """
    if raise_on_error is None:
        warnings.warn(
            "get_deployment_secret() without raise_on_error is deprecated: it "
            "returns None for a store failure as well as a missing key. Pass "
            "raise_on_error=True for a typed SecretStoreError with cold-start "
            "retry, or raise_on_error=False to keep None — the default flips "
            "to True in v4.0.",
            DeprecationWarning,
            stacklevel=2,
        )

    from application_sdk.constants import (  # noqa: PLC0415 — cold path: only on secret resolution
        DEPLOYMENT_NAME,
        DEPLOYMENT_SECRET_STORE_NAME,
        LOCAL_ENVIRONMENT,
    )

    if not raise_on_error:
        if DEPLOYMENT_NAME == LOCAL_ENVIRONMENT:
            return None
        try:
            return await _fetch_deployment_secret(key)
        except SecretNotFoundError:
            # conformance: ignore[E007] legacy contract: a missing key is None
            return None
        except Exception:
            logger.error(
                "Failed to fetch deployment config key: %s", key, exc_info=True
            )
            return None

    if DEPLOYMENT_NAME == LOCAL_ENVIRONMENT:
        raise SecretStoreNotConfiguredError(
            message="No deployment secret store in the local environment",
            resource=DEPLOYMENT_SECRET_STORE_NAME,
        )

    from application_sdk.infrastructure._dapr.http import (  # noqa: PLC0415 — circular: infrastructure/__init__.py loads sibling modules
        retry_past_dapr_cold_start,
    )

    try:
        return await retry_past_dapr_cold_start(
            lambda: _fetch_deployment_secret(key),
            description=f"Deployment secret fetch for '{key}'",
            component=DEPLOYMENT_SECRET_STORE_NAME,
        )
    except DaprSidecarUnreachableError as exc:
        raise SecretStoreUnreachableError(
            key,
            component=exc.component,
            attempts=exc.attempts,
            elapsed_seconds=exc.elapsed_seconds,
        ) from None


async def _fetch_deployment_secret(key: str) -> Any:
    """One attempt at *key*: the multi-key bundle, then a single-key lookup.

    An empty value counts as absent at either step: the ``local.env`` store
    (``components/deployment-secrets.yaml``) answers an unset variable with
    ``""`` rather than an error, and an empty credential is never usable.

    Raises:
        SecretNotFoundError: *key* is in neither the bundle nor the store.
        SecretStoreError: The store failed or was unreachable (the
            :class:`SecretStoreUnavailableError` subtype for the latter, so
            the cold-start retry picks it up), or the Dapr client could not
            be constructed (non-retryable: a configuration fault).
    """
    from application_sdk.constants import (  # noqa: PLC0415 — cold path: only on secret resolution
        DEPLOYMENT_SECRET_PATH,
        DEPLOYMENT_SECRET_STORE_NAME,
    )
    from application_sdk.infrastructure._dapr.client import (  # noqa: PLC0415 — circular: _dapr/client.py imports this module
        classify_secret_fetch_error,
    )
    from application_sdk.infrastructure._dapr.http import (  # noqa: PLC0415 — circular: infrastructure/__init__.py loads sibling modules
        AsyncDaprClient,
    )

    try:
        client = AsyncDaprClient()
    # conformance: ignore[E004] re-raised as a typed SecretStoreError; cause chain preserved
    except Exception as exc:
        raise SecretStoreError(
            f"Could not create the Dapr client for the deployment secret store: {exc}",
            secret_name=key,
            cause=exc,
            retryable=False,
        ) from exc
    try:
        try:
            result = await client.get_secret(
                DEPLOYMENT_SECRET_STORE_NAME, DEPLOYMENT_SECRET_PATH
            )
        # conformance: ignore[E004] classified and re-raised; only a missing bundle falls through to the single-key lookup
        except Exception as exc:
            error = classify_secret_fetch_error(DEPLOYMENT_SECRET_PATH, exc)
            if not isinstance(error, SecretNotFoundError):
                raise error from exc
        else:
            secret_data = process_secret_data(result)
            if _is_present(secret_data.get(key)):
                return secret_data[key]

        logger.debug("Multi-key bundle lookup missed; trying single-key: %s", key)
        try:
            result = await client.get_secret(DEPLOYMENT_SECRET_STORE_NAME, key)
        # conformance: ignore[E004] re-raised as the classified SecretStoreError; cause chain preserved
        except Exception as exc:
            raise classify_secret_fetch_error(key, exc) from exc

        single_data = process_secret_data(result)
        if key in single_data:
            if _is_present(single_data[key]):
                return single_data[key]
            raise SecretNotFoundError(key)
        # A secret stored under *key* whose one entry has another name (a
        # Kubernetes secret's sole data key) is still the value of *key*.
        # Read that from the raw response: process_secret_data unwraps a
        # single JSON-document entry into its fields, and the sole field of
        # that document is not the secret named *key*.
        raw = dict(result)
        if len(raw) == 1 and _is_present(sole := next(iter(raw.values()))):
            return sole
        raise SecretNotFoundError(key)
    finally:
        await client.close()


def _is_present(value: Any) -> bool:
    """Whether a store answered with a usable value (``None`` and ``""`` are absent)."""
    return value is not None and value != ""


@dataclass(kw_only=True)
class SecretStoreError(DependencyUnavailableError):
    """Generic secret-store failure (category=DEPENDENCY_UNAVAILABLE).

    Use ``SecretNotFoundError`` when the secret key is absent; use this class
    for connectivity, permission, or other store-level failures.
    """

    secret_name: str | None = None

    DEFAULT_ERROR_CODE: ClassVar[ErrorCode] = SECRET_STORE_ERROR
    code: ClassVar[str] = "DEPENDENCY_UNAVAILABLE_SECRET_STORE"

    # Intentional: dataclass fields define the wire-evidence schema; custom __init__ preserves positional-message compat.
    def __init__(
        self,
        message: str,
        *,
        secret_name: str | None = None,
        cause: Exception | None = None,
        error_code: ErrorCode | None = None,
        retryable: bool | None = None,
    ) -> None:
        DependencyUnavailableError.__init__(
            self, message=message, cause=cause, retryable=retryable
        )
        self.secret_name = secret_name
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
        if self.secret_name:
            parts.append(f"secret={self.secret_name}")
        if self.cause:
            parts.append(f"caused_by={type(self.cause).__name__}: {self.cause}")
        return " | ".join(parts)


class SecretStoreUnavailableError(SecretStoreError, ColdStartRaceError):
    """The secret store / Dapr sidecar was *unreachable* — a transport failure
    (connection refused, reset mid-handshake, read/write/close error, timeout)
    or the Dapr secrets-API ``ERR_SECRET_STORES_NOT_CONFIGURED`` response (no
    secret store component registered yet) — as opposed to the store
    answering and rejecting or failing the request (bad auth / binding / path,
    a throttled or access-denied backend — a plain :class:`SecretStoreError` —
    or a genuinely-missing key — :class:`SecretNotFoundError`).

    Classified at the infrastructure layer (``_dapr/client.py``), where the
    httpx exception family and the Dapr error body are visible, so callers
    can retry a cold-start race against a not-yet-ready sidecar without
    duck-typing exception text. Remains a ``SecretStoreError`` so ``except
    SecretStoreError`` still catches it, and a :class:`ColdStartRaceError` so
    :func:`~application_sdk.infrastructure.retry_past_dapr_cold_start`
    retries it.
    """

    code: ClassVar[str] = "DEPENDENCY_UNAVAILABLE_SECRET_STORE_UNREACHABLE"

    def __init__(self, secret_name: str, *, cause: Exception | None = None) -> None:
        super().__init__(
            f"Secret store unavailable while fetching secret '{secret_name}'",
            secret_name=secret_name,
            cause=cause,
        )


@dataclass(kw_only=True)
class SecretStoreUnreachableError(SecretStoreError, DaprSidecarUnreachableError):
    """Terminal counterpart of :class:`SecretStoreUnavailableError`: the secret
    store's Dapr sidecar stayed unreachable for the *entire* cold-start budget —
    "waited the whole budget, done waiting", not "not reachable yet".

    Raised (hash-labelled, cause-free) at the secret-resolution catch sites when
    :func:`~application_sdk.infrastructure.retry_past_dapr_cold_start` gives up
    with a :class:`~application_sdk.errors.leaves.DaprSidecarUnreachableError`.
    Stays a ``SecretStoreError`` (so ``except SecretStoreError:`` still catches
    it) and — via ``DaprSidecarUnreachableError`` — a ``ColdStartRaceError`` (so
    the probe aggregators' ``isinstance(result, ColdStartRaceError)`` branch
    still routes it), while its distinct type name and ``code`` keep a persistent
    outage distinguishable from a still-cold race end-to-end. Shares
    ``DaprSidecarUnreachableError``'s ``code`` so the terminal signal is one
    dashboard filter across domains; the *type name* carries the domain.

    Secret-safe: the ref-key stays hashed in ``secret_name`` and no ``cause`` is
    attached (the underlying httpx error can re-embed the ref-key via a
    percent-encoded URL). Carries only the secret-free ``component`` /
    ``attempts`` / ``elapsed_seconds`` diagnostics. ``@dataclass`` so those three
    reach the wire ``evidence`` via ``to_failure_details()`` (a plain-attribute
    class would drop them); the custom ``__init__`` is preserved because
    ``@dataclass`` skips init generation when the class defines its own.

    Inherits the deliberate wire-retryable default from
    ``DaprSidecarUnreachableError``: this type names the outage, it does not stop
    the retry (an activity-level retry may still recover on a healthy worker).
    """

    code: ClassVar[str] = "DEPENDENCY_UNAVAILABLE_SIDECAR_UNREACHABLE"

    def __init__(
        self,
        secret_name: str,
        *,
        component: str | None = None,
        attempts: int | None = None,
        elapsed_seconds: float | None = None,
    ) -> None:
        SecretStoreError.__init__(
            self,
            f"Secret store unreachable while fetching secret '{secret_name}'",
            secret_name=secret_name,
        )
        self.component = component
        self.attempts = attempts
        self.elapsed_seconds = elapsed_seconds


@dataclass(kw_only=True)
class AgentSecretReadError(PreconditionError, SecretStoreError):
    """The store answered a v2 ``secretKeyRef`` read with a failure (throttled,
    access denied, bad binding) rather than "not found" or an outage.

    ``PreconditionError`` comes first so the category is PRECONDITION and the
    audience USER: the customer must fix the store or its grants. Still a
    ``SecretStoreError`` so ``except SecretStoreError:`` catches it. Keyed by
    the payload *field* name — never the secret name, which encodes the
    customer's store layout. ``retryable`` is carried over from the source.
    """

    code: ClassVar[str] = "PRECONDITION_AGENT_SECRET_READ"

    def __init__(self, field: str, *, retryable: bool | None = None) -> None:
        SecretStoreError.__init__(
            self,
            f"Secret store failed to return the secret for agent_json field "
            f"'{field}'. Check that your secret store is healthy and that this "
            "app is allowed to read it.",
            secret_name=field,
            retryable=retryable,
        )
        self.resource = "agent_json_secret"
        self.suggested_action = (
            "Check that the secret named by this field's secretKeyRef exists and "
            "that the SDR's secret-store identity can read it."
        )


@dataclass(kw_only=True)
class SecretNotFoundError(NotFoundError, SecretStoreError):
    """The requested secret key was not found in the secret store.

    Categorical parent is ``NotFoundError`` (category=NOT_FOUND); domain
    parent is ``SecretStoreError`` so ``except SecretStoreError:`` still catches.
    """

    DEFAULT_ERROR_CODE: ClassVar[ErrorCode] = SECRET_NOT_FOUND
    code: ClassVar[str] = "NOT_FOUND_SECRET"
    default_retryable: ClassVar[bool] = False
    audience: ClassVar[Audience] = Audience.USER

    def __init__(self, secret_name: str) -> None:
        NotFoundError.__init__(self, message=f"Secret '{secret_name}' not found")
        self.secret_name = secret_name
        self._error_code = SECRET_NOT_FOUND

    @property
    def error_code(self) -> ErrorCode:
        return self._error_code

    def __str__(self) -> str:
        parts = [f"[{self.error_code.code}] {self.message}"]
        if self.secret_name:
            parts.append(f"secret={self.secret_name}")
        return " | ".join(parts)


class SecretStore(Protocol):
    """Protocol for secrets management.

    Secret stores provide secure access to credentials and sensitive data.
    The underlying implementation (DAPR, Vault, etc.) is hidden.
    """

    async def get(self, name: str) -> str:
        """Get a secret by name.

        Args:
            name: Secret name.

        Returns:
            The secret value.

        Raises:
            SecretNotFoundError: If secret not found.
            SecretStoreError: If retrieval fails.
        """
        ...

    async def get_optional(self, name: str) -> str | None:
        """Get a secret by name, returning None if not found.

        Args:
            name: Secret name.

        Returns:
            The secret value, or None if not found.

        Raises:
            SecretStoreError: If retrieval fails (other than not found).
        """
        ...

    async def get_bulk(self, names: list[str]) -> dict[str, str]:
        """Get multiple secrets.

        Args:
            names: List of secret names.

        Returns:
            Dictionary of name -> value for found secrets.

        Raises:
            SecretStoreError: If retrieval fails.
        """
        ...

    async def list_names(self) -> list[str]:
        """List available secret names.

        Returns:
            List of secret names.
        """
        ...


class EnvironmentSecretStore:
    """Secret store backed by environment variables.

    Useful for local development and simple deployments.
    """

    def __init__(self, prefix: str = "") -> None:
        """Initialize with optional prefix.

        Args:
            prefix: Prefix for environment variable names.
                   e.g., prefix ``"MYAPP_"`` maps secret ``"DB_PASSWORD"``
                   to env var ``"MYAPP_DB_PASSWORD"``.
        """
        self._prefix = prefix

    async def get(self, name: str) -> str:
        """Get a secret from environment."""

        env_name = f"{self._prefix}{name}"
        value = os.environ.get(env_name)
        if value is None:
            logger.debug(
                "Secret '%s' not found in environment (env var: %s)", name, env_name
            )
            raise SecretNotFoundError(name)
        return value

    async def get_optional(self, name: str) -> str | None:
        """Get a secret from environment, returning None if not set."""

        env_name = f"{self._prefix}{name}"
        return os.environ.get(env_name)

    async def get_bulk(self, names: list[str]) -> dict[str, str]:
        """Get multiple secrets from environment."""

        result = {}
        missing = []
        for name in names:
            env_name = f"{self._prefix}{name}"
            value = os.environ.get(env_name)
            if value is not None:
                result[name] = value
            else:
                missing.append(name)
        if missing and not result:
            logger.warning(
                "get_bulk: no secrets resolved; %d env vars not set: %s",
                len(missing),
                missing,
            )
        elif missing:
            logger.debug(
                "get_bulk: resolved %d, skipped %d unset env vars",
                len(result),
                len(missing),
            )
        return result

    async def list_names(self) -> list[str]:
        """List environment variables with the configured prefix."""

        if not self._prefix:
            return list(os.environ.keys())
        return [
            k[len(self._prefix) :]
            for k in os.environ.keys()
            if k.startswith(self._prefix)
        ]
