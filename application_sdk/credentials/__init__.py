"""Typed credential system for the Application SDK.

Public API:

    from application_sdk.credentials import (
        # Core ref
        CredentialRef,

        # Factory functions
        api_key_ref, basic_ref, bearer_token_ref, oauth_client_ref,
        certificate_ref, git_ssh_ref, git_token_ref,
        atlan_api_token_ref, atlan_oauth_client_ref,
        legacy_credential_ref,

        # Credential types
        Credential,
        BasicCredential, ApiKeyCredential, BearerTokenCredential,
        OAuthClientCredential, CertificateCredential, RawCredential,

        # Git types
        GitSshCredential, GitTokenCredential,

        # Atlan types
        AtlanApiToken, AtlanOAuthClient,

        # Resolver
        CredentialResolver,

        # Registry
        CredentialTypeRegistry, get_registry, register_credential_type,

        # Errors
        CredentialError, CredentialNotFoundError,
        CredentialParseError, CredentialValidationError,
    )
"""

from application_sdk.credentials.atlan import AtlanApiToken, AtlanOAuthClient
from application_sdk.credentials.atlan_client import (
    AtlanClientMixin,
    create_async_atlan_client,
)
from application_sdk.credentials.errors import (
    CredentialError,
    CredentialNotFoundError,
    CredentialParseError,
    CredentialValidationError,
)
from application_sdk.credentials.git import GitSshCredential, GitTokenCredential
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
    legacy_credential_ref,
    oauth_client_ref,
)
from application_sdk.credentials.registry import (
    CredentialTypeRegistry,
    get_registry,
    register_credential_type,
)
from application_sdk.credentials.resolver import CredentialResolver
from application_sdk.credentials.types import (
    ApiKeyCredential,
    BasicCredential,
    BearerTokenCredential,
    CertificateCredential,
    Credential,
    OAuthClientCredential,
    RawCredential,
)

__all__ = [
    # Core ref
    "CredentialRef",
    # Factory functions
    "api_key_ref",
    "basic_ref",
    "bearer_token_ref",
    "oauth_client_ref",
    "certificate_ref",
    "git_ssh_ref",
    "git_token_ref",
    "atlan_api_token_ref",
    "atlan_oauth_client_ref",
    "legacy_credential_ref",
    # Credential types
    "Credential",
    "BasicCredential",
    "ApiKeyCredential",
    "BearerTokenCredential",
    "OAuthClientCredential",
    "CertificateCredential",
    "RawCredential",
    # Git types
    "GitSshCredential",
    "GitTokenCredential",
    # Atlan types
    "AtlanApiToken",
    "AtlanOAuthClient",
    # Atlan client
    "create_async_atlan_client",
    "AtlanClientMixin",
    # Resolver
    "CredentialResolver",
    # Registry
    "CredentialTypeRegistry",
    "get_registry",
    "register_credential_type",
    # Errors
    "CredentialError",
    "CredentialNotFoundError",
    "CredentialParseError",
    "CredentialValidationError",
]
