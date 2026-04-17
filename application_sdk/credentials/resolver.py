"""CredentialResolver — resolves CredentialRef to typed Credential instances."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from application_sdk.observability.logger_adaptor import get_logger

if TYPE_CHECKING:
    from application_sdk.credentials.ref import CredentialRef
    from application_sdk.credentials.registry import CredentialTypeRegistry
    from application_sdk.credentials.types import Credential
    from application_sdk.infrastructure.secrets import SecretStore

logger = get_logger(__name__)


class CredentialResolver:
    """Resolves a CredentialRef to a typed Credential.

    Supports two resolution paths:

    1. **Named path** (``credential_guid`` is empty): Calls
       ``secret_store.get(ref.name)`` → parses JSON → looks up parser in
       registry → returns typed Credential.

    2. **GUID path** (``credential_guid`` is non-empty): First checks
       ``secret_store.get(ref.name)`` to cover in-process inline credentials
       stored by the handler layer (combined-mode / local dev). If not found
       there, uses ``DaprCredentialVault.get_credentials(guid)`` to resolve
       platform-issued GUIDs from the upstream store. → if
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
            return await self._resolve_by_guid(ref)
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
        except SecretNotFoundError as exc:
            raise CredentialNotFoundError(ref.name) from exc
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
        data = await self._resolve_by_guid(ref)
        if ref.credential_type == "unknown":
            from application_sdk.credentials.types import RawCredential

            return RawCredential(data=data)
        return self._registry.parse(ref.credential_type, data)

    async def _resolve_by_guid(self, ref: "CredentialRef") -> dict[str, Any]:
        """Resolve credentials by GUID.

        Checks the local secret store first (covers in-process credential
        injection from handler/service.py in combined-mode and local dev), then
        falls back to DaprCredentialVault for production platform-issued GUIDs.
        """

        # State store check — handler/service.py stores inline credentials here
        # under the "cred:" prefix + UUID that becomes ref.name / ref.credential_guid.
        # Guarded to local-dev only (symmetric with the write guard in handler/service.py)
        # to avoid unnecessary state store round-trips in production.
        import os

        from application_sdk.infrastructure.context import get_infrastructure

        is_local_dev = os.environ.get("ATLAN_LOCAL_DEVELOPMENT", "").lower() in (
            "true",
            "1",
        )
        infra = get_infrastructure()
        if is_local_dev and infra and infra.state_store:
            try:
                data = await infra.state_store.load(f"cred:{ref.name}")
                if data is not None:
                    return data
            except Exception:
                logger.debug(
                    "State store lookup failed for GUID %r; trying secret store",
                    ref.name,
                    exc_info=True,
                )

        # Secret store check (backward compat) — handler/service.py previously
        # stored inline credentials here under the same UUID.
        from application_sdk.infrastructure.secrets import SecretNotFoundError

        try:
            raw = await self._secret_store.get(ref.name)
            data_parsed: dict[str, Any] = json.loads(raw)
            return data_parsed
        except SecretNotFoundError:
            pass
        except json.JSONDecodeError:
            logger.debug(
                "Local store value for GUID %r is not valid JSON; trying Dapr",
                ref.name,
            )
        except Exception:
            logger.debug(
                "Local store lookup failed for GUID %r; trying Dapr",
                ref.name,
                exc_info=True,
            )

        # Fall back to DaprCredentialVault for platform-issued GUIDs.
        from application_sdk.credentials.errors import CredentialNotFoundError
        from application_sdk.infrastructure import DaprCredentialVault
        from application_sdk.infrastructure._dapr.http import AsyncDaprClient

        dapr_client = AsyncDaprClient()
        try:
            vault = DaprCredentialVault(dapr_client)
            result: dict[str, Any] = await vault.get_credentials(ref.credential_guid)
            return result
        except CredentialNotFoundError:
            raise
        except Exception as exc:
            raise CredentialNotFoundError(ref.credential_guid) from exc
        finally:
            await dapr_client.close()
