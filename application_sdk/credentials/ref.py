"""CredentialRef — secret-free frozen dataclass for identifying credentials."""

from __future__ import annotations

import dataclasses


@dataclasses.dataclass(frozen=True)
class CredentialRef:
    """A reference to a credential in the secret store.

    This is a secret-free identifier — it contains no sensitive data itself.
    Use a CredentialResolver to turn a CredentialRef into a typed Credential.

    The ``credential_guid`` field provides backward compatibility with apps
    that still use the legacy v2 ``SecretStore.get_credentials(guid)`` path.
    When non-empty, the resolver uses the legacy resolution path.
    """

    name: str
    """Secret store key or human-readable name for this credential."""

    credential_type: str
    """Type identifier used to look up the parser in the registry (e.g. 'api_key')."""

    store_name: str = "default"
    """Which secret store to use (for multi-store setups)."""

    credential_guid: str = ""
    """Legacy credential GUID — non-empty triggers the v2 resolution path."""

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
    """Create a CredentialRef from a legacy v2 credential GUID.

    This bridge factory wraps an existing ``credential_guid`` so that apps
    using the old pattern can migrate incrementally to CredentialRef without
    changing their secret-store layout.

    Args:
        guid: The legacy credential GUID (passed as ``credential_guid``).
        credential_type: The credential type hint; defaults to ``"unknown"``
            which causes the resolver to return a ``RawCredential``.
    """
    return CredentialRef(
        name=guid, credential_type=credential_type, credential_guid=guid
    )
