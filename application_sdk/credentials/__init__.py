"""Typed credential system for the Application SDK.

Public API::

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

        # agent_json ingress normalisation
        normalize_agent_json, lift_agent_json, declared_agent_spec_type,
        AGENT_JSON_ALIASES,

        # Input routing — (ref, inline) from a workflow input
        route_credentials, ResolvedCredentials, find_prebuilt_credential_ref,
        normalize_inline_credentials, flatten_dotted_keys,
        CredentialValue, CredentialMap, InlineCredentials,
    )
"""

import importlib
from typing import TYPE_CHECKING, Any

from application_sdk.credentials.errors import (
    CredentialError,
    CredentialNotFoundError,
    CredentialParseError,
    CredentialValidationError,
)
from application_sdk.credentials.ingress import (
    AGENT_JSON_ALIASES,
    declared_agent_spec_type,
    lift_agent_json,
    normalize_agent_json,
)
from application_sdk.credentials.spec import AgentCredentialSpec

if TYPE_CHECKING:
    from application_sdk.common.transforms import (
        camel_to_kebab,
        expand_dotted_keys,
        kebab_to_camel,
        transform_agent_credentials,
    )
    from application_sdk.credentials.atlan import AtlanApiToken, AtlanOAuthClient
    from application_sdk.credentials.atlan_client import (
        AtlanClientMixin,
        create_async_atlan_client,
    )
    from application_sdk.credentials.git import GitSshCredential, GitTokenCredential
    from application_sdk.credentials.oauth import OAuthTokenError, OAuthTokenService
    from application_sdk.credentials.ref import (
        CredentialRef,
        CredentialResolvable,
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
    from application_sdk.credentials.utils import parse_credentials_extra

#: Worker-side names: imported on first access, so the api distribution
#: (which ships this ``__init__`` without them) imports cleanly.
_LAZY: dict[str, tuple[str, str]] = {
    "ApiKeyCredential": ("application_sdk.credentials.types", "ApiKeyCredential"),
    "AtlanApiToken": ("application_sdk.credentials.atlan", "AtlanApiToken"),
    "AtlanClientMixin": (
        "application_sdk.credentials.atlan_client",
        "AtlanClientMixin",
    ),
    "AtlanOAuthClient": ("application_sdk.credentials.atlan", "AtlanOAuthClient"),
    "BasicCredential": ("application_sdk.credentials.types", "BasicCredential"),
    "BearerTokenCredential": (
        "application_sdk.credentials.types",
        "BearerTokenCredential",
    ),
    "CertificateCredential": (
        "application_sdk.credentials.types",
        "CertificateCredential",
    ),
    "Credential": ("application_sdk.credentials.types", "Credential"),
    "CredentialRef": ("application_sdk.credentials.ref", "CredentialRef"),
    "CredentialResolvable": ("application_sdk.credentials.ref", "CredentialResolvable"),
    "CredentialResolver": (
        "application_sdk.credentials.resolver",
        "CredentialResolver",
    ),
    "CredentialTypeRegistry": (
        "application_sdk.credentials.registry",
        "CredentialTypeRegistry",
    ),
    "GitSshCredential": ("application_sdk.credentials.git", "GitSshCredential"),
    "GitTokenCredential": ("application_sdk.credentials.git", "GitTokenCredential"),
    "OAuthClientCredential": (
        "application_sdk.credentials.types",
        "OAuthClientCredential",
    ),
    "OAuthTokenError": ("application_sdk.credentials.oauth", "OAuthTokenError"),
    "OAuthTokenService": ("application_sdk.credentials.oauth", "OAuthTokenService"),
    "RawCredential": ("application_sdk.credentials.types", "RawCredential"),
    "api_key_ref": ("application_sdk.credentials.ref", "api_key_ref"),
    "atlan_api_token_ref": ("application_sdk.credentials.ref", "atlan_api_token_ref"),
    "atlan_oauth_client_ref": (
        "application_sdk.credentials.ref",
        "atlan_oauth_client_ref",
    ),
    "basic_ref": ("application_sdk.credentials.ref", "basic_ref"),
    "bearer_token_ref": ("application_sdk.credentials.ref", "bearer_token_ref"),
    "camel_to_kebab": ("application_sdk.common.transforms", "camel_to_kebab"),
    "certificate_ref": ("application_sdk.credentials.ref", "certificate_ref"),
    "create_async_atlan_client": (
        "application_sdk.credentials.atlan_client",
        "create_async_atlan_client",
    ),
    "expand_dotted_keys": ("application_sdk.common.transforms", "expand_dotted_keys"),
    "get_registry": ("application_sdk.credentials.registry", "get_registry"),
    "git_ssh_ref": ("application_sdk.credentials.ref", "git_ssh_ref"),
    "git_token_ref": ("application_sdk.credentials.ref", "git_token_ref"),
    "kebab_to_camel": ("application_sdk.common.transforms", "kebab_to_camel"),
    "legacy_credential_ref": (
        "application_sdk.credentials.ref",
        "legacy_credential_ref",
    ),
    "oauth_client_ref": ("application_sdk.credentials.ref", "oauth_client_ref"),
    "parse_credentials_extra": (
        "application_sdk.credentials.utils",
        "parse_credentials_extra",
    ),
    "register_credential_type": (
        "application_sdk.credentials.registry",
        "register_credential_type",
    ),
    "transform_agent_credentials": (
        "application_sdk.common.transforms",
        "transform_agent_credentials",
    ),
}

__all__ = [
    # Core ref + spec + protocol
    "CredentialRef",
    "CredentialResolvable",
    "AgentCredentialSpec",
    # agent_json ingress normalisation
    "normalize_agent_json",
    "lift_agent_json",
    "declared_agent_spec_type",
    "AGENT_JSON_ALIASES",
    # Input routing: (ref, inline) from a workflow input
    "route_credentials",
    "ResolvedCredentials",
    "find_prebuilt_credential_ref",
    "normalize_inline_credentials",
    "flatten_dotted_keys",
    "CredentialValue",
    "CredentialMap",
    "InlineCredentials",
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
    # OAuth token service
    "OAuthTokenService",
    "OAuthTokenError",
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
    # Transforms (deprecated — will be removed once apps are fully native)
    "kebab_to_camel",
    "camel_to_kebab",
    "expand_dotted_keys",
    "transform_agent_credentials",
    # Utilities
    "parse_credentials_extra",
]

# ``routing`` builds bounded contract types from ``contracts.types``, which itself
# imports ``credentials.ref`` — loading it eagerly here would close that cycle
# whenever ``contracts`` is imported first. Its names resolve on first access too.
_LAZY.update(
    {
        "route_credentials": (
            "application_sdk.credentials.routing",
            "route_credentials",
        ),
        "ResolvedCredentials": (
            "application_sdk.credentials.routing",
            "ResolvedCredentials",
        ),
        "find_prebuilt_credential_ref": (
            "application_sdk.credentials.routing",
            "find_prebuilt_credential_ref",
        ),
        "normalize_inline_credentials": (
            "application_sdk.credentials.routing",
            "normalize_inline_credentials",
        ),
        "flatten_dotted_keys": (
            "application_sdk.credentials.routing",
            "flatten_dotted_keys",
        ),
        "CredentialValue": ("application_sdk.credentials.routing", "CredentialValue"),
        "CredentialMap": ("application_sdk.credentials.routing", "CredentialMap"),
        "InlineCredentials": (
            "application_sdk.credentials.routing",
            "InlineCredentials",
        ),
    }
)

if TYPE_CHECKING:
    from application_sdk.credentials.routing import (
        CredentialMap,
        CredentialValue,
        InlineCredentials,
        ResolvedCredentials,
        find_prebuilt_credential_ref,
        flatten_dotted_keys,
        normalize_inline_credentials,
        route_credentials,
    )


def __getattr__(name: str) -> Any:
    target = _LAZY.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(importlib.import_module(target[0]), target[1])
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(_LAZY))
