"""Async Atlan client factory and mixin for App subclasses."""

from __future__ import annotations

from typing import TYPE_CHECKING

from application_sdk.observability.logger_adaptor import get_logger
from application_sdk.version import __version__ as _SDK_VERSION

if TYPE_CHECKING:
    from pyatlan_v9.client.aio import AsyncAtlanClient  # type: ignore[import]

    from application_sdk.app.context import AppContext
    from application_sdk.credentials.ref import CredentialRef
    from application_sdk.credentials.types import Credential

logger = get_logger(__name__)

# Well-known state key for handing off a validated AsyncAtlanClient from
# validate() to the first get_or_create_async_atlan_client() call.
# This avoids a second login when validation already created a working client.
_VALIDATED_ASYNC_CLIENT_KEY = "validated_async_atlan_client"


def _app_request_headers(context: AppContext | None) -> dict[str, str]:
    """Return SDK-side ``x-atlan-app-*`` headers for a pyatlan client.

    pyatlan_v9 already injects ``x-atlan-agent`` / ``x-atlan-agent-id`` /
    ``x-atlan-client-origin`` / ``x-atlan-python-version`` /
    ``x-atlan-client-type`` on every request (see
    ``pyatlan_v9.client.atlan.AtlanClient._default_headers``). This adds
    the three values that identify the *app calling pyatlan*, which the
    tenant gateway uses for per-app observability + quota separation
    (BLDX-1246):

      * ``x-atlan-app-name``        — connector slug from ``AppContext.app_name``
      * ``x-atlan-app-version``     — app version from ``AppContext.app_version``
      * ``x-atlan-app-sdk-version`` — ``application_sdk.__version__``

    Header naming follows pyatlan's existing lowercase ``x-atlan-*``
    convention so the gateway's request parser handles them uniformly.

    Args:
        context: The :class:`AppContext` providing ``app_name`` / ``app_version``.
            ``None`` is tolerated (e.g. ad-hoc client creation in tests) — only
            the SDK version header is emitted in that case so calls remain
            attributable to the SDK.

    Returns:
        Dict of headers ready to pass to
        :meth:`pyatlan_v9.client.aio.AsyncAtlanClient.update_headers`.
    """
    headers: dict[str, str] = {"x-atlan-app-sdk-version": _SDK_VERSION}
    if context is not None:
        # ``getattr`` rather than direct attr access — AppContext fields
        # are dataclass attrs with no defaults, but a test double / partial
        # context may not expose both. Skip silently in that case so we
        # never block client construction on header metadata.
        app_name = getattr(context, "app_name", None)
        app_version = getattr(context, "app_version", None)
        if app_name:
            headers["x-atlan-app-name"] = app_name
        if app_version:
            headers["x-atlan-app-version"] = app_version
    return headers


def _apply_app_headers(client: AsyncAtlanClient, headers: dict[str, str]) -> None:
    """Best-effort attach app-identification headers to a pyatlan_v9 client.

    :class:`pyatlan_v9.client.aio.AsyncAtlanClient` exposes
    ``update_headers`` (verified via :func:`inspect.getsource` against
    the vendored pyatlan_v9 wheel). The method is a session-headers
    merge, so calling it post-construction is the supported extension
    point.

    Header attachment is best-effort: if pyatlan ever renames or drops
    the method we surface a debug log but don't fail the caller — losing
    observability headers is strictly less bad than refusing to construct
    a working client.
    """
    if not headers:
        return
    update_headers = getattr(client, "update_headers", None)
    if not callable(update_headers):
        logger.debug(
            "pyatlan client %s exposes no update_headers; skipping app-identification headers",
            type(client).__name__,
        )
        return
    try:
        update_headers(headers)
    # conformance: ignore[E004] best-effort header attachment; exc_info already captured in debug log; swallowing is intentional per docstring
    except Exception:
        logger.debug(
            "update_headers failed on pyatlan client %s; continuing without app headers",
            type(client).__name__,
            exc_info=True,
        )


