"""Async Atlan client factory and mixin for App subclasses."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from application_sdk.credentials.ref import CredentialRef
    from application_sdk.credentials.types import Credential

# Cache key used to store a client created during credential validation,
# so the mixin can reuse it instead of creating a second connection.
_VALIDATED_ASYNC_CLIENT_KEY = "validated_async_atlan_client"


def create_async_atlan_client(cred: "Credential") -> "object":
    """Create an AsyncAtlanClient from a resolved Atlan credential.

    Args:
        cred: A resolved ``AtlanApiToken`` or ``AtlanOAuthClient`` credential.

    Returns:
        An ``AsyncAtlanClient`` instance configured for the given credential.

    Raises:
        TypeError: If ``cred`` is not a supported Atlan credential type.
    """
    from pyatlan_v9.client.aio import AsyncAtlanClient  # type: ignore[import]

    from application_sdk.credentials.atlan import AtlanApiToken, AtlanOAuthClient

    if isinstance(cred, AtlanApiToken):
        return AsyncAtlanClient(base_url=cred.base_url, api_key=cred.token)
    if isinstance(cred, AtlanOAuthClient):
        return AsyncAtlanClient(
            base_url=cred.base_url,
            oauth_client_id=cred.client_id,
            oauth_client_secret=cred.client_secret,
        )
    raise TypeError(
        f"Unsupported Atlan credential type: {type(cred).__name__}. "
        "Expected AtlanApiToken or AtlanOAuthClient."
    )


class AtlanClientMixin:
    """Mixin providing cached async Atlan client access for App subclasses.

    Mix into an ``App`` subclass to get ``get_or_create_async_atlan_client``:

        class MyApp(AtlanClientMixin, App):
            @task
            async def do_work(self, input: MyInput) -> MyOutput:
                client = await self.get_or_create_async_atlan_client(input.credential)
                result = await client.asset.get_by_guid(...)
    """

    async def get_or_create_async_atlan_client(
        self, credential: "CredentialRef"
    ) -> "object":
        """Return a cached AsyncAtlanClient for the given credential ref.

        If a client was already created during the credential validation phase
        (stored under ``_VALIDATED_ASYNC_CLIENT_KEY``), that client is reused
        directly without re-resolving the credential.  Otherwise the credential
        is resolved via ``self.context`` and a new client is created and cached
        under the per-credential key.

        Args:
            credential: The ``CredentialRef`` identifying the Atlan credential.

        Returns:
            A cached or newly created ``AsyncAtlanClient``.
        """
        # Reuse client created during validation if available.
        validated = self.app_state.get(_VALIDATED_ASYNC_CLIENT_KEY)  # type: ignore[attr-defined]
        if validated is not None:
            return validated

        cache_key = f"async_atlan_client:{credential.name}"
        cached = self.app_state.get(cache_key)  # type: ignore[attr-defined]
        if cached is not None:
            return cached

        cred = await self.context.resolve_credential(credential)  # type: ignore[attr-defined]
        client = create_async_atlan_client(cred)
        self.app_state.set(cache_key, client)  # type: ignore[attr-defined]
        return client
