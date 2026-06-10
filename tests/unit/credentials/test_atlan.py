"""Tests for Atlan-specific typed credentials (credentials/atlan.py).

Covers:
- AtlanApiToken.validate (success + missing token / base_url)
- AtlanOAuthClient.validate (success + missing field branches)
- effective_token_url (token_url set / Heracles override / base_url derived /
  empty fallback)
- build_token_request_data (Keycloak vs Heracles, with/without scopes)
- with_new_token preserves AtlanOAuthClient subclass + base_url
- _parse_atlan_api_token / _parse_atlan_oauth_client (round-trips)
- BLDX-1129 anchor: inline ``pyatlan_v9.client.aio.AsyncAtlanClient`` and
  inline ``CredentialValidationError`` imports must be exercised.

pyatlan_v9 is mocked via ``sys.modules`` injection — never instantiated.
"""

from __future__ import annotations

import sys
from types import ModuleType
from unittest.mock import AsyncMock, MagicMock

import pytest

from application_sdk.credentials.atlan import (
    AtlanApiToken,
    AtlanOAuthClient,
    _parse_atlan_api_token,
    _parse_atlan_oauth_client,
)
from application_sdk.credentials.errors import CredentialValidationError

# ---------------------------------------------------------------------------
# pyatlan_v9 mocking helper
# ---------------------------------------------------------------------------


def _install_fake_pyatlan(get_current_side_effect: object | None = None):
    """Install a fake pyatlan_v9 module hierarchy in sys.modules.

    Returns the AsyncAtlanClient mock so tests can assert it was called.
    """
    if get_current_side_effect is None:

        async def _ok():
            return MagicMock()

        get_current = AsyncMock(side_effect=_ok)
    elif isinstance(get_current_side_effect, Exception):
        get_current = AsyncMock(side_effect=get_current_side_effect)
    else:
        get_current = AsyncMock(return_value=get_current_side_effect)

    fake_client_instance = MagicMock()
    fake_client_instance.user.get_current = get_current

    fake_async_client_cls = MagicMock(return_value=fake_client_instance)

    aio_module = ModuleType("pyatlan_v9.client.aio")
    aio_module.AsyncAtlanClient = fake_async_client_cls
    client_module = ModuleType("pyatlan_v9.client")
    client_module.aio = aio_module
    root_module = ModuleType("pyatlan_v9")
    root_module.client = client_module

    sys.modules["pyatlan_v9"] = root_module
    sys.modules["pyatlan_v9.client"] = client_module
    sys.modules["pyatlan_v9.client.aio"] = aio_module
    return fake_async_client_cls, fake_client_instance


@pytest.fixture
def restore_pyatlan_modules():
    """Snapshot/restore pyatlan_v9 entries in sys.modules across tests."""
    keys = ("pyatlan_v9", "pyatlan_v9.client", "pyatlan_v9.client.aio")
    snapshot = {k: sys.modules.get(k) for k in keys}
    yield
    for k in keys:
        if snapshot[k] is None:
            sys.modules.pop(k, None)
        else:
            sys.modules[k] = snapshot[k]


# ---------------------------------------------------------------------------
# AtlanApiToken
# ---------------------------------------------------------------------------


class TestAtlanApiToken:
    def test_credential_type(self):
        cred = AtlanApiToken(token="t", base_url="https://x.atlan.com")
        assert cred.credential_type == "atlan_api_token"

    def test_default_base_url_is_empty(self):
        cred = AtlanApiToken(token="t")
        assert cred.base_url == ""

    def test_frozen_field_immutable(self):
        from pydantic import ValidationError

        cred = AtlanApiToken(token="t", base_url="https://x.atlan.com")
        with pytest.raises((ValidationError, AttributeError, TypeError)):
            cred.base_url = "other"  # type: ignore[misc]

    @pytest.mark.asyncio
    async def test_validate_empty_token_raises(self):
        """Empty token → CredentialValidationError, no pyatlan import needed."""
        cred = AtlanApiToken(token="", base_url="https://x.atlan.com")
        with pytest.raises(CredentialValidationError, match="token must not be empty"):
            await cred.validate()

    @pytest.mark.asyncio
    async def test_validate_empty_base_url_raises(self):
        cred = AtlanApiToken(token="tok", base_url="")
        with pytest.raises(
            CredentialValidationError, match="base_url must not be empty"
        ):
            await cred.validate()

    @pytest.mark.asyncio
    async def test_validate_success_invokes_pyatlan(self, restore_pyatlan_modules):
        """Success path imports pyatlan_v9 (BLDX-1129 anchor) and calls
        user.get_current."""
        fake_cls, fake_instance = _install_fake_pyatlan()
        cred = AtlanApiToken(token="tok", base_url="https://x.atlan.com")
        await cred.validate()
        fake_cls.assert_called_once_with(base_url="https://x.atlan.com", api_key="tok")
        fake_instance.user.get_current.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_validate_pyatlan_failure_wrapped(self, restore_pyatlan_modules):
        """If pyatlan raises, error is wrapped with original cause."""
        _install_fake_pyatlan(get_current_side_effect=RuntimeError("network down"))
        cred = AtlanApiToken(token="tok", base_url="https://x.atlan.com")
        with pytest.raises(CredentialValidationError, match="validation failed") as exc:
            await cred.validate()
        assert isinstance(exc.value.__cause__, RuntimeError)
        assert exc.value.credential_name == "atlan_api_token"