def create_async_atlan_client(
    cred: Credential,
    *,
    extra_headers: dict[str, str] | None = None,
) -> AsyncAtlanClient:
    """Create a pyatlan_v9 AsyncAtlanClient from a resolved Atlan credential.

    The SDK standardises on the async + v9 pyatlan surface: msgspec-based,
    fast, and the variant we recommend everywhere in app code. Callers
    that need a sync client or the classic pyatlan surface (FluentSearch,
    role_cache, …) should construct those directly from pyatlan — this
    factory deliberately exposes only the canonical path.

    Args:
        cred: A resolved ``AtlanApiToken`` or ``AtlanOAuthClient`` credential.
        extra_headers: Optional ``x-atlan-app-*`` (or other) headers to
            stamp onto the underlying HTTP session via
            :meth:`AsyncAtlanClient.update_headers`. Typically supplied by
            :class:`AtlanClientMixin` via :func:`_app_request_headers`;
            callers constructing a client manually can pass their own.

    Returns:
        A ``pyatlan_v9`` ``AsyncAtlanClient`` instance configured for the
        given credential.

    Raises:
        AtlanCredentialTypeError: If ``cred`` is not a supported Atlan credential type.
    """
    from pyatlan_v9.client.aio import (  # type: ignore[import]  # noqa: PLC0415 — optional dep: pyatlan_v9 (vendored)
        AsyncAtlanClient,
    )

    from application_sdk.credentials.atlan import (  # noqa: PLC0415 — circular: credentials/__init__.py loads sibling modules
        AtlanApiToken,
        AtlanOAuthClient,
    )

    logger.debug(
        "creating AsyncAtlanClient credential_type=%s base_url=%s",
        type(cred).__name__,
        getattr(cred, "base_url", None),
    )
    if isinstance(cred, AtlanApiToken):
        client = AsyncAtlanClient(base_url=cred.base_url, api_key=cred.token)
    elif isinstance(cred, AtlanOAuthClient):
        # Pass client_id/secret so the client's internal token manager
        # handles token exchange and automatic refresh.
        # Never use a pre-fetched access_token as a static api_key — it expires
        # and the client silently returns empty responses rather than 401.
        client = AsyncAtlanClient(
            base_url=cred.base_url,
            oauth_client_id=cred.client_id,
            oauth_client_secret=cred.client_secret,
        )
    else:
        from application_sdk.credentials.errors import (  # noqa: PLC0415
            AtlanCredentialTypeError,
        )

        raise AtlanCredentialTypeError()

    if extra_headers:
        _apply_app_headers(client, extra_headers)
    return client


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
        self, credential: CredentialRef
    ) -> AsyncAtlanClient:
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
                "reusing cached Atlan async client credential=%s", credential.name
            )
            return cached

        # 2. Validated hand-off — claim once, move to named slot, clear well-known key.
        validated = self.app_state.get(_VALIDATED_ASYNC_CLIENT_KEY)  # type: ignore[attr-defined]
        if validated is not None:
            self.app_state.set(_VALIDATED_ASYNC_CLIENT_KEY, None)  # type: ignore[attr-defined]
            self.app_state.set(cache_key, validated)  # type: ignore[attr-defined]
            logger.debug(
                "reusing validated Atlan async client credential=%s",
                credential.name,
            )
            return validated

        # 3. Resolve and create.
        cred = await self.context.resolve_credential(credential)  # type: ignore[attr-defined]
        # Stamp ``x-atlan-app-*`` headers (BLDX-1246) so the tenant
        # gateway can attribute every pyatlan request back to this app
        # for per-app observability / quota separation. Sourced from
        # the AppContext that the framework already injects on the mixin.
        extra_headers = _app_request_headers(getattr(self, "context", None))  # type: ignore[attr-defined]
        client = create_async_atlan_client(cred, extra_headers=extra_headers)
        self.app_state.set(cache_key, client)  # type: ignore[attr-defined]
        logger.debug(
            "Atlan async client created and cached credential=%s type=%s",
            credential.name,
            type(cred).__name__,
        )
        return client
