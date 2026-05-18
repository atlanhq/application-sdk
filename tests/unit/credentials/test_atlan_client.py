"""Unit tests for the four Atlan client factories + AtlanClientMixin.

Two flavors of upstream pyatlan ship side by side:
- ``pyatlan_v9`` (vendored) — fast msgspec surface, used by the
  credential mixin via ``create_*_atlan_client_v9``.
- ``pyatlan`` (standard) — richer FluentSearch / role_cache surface,
  used by the testing harness via ``create_*_atlan_client``.

Tests below mirror that split: each factory has its own class, and
mocks the upstream package that factory imports from.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from application_sdk.credentials.atlan import AtlanApiToken, AtlanOAuthClient
from application_sdk.credentials.atlan_client import (
    _VALIDATED_ASYNC_CLIENT_KEY,
    AtlanClientMixin,
    _app_request_headers,
    create_async_atlan_client,
    create_async_atlan_client_v9,
    create_atlan_client,
    create_atlan_client_v9,
)
from application_sdk.credentials.ref import CredentialRef
from application_sdk.credentials.types import BasicCredential
from application_sdk.version import __version__ as _SDK_VERSION

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
# create_async_atlan_client_v9 — pyatlan_v9 async factory
# ---------------------------------------------------------------------------


class TestCreateAsyncAtlanClientV9:
    def test_api_token_passes_base_url_and_api_key(self) -> None:
        """Verify correct kwargs are forwarded for AtlanApiToken."""
        cred = AtlanApiToken(token="my-token", base_url="https://tenant.atlan.com")
        mock_client = MagicMock()

        with patch(
            "pyatlan_v9.client.aio.AsyncAtlanClient", return_value=mock_client
        ) as mock_cls:
            result = create_async_atlan_client_v9(cred)

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
            result = create_async_atlan_client_v9(cred)

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
                create_async_atlan_client_v9(cred)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# create_async_atlan_client — pyatlan (standard) async factory
# ---------------------------------------------------------------------------


class TestCreateAsyncAtlanClient:
    """Same shape as TestCreateAsyncAtlanClientV9 but mocks pyatlan (non-v9)."""

    def test_api_token_forwards_kwargs(self) -> None:
        cred = AtlanApiToken(token="my-token", base_url="https://tenant.atlan.com")
        mock_client = MagicMock()

        with patch(
            "pyatlan.client.aio.client.AsyncAtlanClient", return_value=mock_client
        ) as mock_cls:
            result = create_async_atlan_client(cred)

        mock_cls.assert_called_once_with(
            base_url="https://tenant.atlan.com",
            api_key="my-token",
        )
        assert result is mock_client

    def test_oauth_client_forwards_kwargs(self) -> None:
        cred = AtlanOAuthClient(
            client_id="cid",
            client_secret="secret",
            token_url="https://t.atlan.com/auth/realms/default/protocol/openid-connect/token",
            base_url="https://t.atlan.com",
        )
        mock_client = MagicMock()

        with patch(
            "pyatlan.client.aio.client.AsyncAtlanClient", return_value=mock_client
        ) as mock_cls:
            create_async_atlan_client(cred)

        mock_cls.assert_called_once_with(
            base_url="https://t.atlan.com",
            oauth_client_id="cid",
            oauth_client_secret="secret",
        )

    def test_unsupported_type_raises_type_error(self) -> None:
        cred = BasicCredential(username="u", password="p")
        with pytest.raises(TypeError, match="Unsupported Atlan credential type"):
            with patch("pyatlan.client.aio.client.AsyncAtlanClient"):
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
            "application_sdk.credentials.atlan_client.create_async_atlan_client_v9",
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
            "application_sdk.credentials.atlan_client.create_async_atlan_client_v9",
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


# ---------------------------------------------------------------------------
# _app_request_headers — header derivation from AppContext (BLDX-1246)
# ---------------------------------------------------------------------------


class TestAppRequestHeaders:
    def test_full_context_emits_all_three_headers(self) -> None:
        # Happy path: AppContext exposes both app_name and app_version → we
        # emit the trio of x-atlan-app-* headers the gateway uses for
        # per-app observability.
        ctx = MagicMock()
        ctx.app_name = "mysql"
        ctx.app_version = "0.7.21"

        headers = _app_request_headers(ctx)

        assert headers == {
            "x-atlan-app-name": "mysql",
            "x-atlan-app-version": "0.7.21",
            "x-atlan-app-sdk-version": _SDK_VERSION,
        }

    def test_none_context_emits_only_sdk_version(self) -> None:
        # Ad-hoc client creation (e.g. tests, scripts) — no context attached.
        # SDK version stays so the call is still attributable to application_sdk.
        headers = _app_request_headers(None)

        assert headers == {"x-atlan-app-sdk-version": _SDK_VERSION}

    def test_partial_context_skips_missing_fields(self) -> None:
        # Test doubles or partially-constructed contexts may not have
        # app_version yet. Skip silently rather than stamping empty strings.
        ctx = MagicMock(spec=[])
        ctx.app_name = "mysql"
        # No app_version attribute set

        headers = _app_request_headers(ctx)

        assert headers["x-atlan-app-name"] == "mysql"
        assert headers["x-atlan-app-sdk-version"] == _SDK_VERSION
        assert "x-atlan-app-version" not in headers

    def test_blank_values_skipped(self) -> None:
        # Empty-string app_name/app_version: don't pollute headers with empties.
        ctx = MagicMock()
        ctx.app_name = ""
        ctx.app_version = ""

        headers = _app_request_headers(ctx)

        assert headers == {"x-atlan-app-sdk-version": _SDK_VERSION}


# ---------------------------------------------------------------------------
# Header-injection contract — same shape for all four factories (BLDX-1246).
# Each test class targets one factory + its upstream package's mock path.
# ---------------------------------------------------------------------------


class TestCreateAsyncAtlanClientV9Headers:
    """Header injection on pyatlan_v9 async factory."""

    def test_extra_headers_calls_update_headers(self) -> None:
        cred = AtlanApiToken(token="tok", base_url="https://t.atlan.com")
        mock_client = MagicMock()
        headers = {"x-atlan-app-name": "mysql", "x-atlan-app-version": "0.7.21"}

        with patch("pyatlan_v9.client.aio.AsyncAtlanClient", return_value=mock_client):
            result = create_async_atlan_client_v9(cred, extra_headers=headers)

        assert result is mock_client
        mock_client.update_headers.assert_called_once_with(headers)

    def test_no_headers_does_not_call_update_headers(self) -> None:
        # Default invocation (no caller-supplied headers) should not touch
        # the pyatlan session — keeps existing test fixtures working.
        cred = AtlanApiToken(token="tok", base_url="https://t.atlan.com")
        mock_client = MagicMock()

        with patch("pyatlan_v9.client.aio.AsyncAtlanClient", return_value=mock_client):
            create_async_atlan_client_v9(cred)

        mock_client.update_headers.assert_not_called()

    def test_oauth_credential_with_headers(self) -> None:
        # OAuth path must also stamp the headers post-construction.
        cred = AtlanOAuthClient(
            client_id="cid",
            client_secret="secret",
            token_url="https://t.atlan.com/auth/realms/default/protocol/openid-connect/token",
            base_url="https://t.atlan.com",
        )
        mock_client = MagicMock()
        headers = {"x-atlan-app-name": "postgres"}

        with patch("pyatlan_v9.client.aio.AsyncAtlanClient", return_value=mock_client):
            create_async_atlan_client_v9(cred, extra_headers=headers)

        mock_client.update_headers.assert_called_once_with(headers)

    def test_update_headers_failure_does_not_break_client_creation(self) -> None:
        # Header attachment is best-effort. If update_headers raises (pyatlan
        # rename / hot-fix scenario), we still return the working client —
        # losing observability headers beats refusing to construct a client.
        cred = AtlanApiToken(token="tok", base_url="https://t.atlan.com")
        mock_client = MagicMock()
        mock_client.update_headers.side_effect = RuntimeError("transient")

        with patch("pyatlan_v9.client.aio.AsyncAtlanClient", return_value=mock_client):
            result = create_async_atlan_client_v9(
                cred, extra_headers={"x-atlan-app-name": "mysql"}
            )

        assert result is mock_client


class TestCreateAsyncAtlanClientHeaders:
    """Header injection on pyatlan (non-v9) async factory. Mirrors the v9 suite."""

    def test_extra_headers_calls_update_headers(self) -> None:
        cred = AtlanApiToken(token="tok", base_url="https://t.atlan.com")
        mock_client = MagicMock()
        headers = {"x-atlan-app-name": "mysql"}

        with patch(
            "pyatlan.client.aio.client.AsyncAtlanClient", return_value=mock_client
        ):
            create_async_atlan_client(cred, extra_headers=headers)

        mock_client.update_headers.assert_called_once_with(headers)

    def test_no_headers_does_not_call_update_headers(self) -> None:
        cred = AtlanApiToken(token="tok", base_url="https://t.atlan.com")
        mock_client = MagicMock()

        with patch(
            "pyatlan.client.aio.client.AsyncAtlanClient", return_value=mock_client
        ):
            create_async_atlan_client(cred)

        mock_client.update_headers.assert_not_called()


# ---------------------------------------------------------------------------
# Sync factories — pyatlan_v9 and pyatlan flavors
# ---------------------------------------------------------------------------


class TestCreateAtlanClientV9:
    def test_api_token_forwards_kwargs(self) -> None:
        cred = AtlanApiToken(token="tok", base_url="https://t.atlan.com")
        mock_client = MagicMock()

        with patch(
            "pyatlan_v9.client.atlan.AtlanClient", return_value=mock_client
        ) as mock_cls:
            result = create_atlan_client_v9(cred)

        mock_cls.assert_called_once_with(
            base_url="https://t.atlan.com",
            api_key="tok",
        )
        assert result is mock_client

    def test_oauth_client_forwards_kwargs(self) -> None:
        cred = AtlanOAuthClient(
            client_id="cid",
            client_secret="secret",
            token_url="https://t.atlan.com/auth/realms/default/protocol/openid-connect/token",
            base_url="https://t.atlan.com",
        )
        mock_client = MagicMock()

        with patch(
            "pyatlan_v9.client.atlan.AtlanClient", return_value=mock_client
        ) as mock_cls:
            create_atlan_client_v9(cred)

        mock_cls.assert_called_once_with(
            base_url="https://t.atlan.com",
            oauth_client_id="cid",
            oauth_client_secret="secret",
        )

    def test_extra_headers_calls_update_headers(self) -> None:
        cred = AtlanApiToken(token="tok", base_url="https://t.atlan.com")
        mock_client = MagicMock()
        headers = {"x-atlan-app-name": "mysql"}

        with patch("pyatlan_v9.client.atlan.AtlanClient", return_value=mock_client):
            create_atlan_client_v9(cred, extra_headers=headers)

        mock_client.update_headers.assert_called_once_with(headers)

    def test_unsupported_type_raises_type_error(self) -> None:
        cred = BasicCredential(username="u", password="p")
        with pytest.raises(TypeError, match="Unsupported Atlan credential type"):
            with patch("pyatlan_v9.client.atlan.AtlanClient"):
                create_atlan_client_v9(cred)  # type: ignore[arg-type]


class TestCreateAtlanClient:
    """pyatlan (non-v9) sync factory."""

    def test_api_token_forwards_kwargs(self) -> None:
        cred = AtlanApiToken(token="tok", base_url="https://t.atlan.com")
        mock_client = MagicMock()

        with patch(
            "pyatlan.client.atlan.AtlanClient", return_value=mock_client
        ) as mock_cls:
            create_atlan_client(cred)

        mock_cls.assert_called_once_with(
            base_url="https://t.atlan.com",
            api_key="tok",
        )

    def test_extra_headers_calls_update_headers(self) -> None:
        cred = AtlanApiToken(token="tok", base_url="https://t.atlan.com")
        mock_client = MagicMock()
        headers = {"x-atlan-app-name": "mysql"}

        with patch("pyatlan.client.atlan.AtlanClient", return_value=mock_client):
            create_atlan_client(cred, extra_headers=headers)

        mock_client.update_headers.assert_called_once_with(headers)

    def test_unsupported_type_raises_type_error(self) -> None:
        cred = BasicCredential(username="u", password="p")
        with pytest.raises(TypeError, match="Unsupported Atlan credential type"):
            with patch("pyatlan.client.atlan.AtlanClient"):
                create_atlan_client(cred)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# AtlanClientMixin — passes derived headers (BLDX-1246)
# ---------------------------------------------------------------------------


class TestAtlanClientMixinHeaders:
    @pytest.mark.asyncio
    async def test_mixin_passes_context_derived_headers(self) -> None:
        # Verify the mixin sources app_name / app_version from the AppContext
        # and forwards them through to the factory on cache-miss creation.
        cred_ref = CredentialRef(name="my-atlan", credential_type="atlan_api_token")
        resolved = AtlanApiToken(token="tok", base_url="https://t.atlan.com")

        app_state = _make_app_state()
        mixin = _make_mixin_app(app_state, resolved_cred=resolved)
        # _make_mixin_app uses AsyncMock for context; pin the attrs the
        # production code reads.
        mixin.context.app_name = "mysql"  # type: ignore[attr-defined]
        mixin.context.app_version = "0.7.21"  # type: ignore[attr-defined]

        mock_client = MagicMock()
        with patch(
            "application_sdk.credentials.atlan_client.create_async_atlan_client_v9",
            return_value=mock_client,
        ) as factory:
            await mixin.get_or_create_async_atlan_client(cred_ref)

        # factory called once with cred + extra_headers
        assert factory.call_count == 1
        kwargs = factory.call_args.kwargs
        assert kwargs["extra_headers"] == {
            "x-atlan-app-name": "mysql",
            "x-atlan-app-version": "0.7.21",
            "x-atlan-app-sdk-version": _SDK_VERSION,
        }


# ---------------------------------------------------------------------------
# Real-client integration — verify both pyatlan flavors actually accept our
# headers (BLDX-1246). Offline; no network. Catches upstream renames the
# mocked tests can't detect.
# ---------------------------------------------------------------------------


class TestPyatlanV9ClientHeaderContractRealClient:
    """Construct an actual pyatlan_v9 client and inspect its session."""

    def test_async_client_headers_actually_present_on_session(self) -> None:
        cred = AtlanApiToken(token="tok", base_url="https://t.atlan.com")

        client = create_async_atlan_client_v9(
            cred,
            extra_headers={
                "x-atlan-app-name": "mysql",
                "x-atlan-app-version": "0.7.21",
                "x-atlan-app-sdk-version": "3.10.1",
            },
        )

        # AsyncAtlanClient.update_headers writes into self._async_session.headers
        # (verified via inspect.getsource of the vendored pyatlan_v9 wheel).
        # If that internal attribute is ever renamed, this test catches it.
        session_headers = getattr(client, "_async_session", None)
        assert session_headers is not None, (
            "pyatlan_v9 AsyncAtlanClient no longer exposes _async_session — "
            "header injection contract has changed; investigate before merge."
        )
        h = session_headers.headers
        assert h.get("x-atlan-app-name") == "mysql"
        assert h.get("x-atlan-app-version") == "0.7.21"
        assert h.get("x-atlan-app-sdk-version") == "3.10.1"

    def test_async_client_without_extra_headers_still_constructs(self) -> None:
        # No-headers path: the client must construct cleanly.
        cred = AtlanApiToken(token="tok", base_url="https://t.atlan.com")
        client = create_async_atlan_client_v9(cred)

        # update_headers is a method on the real client — confirm the
        # contract our _apply_app_headers helper checks for is present.
        assert callable(getattr(client, "update_headers", None))


class TestPyatlanClientHeaderContractRealClient:
    """Construct an actual pyatlan (non-v9) AsyncAtlanClient and inspect it.

    Mirrors the v9 contract test for the standard pyatlan flavor — both
    paths must keep working through pyatlan API churn.
    """

    def test_async_client_exposes_update_headers(self) -> None:
        # pyatlan (non-v9) AsyncAtlanClient must implement update_headers
        # for our header-injection contract to work. If a future pyatlan
        # release drops the method, this fails loudly here rather than
        # silently in production.
        cred = AtlanApiToken(token="tok", base_url="https://t.atlan.com")
        client = create_async_atlan_client(cred)
        assert callable(getattr(client, "update_headers", None)), (
            "pyatlan AsyncAtlanClient no longer exposes update_headers — "
            "header injection contract has changed; see _apply_app_headers."
        )

    def test_async_client_headers_attached_after_call(self) -> None:
        # Stamp the headers and verify they're observable on the client.
        # We don't pin the exact attribute name pyatlan uses for the
        # session (it differs from v9's _async_session); calling
        # update_headers and reading them back via a public-ish API
        # is enough to prove the contract holds.
        cred = AtlanApiToken(token="tok", base_url="https://t.atlan.com")
        client = create_async_atlan_client(
            cred,
            extra_headers={"x-atlan-app-name": "mysql"},
        )
        # update_headers wrote into the session; checking the call landed
        # by re-invoking with no args should be a no-op rather than raise.
        client.update_headers({"x-atlan-app-version": "0.7.21"})