# ---------------------------------------------------------------------------
# AtlanOAuthClient — properties and helpers
# ---------------------------------------------------------------------------


class TestAtlanOAuthClientProperties:
    def _make(self, **kwargs) -> AtlanOAuthClient:
        defaults = dict(
            client_id="cid",
            client_secret="csec",
            token_url="",
            base_url="https://t.atlan.com",
        )
        defaults.update(kwargs)
        return AtlanOAuthClient(**defaults)

    def test_credential_type(self):
        assert self._make().credential_type == "atlan_oauth_client"

    def test_effective_token_url_uses_explicit_value(self, monkeypatch):
        monkeypatch.delenv("ATLAN_INTERNAL_HERACLES_URL", raising=False)
        cred = self._make(token_url="https://override/oauth/token")
        assert cred.effective_token_url == "https://override/oauth/token"

    def test_effective_token_url_uses_internal_heracles(self, monkeypatch):
        monkeypatch.setenv(
            "ATLAN_INTERNAL_HERACLES_URL", "http://heracles.svc.cluster.local/"
        )
        cred = self._make()
        assert (
            cred.effective_token_url == "http://heracles.svc.cluster.local/oauth/token"
        )

    def test_effective_token_url_uses_base_url(self, monkeypatch):
        monkeypatch.delenv("ATLAN_INTERNAL_HERACLES_URL", raising=False)
        cred = self._make(base_url="https://t.atlan.com/")
        assert cred.effective_token_url == (
            "https://t.atlan.com/auth/realms/default/protocol/openid-connect/token"
        )

    def test_effective_token_url_empty_when_nothing_set(self, monkeypatch):
        monkeypatch.delenv("ATLAN_INTERNAL_HERACLES_URL", raising=False)
        cred = self._make(base_url="")
        assert cred.effective_token_url == ""

    def test_build_token_request_data_keycloak_form(self, monkeypatch):
        monkeypatch.delenv("ATLAN_INTERNAL_HERACLES_URL", raising=False)
        cred = self._make()
        data, use_json = cred.build_token_request_data()
        assert use_json is False
        assert data == {
            "client_id": "cid",
            "client_secret": "csec",
            "grant_type": "client_credentials",
        }

    def test_build_token_request_data_keycloak_form_with_scopes(self, monkeypatch):
        monkeypatch.delenv("ATLAN_INTERNAL_HERACLES_URL", raising=False)
        cred = self._make(scopes=("read", "write"))
        data, use_json = cred.build_token_request_data()
        assert use_json is False
        assert data["scope"] == "read write"

    def test_build_token_request_data_heracles_json(self, monkeypatch):
        monkeypatch.setenv(
            "ATLAN_INTERNAL_HERACLES_URL", "http://heracles.svc.cluster.local"
        )
        cred = self._make(scopes=("read",))
        data, use_json = cred.build_token_request_data()
        assert use_json is True
        assert data == {
            "clientId": "cid",
            "clientSecret": "csec",
            "grantType": "client_credentials",
            "scope": "read",
        }

    def test_build_token_request_data_heracles_no_scopes(self, monkeypatch):
        monkeypatch.setenv(
            "ATLAN_INTERNAL_HERACLES_URL", "http://heracles.svc.cluster.local"
        )
        cred = self._make()
        data, use_json = cred.build_token_request_data()
        assert use_json is True
        assert "scope" not in data

    def test_with_new_token_preserves_subclass_and_base_url(self):
        """Override returns AtlanOAuthClient (not OAuthClientCredential),
        preserving Atlan-specific base_url field."""
        cred = self._make(
            access_token="old",
            refresh_token="r1",
            base_url="https://t.atlan.com",
        )
        updated = cred.with_new_token(
            access_token="new", expires_at="2099-01-01T00:00:00Z"
        )
        assert isinstance(updated, AtlanOAuthClient)
        assert updated.access_token == "new"
        assert updated.refresh_token == "r1"  # preserved when not provided
        assert updated.expires_at == "2099-01-01T00:00:00Z"
        assert updated.base_url == "https://t.atlan.com"

    def test_with_new_token_overrides_refresh_token_when_given(self):
        cred = self._make(refresh_token="r1")
        updated = cred.with_new_token(access_token="new", refresh_token="r2")
        assert updated.refresh_token == "r2"


