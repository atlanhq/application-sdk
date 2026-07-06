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

    Supports three resolution paths, selected by which field on the
    :class:`~application_sdk.credentials.ref.CredentialRef` is populated:

    1. **Agent path** (``agent_spec`` is non-None): Uses the typed
       :class:`AgentCredentialSpec`, fetches the bundle at ``secret-path`` via the injected
       :class:`SecretStore`, substitutes ref-keys for real values, and
       collapses dotted root keys into nested dicts.  See
       :mod:`application_sdk.credentials.agent` for the substitution
       contract.  Typed resolution uses the ``auth_type`` field on the
       spec when ``credential_type`` is empty.

    2. **GUID path** (``credential_guid`` is non-empty): Uses
       ``DaprCredentialVault.get_credentials(guid)`` to resolve
       platform-issued GUIDs from the upstream store. → if
       ``credential_type != "unknown"`` parses into typed Credential, otherwise
       wraps in ``RawCredential``.

    3. **Named path** (both routing fields empty): Calls
       ``secret_store.get(ref.name)`` → parses JSON → looks up parser in
       registry → returns typed Credential.
    """

    def __init__(
        self,
        secret_store: SecretStore,
        registry: CredentialTypeRegistry | None = None,
    ) -> None:
        self._secret_store = secret_store
        if registry is None:
            from application_sdk.credentials.registry import (  # noqa: PLC0415 — circular: credentials/__init__.py loads sibling modules
                get_registry,
            )

            self._registry = get_registry()
        else:
            self._registry = registry

    async def resolve(self, ref: CredentialRef) -> Credential:
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

    async def resolve_raw(self, ref: CredentialRef) -> dict[str, Any]:
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

    async def _resolve_new(self, ref: CredentialRef) -> Credential:
        data = await self._fetch_raw_json(ref)
        type_name = data.get("type", ref.credential_type)
        return self._registry.parse(type_name, data)

    async def _resolve_agent(self, ref: CredentialRef) -> Credential:
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
            from application_sdk.credentials.types import (  # noqa: PLC0415 — circular: credentials/__init__.py loads sibling modules
                RawCredential,
            )

            return RawCredential(data=data)
        # For typed parsing, pull the section matching the auth type if
        # present (e.g. data["basic"] for ``auth-type: "basic"``);
        # otherwise hand the whole flat dict to the registry parser.
        parser_input = data.get(type_name, data)
        return self._registry.parse(type_name, parser_input)

    async def _resolve_agent_raw(self, ref: CredentialRef) -> dict[str, Any]:
        """Resolve an agent-shape ref to a flat dict with ``extra`` nested."""
        from application_sdk.credentials.agent import (  # noqa: PLC0415 — circular: credentials/__init__.py loads sibling modules
            resolve_agent_credential,
        )

        assert ref.agent_spec is not None  # guaranteed by caller
        return await resolve_agent_credential(ref.agent_spec, self._secret_store)

    async def _fetch_raw_json(self, ref: CredentialRef) -> dict[str, Any]:
        from application_sdk.credentials.errors import (  # noqa: PLC0415 — circular: credentials/__init__.py loads sibling modules
            CredentialNotFoundError,
            CredentialParseError,
        )
        from application_sdk.infrastructure.secrets import (  # noqa: PLC0415 — circular: infrastructure.secrets imports observability which loads credentials transitively
            SecretNotFoundError,
            retry_past_cold_start,
        )

        try:
            # Named-path fetch races the same cold Dapr sidecar the agent
            # bundle fetch does (application_sdk.credentials.agent) — share
            # its retry/one-shot-gate mechanics rather than failing fast on
            # a startup race this store call has no special handling for.
            raw = await retry_past_cold_start(
                lambda: self._secret_store.get(ref.name),
                description=f"Named credential fetch for '{ref.name}'",
            )
        except SecretNotFoundError as exc:
            raise CredentialNotFoundError(ref.name) from exc
        # conformance: ignore[E004] re-raise only; wraps any unexpected fetch error into typed CredentialError and re-raises
        except Exception as exc:
            from application_sdk.credentials.errors import (  # noqa: PLC0415 — circular: credentials/__init__.py loads sibling modules
                CredentialError,
            )

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

    async def _resolve_legacy(self, ref: CredentialRef) -> Credential:
        data = await self._resolve_by_guid(ref)
        if ref.credential_type == "unknown":
            from application_sdk.credentials.types import (  # noqa: PLC0415 — circular: credentials/__init__.py loads sibling modules
                RawCredential,
            )

            return RawCredential(data=data)
        return self._registry.parse(ref.credential_type, data)

    async def _resolve_by_guid(self, ref: CredentialRef) -> dict[str, Any]:
        """Resolve credentials by GUID via DaprCredentialVault.

        Fetches the non-secret credential config from the upstream object store
        (S3) and merges it with secrets from the Dapr secret store (Vault).
        This is the single resolution path for both production and local dev.
        """
        from application_sdk.credentials.errors import (  # noqa: PLC0415 — circular: credentials/__init__.py loads sibling modules
            CredentialNotFoundError,
        )
        from application_sdk.errors import (  # noqa: PLC0415 — circular: errors imports observability which loads credentials transitively
            DependencyUnavailableError,
        )
        from application_sdk.errors.leaves import (  # noqa: PLC0415 — circular: errors imports observability which loads credentials transitively
            ColdStartRaceError,
        )
        from application_sdk.infrastructure import (  # noqa: PLC0415 — optional dep: dapr (DaprCredentialVault is Dapr-only)
            AsyncDaprClient,
            DaprCredentialVault,
        )
        from application_sdk.infrastructure.credential_vault import (  # noqa: PLC0415 — optional dep: dapr (DaprCredentialVault is Dapr-only)
            CredentialVaultError,
        )

        dapr_client = AsyncDaprClient()
        try:
            vault = DaprCredentialVault(dapr_client)
            result: dict[str, Any] = await vault.get_credentials(ref.credential_guid)
            return result
        except CredentialNotFoundError:
            raise
        except CredentialVaultError as exc:
            # CredentialVaultError is DaprCredentialVault's single umbrella
            # error: raised directly (with no `cause`) for a definitively
            # missing/invalid credential config, AND used to wrap a genuine
            # platform outage that exhausted retry_past_cold_start's budget
            # (`cause` is the surviving ColdStartRaceError, e.g.
            # SecretStoreUnavailableError). Only the latter must bypass the
            # not-found collapse below — collapsing a real outage into "not
            # found" would misreport a retryable platform issue as a
            # non-retryable, user-facing "credential not found". A genuinely
            # absent config IS a not-found and should collapse like every
            # other failure here, not be swept up by the broader
            # DependencyUnavailableError check just because this deprecated
            # umbrella type happens to subclass it.
            if isinstance(exc.cause, ColdStartRaceError):
                raise
            raise CredentialNotFoundError(ref.credential_guid) from exc
        # A genuine dependency-unavailable failure that didn't come through
        # CredentialVaultError (e.g. a future CredentialVault implementation
        # raising DependencyUnavailableError directly) must not collapse
        # into "not found" either — same reasoning as above.
        except DependencyUnavailableError:
            raise
        # conformance: ignore[E004] re-raise only; wraps any unexpected vault error into typed CredentialNotFoundError and re-raises
        except Exception as exc:
            raise CredentialNotFoundError(ref.credential_guid) from exc
        finally:
            await dapr_client.close()
