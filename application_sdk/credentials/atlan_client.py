"""Async Atlan client factory and mixin for App subclasses."""

from __future__ import annotations

from typing import TYPE_CHECKING

from application_sdk.observability.logger_adaptor import get_logger

if TYPE_CHECKING:
    from application_sdk.credentials.ref import CredentialRef
    from application_sdk.credentials.types import Credential

logger = get_logger(__name__)

# Well-known state key for handing off a validated AsyncAtlanClient from
# validate() to the first get_or_create_async_atlan_client() call.
# This avoids a second login when validation already created a working client.
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

    logger.debug(
        "creating AsyncAtlanClient",
        credential_type=type(cred).__name__,
        base_url=getattr(cred, "base_url", None),
    )
    if isinstance(cred, AtlanApiToken):
        return AsyncAtlanClient(base_url=cred.base_url, api_key=cred.token)
    if isinstance(cred, AtlanOAuthClient):
        # Pass client_id/secret so AsyncAtlanClient's internal token manager
        # handles token exchange and automatic refresh.
        # Never use a pre-fetched access_token as a static api_key — it expires
        # and the client silently returns empty responses rather than 401.
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

    Mix into an ``App`` subclass to get ``get_or_create_async_atlan_client``::

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

        Lookup order:
        1. Per-credential named cache (``async_atlan_client:{name}``) — fastest
           path on all calls after the first.
        2. Validated client hand-off (``_VALIDATED_ASYNC_CLIENT_KEY``) — reuses
           a client already created during ``validate()``.  The key is claimed
           (moved to the named slot and cleared) so it is only consumed once.
        3. Resolve the credential and create a new client, then cache it.

        Args:
            credential: The ``CredentialRef`` identifying the Atlan credential.

        Returns:
            A cached or newly created ``AsyncAtlanClient``.
        """
        cache_key = f"async_atlan_client:{credential.name}"

        # 1. Named cache — reused on every subsequent call within this execution.
        cached = self.app_state.get(cache_key)  # type: ignore[attr-defined]
        if cached is not None:
            logger.debug(
                "reusing cached Atlan async client",
                credential_name=credential.name,
            )
            return cached

        # 2. Validated hand-off — claim once, move to named slot, clear well-known key.
        validated = self.app_state.get(_VALIDATED_ASYNC_CLIENT_KEY)  # type: ignore[attr-defined]
        if validated is not None:
            self.app_state.set(_VALIDATED_ASYNC_CLIENT_KEY, None)  # type: ignore[attr-defined]
            self.app_state.set(cache_key, validated)  # type: ignore[attr-defined]
            logger.debug(
                "reusing validated Atlan async client",
                credential_name=credential.name,
            )
            return validated

        # 3. Resolve and create.
        cred = await self.context.resolve_credential(credential)  # type: ignore[attr-defined]
        client = create_async_atlan_client(cred)
        self.app_state.set(cache_key, client)  # type: ignore[attr-defined]
        logger.debug(
            "Atlan async client created and cached",
            credential_name=credential.name,
            credential_type=type(cred).__name__,
        )
        return client
