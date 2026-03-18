"""MockCredentialStore — in-memory credential store for unit tests."""

from __future__ import annotations

import json
from typing import Any

from application_sdk.credentials.ref import (
    CredentialRef,
    api_key_ref,
    atlan_api_token_ref,
    atlan_oauth_client_ref,
    basic_ref,
    bearer_token_ref,
    certificate_ref,
    git_ssh_ref,
    git_token_ref,
    oauth_client_ref,
)
from application_sdk.infrastructure.secrets import InMemorySecretStore


class MockCredentialStore:
    """In-memory credential store for unit tests.

    Serializes credential data as JSON and stores it via ``InMemorySecretStore``.
    Each ``add_*`` method returns a ``CredentialRef`` that can be passed to a
    ``CredentialResolver`` or ``AppContext.resolve_credential()``.

    Usage::

        store = MockCredentialStore()
        ref = store.add_api_key("my-service", api_key="secret123")

        # Inject the backing store into a CredentialResolver or AppContext
        resolver = CredentialResolver(store.secret_store)
        cred = await resolver.resolve(ref)
        assert isinstance(cred, ApiKeyCredential)
    """

    def __init__(self) -> None:
        self._store = InMemorySecretStore()
        self._refs: dict[str, CredentialRef] = {}

    @property
    def secret_store(self) -> InMemorySecretStore:
        """Return the backing InMemorySecretStore for injection into resolvers."""
        return self._store

    # ------------------------------------------------------------------
    # add_* methods — one per built-in credential type
    # ------------------------------------------------------------------

    def add_api_key(
        self,
        name: str,
        api_key: str,
        *,
        header_name: str = "X-API-Key",
        prefix: str = "",
    ) -> CredentialRef:
        """Store an API key credential and return its CredentialRef."""
        data = {
            "type": "api_key",
            "api_key": api_key,
            "header_name": header_name,
            "prefix": prefix,
        }
        return self._store_and_ref(name, data, api_key_ref(name))

    def add_basic(self, name: str, username: str, password: str) -> CredentialRef:
        """Store a basic (username/password) credential and return its CredentialRef."""
        data = {"type": "basic", "username": username, "password": password}
        return self._store_and_ref(name, data, basic_ref(name))

    def add_bearer_token(
        self,
        name: str,
        token: str,
        *,
        expires_at: str = "",
    ) -> CredentialRef:
        """Store a bearer token credential and return its CredentialRef."""
        data = {"type": "bearer_token", "token": token, "expires_at": expires_at}
        return self._store_and_ref(name, data, bearer_token_ref(name))

    def add_oauth_client(
        self,
        name: str,
        client_id: str,
        client_secret: str,
        token_url: str,
        *,
        scopes: list[str] | None = None,
        access_token: str = "",
        refresh_token: str = "",
        expires_at: str = "",
    ) -> CredentialRef:
        """Store an OAuth client credential and return its CredentialRef."""
        data: dict[str, Any] = {
            "type": "oauth_client",
            "client_id": client_id,
            "client_secret": client_secret,
            "token_url": token_url,
            "scopes": scopes or [],
            "access_token": access_token,
            "refresh_token": refresh_token,
            "expires_at": expires_at,
        }
        return self._store_and_ref(name, data, oauth_client_ref(name))

    def add_certificate(
        self,
        name: str,
        *,
        cert_data: str = "",
        key_data: str = "",
        ca_data: str = "",
        passphrase: str = "",
    ) -> CredentialRef:
        """Store a certificate credential and return its CredentialRef."""
        data = {
            "type": "certificate",
            "cert_data": cert_data,
            "key_data": key_data,
            "ca_data": ca_data,
            "passphrase": passphrase,
        }
        return self._store_and_ref(name, data, certificate_ref(name))

    def add_git_ssh(
        self,
        name: str,
        key_data: str,
        *,
        passphrase: str = "",
    ) -> CredentialRef:
        """Store a Git SSH credential and return its CredentialRef."""
        data = {
            "type": "git_ssh",
            "key_data": key_data,
            "passphrase": passphrase,
        }
        return self._store_and_ref(name, data, git_ssh_ref(name))

    def add_git_token(self, name: str, token: str) -> CredentialRef:
        """Store a Git token credential and return its CredentialRef."""
        data = {"type": "git_token", "token": token}
        return self._store_and_ref(name, data, git_token_ref(name))

    def add_atlan_api_token(
        self, name: str, token: str, base_url: str
    ) -> CredentialRef:
        """Store an Atlan API token credential and return its CredentialRef."""
        data = {"type": "atlan_api_token", "token": token, "base_url": base_url}
        return self._store_and_ref(name, data, atlan_api_token_ref(name))

    def add_atlan_oauth_client(
        self,
        name: str,
        client_id: str,
        client_secret: str,
        base_url: str,
        *,
        token_url: str = "",
        scopes: list[str] | None = None,
        access_token: str = "",
        refresh_token: str = "",
        expires_at: str = "",
    ) -> CredentialRef:
        """Store an Atlan OAuth client credential and return its CredentialRef."""
        data: dict[str, Any] = {
            "type": "atlan_oauth_client",
            "client_id": client_id,
            "client_secret": client_secret,
            "base_url": base_url,
            "token_url": token_url,
            "scopes": scopes or [],
            "access_token": access_token,
            "refresh_token": refresh_token,
            "expires_at": expires_at,
        }
        return self._store_and_ref(name, data, atlan_oauth_client_ref(name))

    def add_raw(
        self, name: str, credential_type: str, data: dict[str, Any]
    ) -> CredentialRef:
        """Store an arbitrary credential dict and return its CredentialRef."""
        payload = {"type": credential_type, **data}
        ref = CredentialRef(name=name, credential_type=credential_type)
        return self._store_and_ref(name, payload, ref)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _store_and_ref(
        self, name: str, data: dict[str, Any], ref: CredentialRef
    ) -> CredentialRef:
        self._store.set(name, json.dumps(data))
        self._refs[name] = ref
        return ref