# ---------------------------------------------------------------------------
# AtlanOAuthClient.validate
# ---------------------------------------------------------------------------


class TestAtlanOAuthClientValidate:
    def _make(self, **kwargs) -> AtlanOAuthClient:
        defaults = dict(
            client_id="cid",
            client_secret="csec",
            token_url="https://t/token",
            base_url="https://t.atlan.com",
            access_token="atok",
        )
        defaults.update(kwargs)
        return AtlanOAuthClient(**defaults)

    @pytest.mark.asyncio
    async def test_validate_missing_client_id(self):
        cred = self._make(client_id="")
        with pytest.raises(CredentialValidationError, match="client_id"):
            await cred.validate()

    @pytest.mark.asyncio
    async def test_validate_missing_client_secret(self):
        cred = self._make(client_secret="")
        with pytest.raises(CredentialValidationError, match="client_secret"):
            await cred.validate()

    @pytest.mark.asyncio
    async def test_validate_missing_base_url(self):
        cred = self._make(base_url="")
        with pytest.raises(CredentialValidationError, match="base_url"):
            await cred.validate()

    @pytest.mark.asyncio
    async def test_validate_missing_access_token(self):
        cred = self._make(access_token="")
        with pytest.raises(
            CredentialValidationError, match="access_token must not be empty"
        ):
            await cred.validate()

    @pytest.mark.asyncio
    async def test_validate_success_invokes_pyatlan(self, restore_pyatlan_modules):
        """Success path uses inline pyatlan_v9 import (BLDX-1129 anchor).

        Verifies api_key passed is the access_token (not the secret).
        """
        fake_cls, fake_instance = _install_fake_pyatlan()
        cred = self._make(access_token="atok")
        await cred.validate()
        fake_cls.assert_called_once_with(base_url="https://t.atlan.com", api_key="atok")
        fake_instance.user.get_current.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_validate_pyatlan_failure_wrapped(self, restore_pyatlan_modules):
        _install_fake_pyatlan(get_current_side_effect=RuntimeError("401"))
        cred = self._make()
        with pytest.raises(CredentialValidationError, match="validation failed") as exc:
            await cred.validate()
        assert isinstance(exc.value.__cause__, RuntimeError)
        assert exc.value.credential_name == "atlan_oauth_client"


# ---------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------


class TestParseAtlanApiToken:
    def test_parses_full_payload(self):
        cred = _parse_atlan_api_token(
            {
                "token": "tok",
                "expires_at": "2099-01-01T00:00:00Z",
                "base_url": "https://t.atlan.com",
            }
        )
        assert cred.token == "tok"
        assert cred.expires_at == "2099-01-01T00:00:00Z"
        assert cred.base_url == "https://t.atlan.com"

    def test_uses_defaults_for_missing_keys(self):
        cred = _parse_atlan_api_token({})
        assert cred.token == ""
        assert cred.expires_at == ""
        assert cred.base_url == ""


