"""CredentialResolver — resolves CredentialRef to typed Credential instances."""

from __future__ import annotations

import json
import os
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

    Supports three resolution paths, selected by which field on the
    :class:`~application_sdk.credentials.ref.CredentialRef` is populated:

    1. **Agent path** (``agent_spec`` is non-None): Uses the typed
       :class:`AgentCredentialSpec`, fetches the bundle at ``secret-path`` via the injected
       :class:`SecretStore`, substitutes ref-keys for real values, and
       collapses dotted root keys into nested dicts.  See
       :mod:`application_sdk.credentials.agent` for the substitution
       contract.  Typed resolution uses the ``auth_type`` field on the
       spec when ``credential_type`` is empty.

    2. **GUID path** (``credential_guid`` is non-empty): Checks the
       state store (local-dev inline creds), then the secret store (returns
       immediately if the result is complete — i.e. contains ``authType``),
       then falls through to ``DaprCredentialVault.get_credentials(guid)``
       which merges S3 config with Vault secrets.  → if
       ``credential_type != "unknown"`` parses into typed Credential,
       otherwise wraps in ``RawCredential``.

    3. **Named path** (both routing fields empty): Calls
       ``secret_store.get(ref.name)`` → parses JSON → looks up parser in
       registry → returns typed Credential.
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
        if ref.agent_spec:
            return await self._resolve_agent(ref)
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
        if ref.agent_spec:
            return await self._resolve_agent_raw(ref)
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

    async def _resolve_agent(self, ref: "CredentialRef") -> "Credential":
        """Resolve an agent-shape ref to a typed Credential.

        Falls back to the ``auth_type`` field on the agent spec
        when ``ref.credential_type`` is empty (the common case —
        ``CredentialRef.from_workflow_args`` does not populate
        ``credential_type`` for agent refs).
        """
        data = await self._resolve_agent_raw(ref)
        type_name = (
            ref.credential_type
            or (ref.agent_spec.auth_type if ref.agent_spec else "")
            or data.get("auth-type")
            or "unknown"
        )
        if type_name == "unknown":
            from application_sdk.credentials.types import RawCredential

            return RawCredential(data=data)
        # For typed parsing, pull the section matching the auth type if
        # present (e.g. data["basic"] for ``auth-type: "basic"``);
        # otherwise hand the whole flat dict to the registry parser.
        parser_input = data.get(type_name, data)
        return self._registry.parse(type_name, parser_input)

    async def _resolve_agent_raw(self, ref: "CredentialRef") -> dict[str, Any]:
        """Resolve an agent-shape ref to a flat dict with ``extra`` nested."""
        from application_sdk.credentials.agent import resolve_agent_credential

        assert ref.agent_spec is not None  # guaranteed by caller
        return await resolve_agent_credential(ref.agent_spec, self._secret_store)

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

        Resolution order:
        1. State store (local-dev only) — inline credentials from handler
        2. Secret store — returns immediately if result is complete (has authType)
        3. DaprCredentialVault — merges S3 config + Vault secrets (production path)

        In production, the secret store (Vault) only holds the sensitive half
        of the credential.  The completeness check (``authType in result``)
        ensures we fall through to DaprCredentialVault for the full merge.
        """

        # State store check — handler/service.py stores inline credentials here
        # under the "cred:" prefix + UUID that becomes ref.name / ref.credential_guid.
        # Guarded to local-dev only (symmetric with the write guard in handler/service.py)
        # to avoid unnecessary state store round-trips in production.

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

        # Secret store check — backward compat path.  In production the Dapr
        # secret store resolves to Vault which only holds the *sensitive* half
        # of the credential (e.g. username, tokens).  If the result looks
        # incomplete (missing authType — a key config field that always lives
        # in S3), fall through to DaprCredentialVault which merges both halves.
        from application_sdk.infrastructure.secrets import SecretNotFoundError

        try:
            raw = await self._secret_store.get(ref.name)
            data_parsed: dict[str, Any] = json.loads(raw)
            if "authType" in data_parsed:
                # Complete credential (e.g. local dev inline or full bundle)
                return data_parsed
            logger.debug(
                "Secret store returned partial creds for GUID %r "
                "(missing authType); falling through to credential vault",
                ref.name,
            )
        except SecretNotFoundError:
            pass
        except json.JSONDecodeError:
            logger.debug(
                "Local store value for GUID %r is not valid JSON; trying vault",
                ref.name,
            )
        except Exception:
            logger.debug(
                "Local store lookup failed for GUID %r; trying vault",
                ref.name,
                exc_info=True,
            )

        # Fall back to DaprCredentialVault which merges S3 config (authType,
        # host, port, etc.) with Vault secrets (passwords, tokens, etc.).
        from application_sdk.credentials.errors import CredentialNotFoundError
        from application_sdk.infrastructure import AsyncDaprClient, DaprCredentialVault

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
