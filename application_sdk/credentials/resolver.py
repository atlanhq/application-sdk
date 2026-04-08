"""CredentialResolver — resolves CredentialRef to typed Credential instances."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from application_sdk.credentials.ref import CredentialRef
    from application_sdk.credentials.registry import CredentialTypeRegistry
    from application_sdk.credentials.types import Credential
    from application_sdk.infrastructure.secrets import SecretStore


class CredentialResolver:
    """Resolves a CredentialRef to a typed Credential.

    Supports two resolution paths:

    1. **New path** (``credential_guid`` is empty): Calls
       ``secret_store.get(ref.name)`` → parses JSON → looks up parser in
       registry → returns typed Credential.

    2. **Legacy path** (``credential_guid`` is non-empty): Lazily imports the
       deprecated v2 ``SecretStore.get_credentials()`` → if
       ``credential_type != "unknown"`` parses into typed Credential, otherwise
       wraps in ``RawCredential``.
    """

    def __init__(
        self,
        secret_store: "SecretStore",
        registry: "CredentialTypeRegistry | None" = None,
    ) -> None:
        self._secret_store = secret_store
        if registry is None:
            from application_sdk.credentials.registry import get_registry

            self._registry = get_registry()
        else:
            self._registry = registry

    async def resolve(self, ref: "CredentialRef") -> "Credential":
        """Resolve a CredentialRef to a typed Credential.

        Args:
            ref: The CredentialRef to resolve.

        Returns:
            A typed Credential instance.

        Raises:
            CredentialNotFoundError: If the credential cannot be found.
            CredentialParseError: If the JSON cannot be parsed.
        """
        if ref.credential_guid:
            return await self._resolve_legacy(ref)
        return await self._resolve_new(ref)

    async def resolve_raw(self, ref: "CredentialRef") -> dict[str, Any]:
        """Resolve a CredentialRef to a raw dict.

        This bridge method lets legacy clients that expect ``dict[str, Any]``
        consume a ``CredentialRef`` without rewriting their internals.

        Args:
            ref: The CredentialRef to resolve.

        Returns:
            The raw credential data as a dict.
        """
        if ref.credential_guid:
            return await self._resolve_legacy_raw(ref)
        raw_json = await self._fetch_raw_json(ref)
        return raw_json

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _resolve_new(self, ref: "CredentialRef") -> "Credential":
        data = await self._fetch_raw_json(ref)
        type_name = data.get("type", ref.credential_type)
        return self._registry.parse(type_name, data)

    async def _fetch_raw_json(self, ref: "CredentialRef") -> dict[str, Any]:
        from application_sdk.credentials.errors import (
            CredentialNotFoundError,
            CredentialParseError,
        )
        from application_sdk.infrastructure.secrets import SecretNotFoundError

        try:
            raw = await self._secret_store.get(ref.name)
        except SecretNotFoundError:
            raise CredentialNotFoundError(ref.name)
        except Exception as exc:
            from application_sdk.credentials.errors import CredentialError

            raise CredentialError(
                f"Failed to fetch credential '{ref.name}': {exc}",
                credential_name=ref.name,
                cause=exc,
            ) from exc

        try:
            data: dict[str, Any] = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise CredentialParseError(
                f"Credential '{ref.name}' is not valid JSON: {exc}",
                credential_name=ref.name,
                cause=exc,
            ) from exc

        return data

    async def _resolve_legacy(self, ref: "CredentialRef") -> "Credential":
        data = await self._resolve_legacy_raw(ref)
        if ref.credential_type == "unknown":
            from application_sdk.credentials.types import RawCredential

            return RawCredential(data=data)
        return self._registry.parse(ref.credential_type, data)

    async def _resolve_legacy_raw(self, ref: "CredentialRef") -> dict[str, Any]:
        """Call the deprecated v2 SecretStore.get_credentials() API."""
        from application_sdk.credentials.errors import CredentialNotFoundError

        try:
            # Lazy import of the v2 deprecated service
            from application_sdk.services.secretstore import (
                SecretStore as V2SecretStore,  # type: ignore[import]
            )

            result: dict[str, Any] = await V2SecretStore.get_credentials(
                ref.credential_guid
            )
            return result
        except ImportError:
            # v2 SecretStore not available — fall back to v3 secret store
            pass
        except Exception as exc:
            raise CredentialNotFoundError(ref.credential_guid) from exc

        # Fall back: try the v3 secret store with the GUID as the key
        data = await self._fetch_raw_json(
            ref.__class__(
                name=ref.credential_guid,
                credential_type=ref.credential_type,
                store_name=ref.store_name,
            )
        )
        return data