class TestParseAtlanOAuthClient:
    def test_parses_full_payload_with_list_scopes(self):
        cred = _parse_atlan_oauth_client(
            {
                "client_id": "cid",
                "client_secret": "csec",
                "token_url": "https://t/token",
                "scopes": ["read", "write"],
                "access_token": "atok",
                "refresh_token": "rtok",
                "expires_at": "2099-01-01T00:00:00Z",
                "base_url": "https://t.atlan.com",
            }
        )
        assert cred.client_id == "cid"
        assert cred.scopes == ("read", "write")
        assert cred.base_url == "https://t.atlan.com"

    def test_handles_non_list_scopes_defensively(self):
        """A non-list scopes value (e.g., None) should produce empty tuple."""
        cred = _parse_atlan_oauth_client(
            {"client_id": "cid", "client_secret": "csec", "scopes": None}
        )
        assert cred.scopes == ()

    def test_uses_defaults_for_missing_keys(self):
        cred = _parse_atlan_oauth_client({})
        assert cred.client_id == ""
        assert cred.client_secret == ""
        assert cred.scopes == ()
        assert cred.access_token == ""
        assert cred.base_url == ""


# ---------------------------------------------------------------------------
# Security: token leakage in validation error messages
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_validate_does_not_leak_token_in_error_message(
    restore_pyatlan_modules,
):
    """The user-facing CredentialValidationError ``.message`` must not
    embed the underlying pyatlan exception text — that text could include
    the api_key/access_token. Locks the BLDX-1166 fix on PR #1601 which
    switched to ``type(exc).__name__`` in the message.

    Note: ``CredentialError.__str__`` still composes ``cause`` into the
    full string repr, so ``str(exc.value)`` is allowed to contain the
    secret transitively. Logger / response-handler code is responsible
    for using ``.message`` (or the structured error code), not the full
    str representation.
    """
    secret = "super-secret-token-do-not-leak"
    _install_fake_pyatlan(
        get_current_side_effect=RuntimeError(f"401 with key {secret}")
    )
    cred = AtlanApiToken(token=secret, base_url="https://x.atlan.com")
    with pytest.raises(CredentialValidationError) as exc:
        await cred.validate()
    # The message itself is structured + redacted — uses type name only.
    assert secret not in exc.value.message
    assert "RuntimeError" in exc.value.message


# ---------------------------------------------------------------------------
# base_url scheme validation (https-only, http for loopback dev)
# ---------------------------------------------------------------------------


class TestBaseUrlSchemeValidation:
    """Construction-time validation of base_url on both Atlan credentials."""

    @pytest.mark.parametrize(
        "base_url",
        [
            "https://tenant.atlan.com",
            "https://tenant.atlan.com/",
            "http://localhost:8000",
            "http://127.0.0.1:8000",
            "",  # empty stays allowed — fallback chains handle it
        ],
    )
    def test_api_token_accepts_valid_base_url(self, base_url: str) -> None:
        cred = AtlanApiToken(token="t", base_url=base_url)
        assert cred.base_url == base_url

    @pytest.mark.parametrize(
        "base_url",
        [
            "http://tenant.atlan.com",
            "http://10.0.0.5:8080",
            "ftp://tenant.atlan.com",
            "tenant.atlan.com",  # no scheme
        ],
    )
    def test_api_token_rejects_invalid_base_url(self, base_url: str) -> None:
        with pytest.raises(CredentialValidationError) as exc_info:
            AtlanApiToken(token="t", base_url=base_url)
        assert "https" in str(exc_info.value)
        # Never echo the URL itself (may embed credentials).
        assert base_url not in str(exc_info.value)

    @pytest.mark.parametrize(
        "base_url",
        [
            "https://tenant.atlan.com",
            "http://localhost:8000",
            "http://127.0.0.1",
            "",
        ],
    )
    def test_oauth_client_accepts_valid_base_url(self, base_url: str) -> None:
        cred = AtlanOAuthClient(
            client_id="c", client_secret="s", token_url="", base_url=base_url
        )
        assert cred.base_url == base_url

    @pytest.mark.parametrize(
        "base_url",
        [
            "http://tenant.atlan.com",
            "ws://tenant.atlan.com",
        ],
    )
    def test_oauth_client_rejects_invalid_base_url(self, base_url: str) -> None:
        with pytest.raises(CredentialValidationError):
            AtlanOAuthClient(
                client_id="c", client_secret="s", token_url="", base_url=base_url
            )

    def test_parse_helpers_reject_plain_http(self) -> None:
        with pytest.raises(CredentialValidationError):
            _parse_atlan_api_token(
                {"token": "t", "base_url": "http://tenant.atlan.com"}
            )
        with pytest.raises(CredentialValidationError):
            _parse_atlan_oauth_client(
                {
                    "client_id": "c",
                    "client_secret": "s",
                    "base_url": "http://tenant.atlan.com",
                }
            )
