"""Secrets management abstraction."""

import asyncio
import os
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, ClassVar, Protocol, TypeVar

from application_sdk.errors import SECRET_NOT_FOUND, SECRET_STORE_ERROR, ErrorCode
from application_sdk.errors.categories import Audience
from application_sdk.errors.leaves import (
    ColdStartRaceError,
    DependencyUnavailableError,
    NotFoundError,
)
from application_sdk.infrastructure._secret_utils import process_secret_data
from application_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)

_T = TypeVar("_T")


async def get_deployment_secret(key: str) -> Any:
    """Get a specific key from deployment configuration in the deployment secret store.

    Checks whether the deployment secret store Dapr component is registered
    before fetching.  Returns ``None`` when:

    - Running in a local (non-Dapr) environment.
    - The component is not registered.
    - The key is not present in the secret.
    - Any Dapr error occurs.

    Args:
        key: The key to look up inside the deployment secret.

    Returns:
        The value for *key*, or ``None`` if unavailable.
    """
    from application_sdk.constants import (  # noqa: PLC0415 — cold path: only on secret resolution
        DEPLOYMENT_NAME,
        DEPLOYMENT_SECRET_PATH,
        DEPLOYMENT_SECRET_STORE_NAME,
        LOCAL_ENVIRONMENT,
    )

    if DEPLOYMENT_NAME == LOCAL_ENVIRONMENT:
        return None

    try:
        from application_sdk.infrastructure._dapr.http import (  # noqa: PLC0415 — circular: infrastructure/__init__.py loads sibling modules
            AsyncDaprClient,
        )

        client = AsyncDaprClient()
        try:
            # Try multi-key bundle first.
            result = await client.get_secret(
                DEPLOYMENT_SECRET_STORE_NAME, DEPLOYMENT_SECRET_PATH
            )
            secret_data = process_secret_data(result)
            if isinstance(secret_data, dict) and key in secret_data:
                return secret_data[key]

            # Fall back to single-key lookup.
            logger.debug("Multi-key bundle lookup missed; trying single-key: %s", key)
            result = await client.get_secret(DEPLOYMENT_SECRET_STORE_NAME, key)
            single_data = process_secret_data(result)
            if isinstance(single_data, dict):
                if key in single_data:
                    return single_data[key]
                if len(single_data) == 1:
                    return list(single_data.values())[0]

            return None
        finally:
            await client.close()

    except Exception:
        logger.error("Failed to fetch deployment config key: %s", key, exc_info=True)
        return None


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
    or a 5xx from a still-initialising component — as opposed to the store
    answering and rejecting the request (bad auth / binding / path, which is a
    plain :class:`SecretStoreError`).

    Classified at the infrastructure layer (``_dapr/client.py``), where the
    httpx exception family is visible, so callers can retry a cold-start race
    against a not-yet-ready sidecar without duck-typing exception text. Remains
    a ``SecretStoreError`` so ``except SecretStoreError`` still catches it, and
    a :class:`ColdStartRaceError` so :func:`retry_past_cold_start` retries it.
    """

    code: ClassVar[str] = "DEPENDENCY_UNAVAILABLE_SECRET_STORE_UNREACHABLE"

    def __init__(self, secret_name: str, *, cause: Exception | None = None) -> None:
        super().__init__(
            f"Secret store unavailable while fetching secret '{secret_name}'",
            secret_name=secret_name,
            cause=cause,
        )


#: How long an idempotent Dapr-backed call may retry a transient
#: :class:`DependencyUnavailableError` before giving up. The agent
#: secret-bundle fetch is typically the first Dapr call a workflow makes and
#: can race a cold sidecar (daprd component init has been observed up to
#: ~75s on fresh CI runners); every :func:`retry_past_cold_start` caller
#: shares this one budget, since they all race the same sidecar. This is a
#: floor, not a hard ceiling: each retried ``call()`` can itself burn up to
#: the transport's own internal retry budget (~15s, see
#: ``infrastructure/_dapr/http.py``) before raising, so the worst-case total
#: wait before final failure can exceed this value by up to one attempt's
#: duration. Env-overridable for pathological runners.
SECRET_FETCH_MAX_WAIT_SECONDS: float = float(
    os.environ.get("ATLAN_SECRET_FETCH_MAX_WAIT_SECONDS", "120")
)
SECRET_FETCH_BASE_DELAY_SECONDS: float = 2.0
SECRET_FETCH_MAX_DELAY_SECONDS: float = 10.0

#: Process-level cold-start gate, shared by every call site that opts into
#: cold-start retry via :func:`retry_past_cold_start` — they all race the
#: same Dapr sidecar, so confirming readiness via one arms it for all. Set
#: the first time any such call gets a definitive answer (success, or a
#: non-transient failure) from the store in this worker process; a later
#: failure is then a steady-state outage, left to surface via the backend's
#: own (shorter) retry budget rather than being retried again here.
_secret_store_confirmed_ready: bool = False


async def retry_past_cold_start(
    call: Callable[[], Awaitable[_T]],
    *,
    description: str,
) -> _T:
    """Retry an idempotent Dapr-backed call past a cold sidecar.

    Retries a :class:`ColdStartRaceError` (a structurally transient
    failure — e.g. :class:`SecretStoreUnavailableError`) with capped
    exponential backoff until :data:`SECRET_FETCH_MAX_WAIT_SECONDS` elapses.
    Branching on that marker rather than a single concrete exception type
    means any current or future :class:`DependencyUnavailableError` subtype
    (state store, pub/sub, object store) can opt into cold-start retry just
    by also inheriting :class:`ColdStartRaceError` for its transient case —
    no new call site-specific check needed here. This is deliberately
    independent of ``retryable``/``effective_retryable``, which is a
    separate, general Temporal/wire-level retry hint — see
    :class:`ColdStartRaceError`'s docstring for why the two must not be
    conflated.

    Any other exception (a definitive rejection, e.g.
    :class:`SecretNotFoundError` or a 4xx :class:`SecretStoreError`, or an
    exception outside the ``ColdStartRaceError`` family entirely) is proof
    the dependency is reachable: it arms the process-level cold-start gate
    and is re-raised immediately, never retried.

    Scoped to cold start: once any caller using this helper has seen the
    dependency answer definitively in this worker process, later calls skip
    the wait entirely and call through once — a later outage is then a
    steady-state failure, left to surface via the backend's own (shorter)
    retry budget instead of being masked for the full deadline on every
    subsequent call.

    Shared across every call site that opts in (the agent secret-bundle
    fetch, single-key probes, the named-credential resolver path, the
    GUID/vault credential path) — they all race the same sidecar, so one
    confirming readiness arms it for the rest.

    Args:
        call: Zero-arg async callable to retry, e.g. ``lambda:
            secret_store.get(name)``. Must be idempotent — it may be invoked
            more than once.
        description: Human-readable label for the retry-warning log line,
            e.g. ``"Agent secret-bundle fetch at 'foo'"``.
    """
    global _secret_store_confirmed_ready

    if _secret_store_confirmed_ready:
        return await call()

    deadline = time.monotonic() + SECRET_FETCH_MAX_WAIT_SECONDS
    attempt = 0
    while True:
        attempt += 1
        try:
            result = await call()
        except ColdStartRaceError as exc:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                # Gave up waiting, but the dependency never actually
                # answered — do NOT arm the gate, a later call should still
                # wait out the cold start rather than assume steady-state
                # readiness.
                raise
            # Cap the backoff to the remaining budget so the total wait can't
            # overshoot SECRET_FETCH_MAX_WAIT_SECONDS by a full delay, and cap
            # the exponent so a very large max-wait override can't overflow.
            delay = min(
                SECRET_FETCH_MAX_DELAY_SECONDS,
                SECRET_FETCH_BASE_DELAY_SECONDS * (2 ** min(attempt - 1, 10)),
                remaining,
            )
            logger.warning(
                "%s failed (attempt %d); the dependency is not yet "
                "reachable — retrying in %.1fs: %s",
                description,
                attempt,
                delay,
                exc,
                exc_info=True,
            )
            await asyncio.sleep(delay)
        # conformance: ignore[E004] proof the dependency answered (definitively) — arms the cold-start gate and re-raises unchanged
        except Exception:
            _secret_store_confirmed_ready = True
            raise
        else:
            _secret_store_confirmed_ready = True
            return result


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
