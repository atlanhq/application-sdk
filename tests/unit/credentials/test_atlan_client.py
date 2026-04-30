"""Unit tests for create_async_atlan_client and AtlanClientMixin."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from application_sdk.credentials.atlan import AtlanApiToken, AtlanOAuthClient
from application_sdk.credentials.atlan_client import (
    _VALIDATED_ASYNC_CLIENT_KEY,
    AtlanClientMixin,
    create_async_atlan_client,
)
from application_sdk.credentials.ref import CredentialRef
from application_sdk.credentials.types import BasicCredential

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_app_state() -> MagicMock:
    """Return a mock AppStateAccessor with get/set backed by a dict."""
    store: dict = {}
    state = MagicMock()
    state.get.side_effect = lambda key: store.get(key)
    state.set.side_effect = lambda key, value: store.__setitem__(key, value)
    return state


def _make_mixin_app(app_state: MagicMock, resolved_cred=None) -> AtlanClientMixin:
    """Return an AtlanClientMixin instance with mocked App internals."""
    context = AsyncMock()
    if resolved_cred is not None:
        context.resolve_credential.return_value = resolved_cred

    mixin = AtlanClientMixin()
    mixin.app_state = app_state  # type: ignore[attr-defined]
    mixin.context = context  # type: ignore[attr-defined]
    mixin.logger = MagicMock()  # type: ignore[attr-defined]
    return mixin


# ---------------------------------------------------------------------------
# create_async_atlan_client — factory function
# ---------------------------------------------------------------------------


class TestCreateAsyncAtlanClient:
    def test_api_token_passes_base_url_and_api_key(self) -> None:
        """Verify correct kwargs are forwarded for AtlanApiToken."""
        cred = AtlanApiToken(token="my-token", base_url="https://tenant.atlan.com")
        mock_client = MagicMock()

        with patch(
            "pyatlan_v9.client.aio.AsyncAtlanClient", return_value=mock_client
        ) as mock_cls:
            result = create_async_atlan_client(cred)

        mock_cls.assert_called_once_with(
            base_url="https://tenant.atlan.com",
            api_key="my-token",
        )
        assert result is mock_client

    def test_oauth_client_passes_base_url_and_oauth_params(self) -> None:
        """Verify correct kwargs are forwarded for AtlanOAuthClient."""
        cred = AtlanOAuthClient(
            client_id="client-id",
            client_secret="client-secret",
            token_url="https://tenant.atlan.com/auth/realms/default/protocol/openid-connect/token",
            base_url="https://tenant.atlan.com",
        )
        mock_client = MagicMock()

        with patch(
            "pyatlan_v9.client.aio.AsyncAtlanClient", return_value=mock_client
        ) as mock_cls:
            result = create_async_atlan_client(cred)

        mock_cls.assert_called_once_with(
            base_url="https://tenant.atlan.com",
            oauth_client_id="client-id",
            oauth_client_secret="client-secret",
        )
        assert result is mock_client

    def test_unsupported_type_raises_type_error(self) -> None:
        cred = BasicCredential(username="user", password="pass")
        with pytest.raises(TypeError, match="Unsupported Atlan credential type"):
            with patch("pyatlan_v9.client.aio.AsyncAtlanClient"):
                create_async_atlan_client(cred)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# AtlanClientMixin — cache miss (creates new client)
# ---------------------------------------------------------------------------


class TestAtlanClientMixinCacheMiss:
    @pytest.mark.asyncio
    async def test_creates_client_on_first_call(self) -> None:
        cred_ref = CredentialRef(name="my-atlan", credential_type="atlan_api_token")
        resolved = AtlanApiToken(token="tok", base_url="https://t.atlan.com")

        app_state = _make_app_state()
        mixin = _make_mixin_app(app_state, resolved_cred=resolved)

        mock_client = MagicMock()
        with patch(
            "application_sdk.credentials.atlan_client.create_async_atlan_client",
            return_value=mock_client,
        ):
            result = await mixin.get_or_create_async_atlan_client(cred_ref)

        assert result is mock_client
        mixin.context.resolve_credential.assert_awaited_once_with(cred_ref)

    @pytest.mark.asyncio
    async def test_caches_client_after_first_call(self) -> None:
        cred_ref = CredentialRef(name="my-atlan", credential_type="atlan_api_token")
        resolved = AtlanApiToken(token="tok", base_url="https://t.atlan.com")

        app_state = _make_app_state()
        mixin = _make_mixin_app(app_state, resolved_cred=resolved)

        mock_client = MagicMock()
        with patch(
            "application_sdk.credentials.atlan_client.create_async_atlan_client",
            return_value=mock_client,
        ):
            first = await mixin.get_or_create_async_atlan_client(cred_ref)
            second = await mixin.get_or_create_async_atlan_client(cred_ref)

        assert first is second
        # resolve_credential should only be called once (second call is cache hit)
        assert mixin.context.resolve_credential.await_count == 1


# ---------------------------------------------------------------------------
# AtlanClientMixin — cache hit (second call returns same instance)
# ---------------------------------------------------------------------------


class TestAtlanClientMixinCacheHit:
    @pytest.mark.asyncio
    async def test_returns_cached_client_without_resolving(self) -> None:
        cred_ref = CredentialRef(name="my-atlan", credential_type="atlan_api_token")
        mock_client = MagicMock()

        store: dict = {f"async_atlan_client:{cred_ref.name}": mock_client}
        app_state = MagicMock()
        app_state.get.side_effect = lambda key: store.get(key)
        app_state.set.side_effect = lambda key, value: store.__setitem__(key, value)

        mixin = _make_mixin_app(app_state)

        result = await mixin.get_or_create_async_atlan_client(cred_ref)

        assert result is mock_client
        mixin.context.resolve_credential.assert_not_awaited()


# ---------------------------------------------------------------------------
# AtlanClientMixin — validated client reuse
# ---------------------------------------------------------------------------


class TestAtlanClientMixinValidatedClientReuse:
    @pytest.mark.asyncio
    async def test_reuses_validated_client_key(self) -> None:
        """A client stored under _VALIDATED_ASYNC_CLIENT_KEY is reused directly."""
        cred_ref = CredentialRef(name="my-atlan", credential_type="atlan_api_token")
        mock_validated_client = MagicMock()

        store: dict = {_VALIDATED_ASYNC_CLIENT_KEY: mock_validated_client}
        app_state = MagicMock()
        app_state.get.side_effect = lambda key: store.get(key)
        app_state.set.side_effect = lambda key, value: store.__setitem__(key, value)

        mixin = _make_mixin_app(app_state)

        result = await mixin.get_or_create_async_atlan_client(cred_ref)

        assert result is mock_validated_client
        mixin.context.resolve_credential.assert_not_awaited()
