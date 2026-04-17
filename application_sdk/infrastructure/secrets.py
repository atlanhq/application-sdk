"""Secrets management abstraction."""

from typing import Any, ClassVar, Protocol

from application_sdk.errors import SECRET_NOT_FOUND, SECRET_STORE_ERROR, ErrorCode
from application_sdk.infrastructure._secret_utils import process_secret_data
from application_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)


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
    from application_sdk.constants import (
        DEPLOYMENT_NAME,
        DEPLOYMENT_SECRET_PATH,
        DEPLOYMENT_SECRET_STORE_NAME,
        LOCAL_ENVIRONMENT,
    )

    if DEPLOYMENT_NAME == LOCAL_ENVIRONMENT:
        return None

    try:
        from application_sdk.infrastructure._dapr.http import AsyncDaprClient

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


class SecretStoreError(Exception):
    """Raised when secret store operations fail."""

    DEFAULT_ERROR_CODE: ClassVar[ErrorCode] = SECRET_STORE_ERROR

    def __init__(
        self,
        message: str,
        *,
        secret_name: str | None = None,
        cause: Exception | None = None,
        error_code: ErrorCode | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.secret_name = secret_name
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
        if self.secret_name:
            parts.append(f"secret={self.secret_name}")
        if self.cause:
            parts.append(f"caused_by={type(self.cause).__name__}: {self.cause}")
        return " | ".join(parts)


class SecretNotFoundError(SecretStoreError):
    """Raised when a secret is not found."""

    DEFAULT_ERROR_CODE: ClassVar[ErrorCode] = SECRET_NOT_FOUND

    def __init__(self, secret_name: str) -> None:
        super().__init__(
            f"Secret '{secret_name}' not found",
            secret_name=secret_name,
        )


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
        import os

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
        import os

        env_name = f"{self._prefix}{name}"
        return os.environ.get(env_name)

    async def get_bulk(self, names: list[str]) -> dict[str, str]:
        """Get multiple secrets from environment."""
        import os

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
        import os

        if not self._prefix:
            return list(os.environ.keys())
        return [
            k[len(self._prefix) :]
            for k in os.environ.keys()
            if k.startswith(self._prefix)
        ]


def __getattr__(name: str):
    if name == "InMemorySecretStore":
        import warnings

        warnings.warn(
            "InMemorySecretStore is removed in v3. Use application_sdk.testing.mocks.MockSecretStore.",
            DeprecationWarning,
            stacklevel=2,
        )
        from application_sdk.testing.mocks import MockSecretStore

        return MockSecretStore
    raise AttributeError(name)
