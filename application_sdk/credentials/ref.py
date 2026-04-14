"""CredentialRef — secret-free frozen Pydantic model for identifying credentials."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class CredentialRef(BaseModel, frozen=True):
    """A reference to a credential in the secret store.

    This is a secret-free identifier — it contains no sensitive data itself.
    Use a CredentialResolver to turn a CredentialRef into a typed Credential.

    When ``credential_guid`` is non-empty the resolver first checks the local
    secret store (for in-process credential injection in combined-mode / local
    dev), then falls back to ``DaprCredentialVault`` for platform-issued GUIDs.
    """

    model_config = ConfigDict(frozen=True)

    name: str
    """Secret store key or human-readable name for this credential."""

    credential_type: str
    """Type identifier used to look up the parser in the registry (e.g. 'api_key')."""

    store_name: str = "default"
    """Which secret store to use (for multi-store setups)."""

    credential_guid: str = ""
    """Platform-issued credential GUID — non-empty triggers GUID resolution path."""

    def __repr__(self) -> str:
        return (
            f"CredentialRef("
            f"name={self.name!r}, "
            f"credential_type={self.credential_type!r}, "
            f"store_name={self.store_name!r}"
            f")"
        )


# ---------------------------------------------------------------------------
# Factory functions — produce CredentialRef with the correct credential_type
# ---------------------------------------------------------------------------


def api_key_ref(name: str, *, store_name: str = "default") -> CredentialRef:
    """Create a CredentialRef for an API key credential."""
    return CredentialRef(name=name, credential_type="api_key", store_name=store_name)


def basic_ref(name: str, *, store_name: str = "default") -> CredentialRef:
    """Create a CredentialRef for a basic (username/password) credential."""
    return CredentialRef(name=name, credential_type="basic", store_name=store_name)


def bearer_token_ref(name: str, *, store_name: str = "default") -> CredentialRef:
    """Create a CredentialRef for a bearer token credential."""
    return CredentialRef(
        name=name, credential_type="bearer_token", store_name=store_name
    )


def oauth_client_ref(name: str, *, store_name: str = "default") -> CredentialRef:
    """Create a CredentialRef for an OAuth client credential."""
    return CredentialRef(
        name=name, credential_type="oauth_client", store_name=store_name
    )


def certificate_ref(name: str, *, store_name: str = "default") -> CredentialRef:
    """Create a CredentialRef for a certificate credential."""
    return CredentialRef(
        name=name, credential_type="certificate", store_name=store_name
    )


def git_ssh_ref(name: str, *, store_name: str = "default") -> CredentialRef:
    """Create a CredentialRef for a Git SSH credential."""
    return CredentialRef(name=name, credential_type="git_ssh", store_name=store_name)


def git_token_ref(name: str, *, store_name: str = "default") -> CredentialRef:
    """Create a CredentialRef for a Git token (PAT/deploy token) credential."""
    return CredentialRef(name=name, credential_type="git_token", store_name=store_name)


def atlan_api_token_ref(name: str, *, store_name: str = "default") -> CredentialRef:
    """Create a CredentialRef for an Atlan API token credential."""
    return CredentialRef(
        name=name, credential_type="atlan_api_token", store_name=store_name
    )


def atlan_oauth_client_ref(name: str, *, store_name: str = "default") -> CredentialRef:
    """Create a CredentialRef for an Atlan OAuth client credential."""
    return CredentialRef(
        name=name, credential_type="atlan_oauth_client", store_name=store_name
    )


def legacy_credential_ref(guid: str, credential_type: str = "unknown") -> CredentialRef:
    """Create a CredentialRef from a platform-issued credential GUID.

    Wraps a ``credential_guid`` string (issued by the Atlan platform or
    generated in-process by the handler layer) so that templates and
    connectors can resolve it through the standard ``CredentialResolver``
    path.

    The resolver checks the local secret store first (in-process inline
    credentials), then falls back to ``DaprCredentialVault`` for GUIDs
    that only exist in the upstream platform secret store.

    Args:
        guid: The credential GUID.
        credential_type: The credential type hint; defaults to ``"unknown"``
            which causes the resolver to return a ``RawCredential``.
    """
    return CredentialRef(
        name=guid, credential_type=credential_type, credential_guid=guid
    )
