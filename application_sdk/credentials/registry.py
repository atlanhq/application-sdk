"""CredentialTypeRegistry — maps credential_type strings to classes and parsers."""

from __future__ import annotations

from typing import Any, Callable

from application_sdk.credentials.types import Credential

# Type alias for a parser function
CredentialParser = Callable[[dict[str, Any]], Credential]


class CredentialTypeRegistry:
    """Singleton registry mapping credential_type strings to (class, parser) pairs.

    Built-in types are auto-registered on first access. Custom credential types
    can be added with ``register_credential_type()``.
    """

    _instance: "CredentialTypeRegistry | None" = None
    _initialized: bool = False

    def __new__(cls) -> "CredentialTypeRegistry":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._registry: dict[str, tuple[type, CredentialParser]] = {}
            cls._instance._initialized = False
        return cls._instance

    def _ensure_initialized(self) -> None:
        if self._initialized:
            return
        self._initialized = True
        self._register_builtins()

    def _register_builtins(self) -> None:
        from application_sdk.credentials.atlan import (
            AtlanApiToken,
            AtlanOAuthClient,
            _parse_atlan_api_token,
            _parse_atlan_oauth_client,
        )
        from application_sdk.credentials.git import (
            GitSshCredential,
            GitTokenCredential,
            _parse_git_ssh,
            _parse_git_token,
        )
        from application_sdk.credentials.types import (
            ApiKeyCredential,
            BasicCredential,
            BearerTokenCredential,
            CertificateCredential,
            OAuthClientCredential,
            ServicePrincipalCredential,
            _parse_api_key,
            _parse_basic,
            _parse_bearer_token,
            _parse_certificate,
            _parse_oauth_client,
            _parse_service_principal,
        )

        builtins: list[tuple[str, type, CredentialParser]] = [
            ("basic", BasicCredential, _parse_basic),
            ("api_key", ApiKeyCredential, _parse_api_key),
            ("bearer_token", BearerTokenCredential, _parse_bearer_token),
            ("oauth_client", OAuthClientCredential, _parse_oauth_client),
            ("certificate", CertificateCredential, _parse_certificate),
            ("git_ssh", GitSshCredential, _parse_git_ssh),
            ("git_token", GitTokenCredential, _parse_git_token),
            ("atlan_api_token", AtlanApiToken, _parse_atlan_api_token),
            ("atlan_oauth_client", AtlanOAuthClient, _parse_atlan_oauth_client),
            ("service_principal", ServicePrincipalCredential, _parse_service_principal),
        ]
        for name, cls, parser in builtins:
            self._registry[name] = (cls, parser)

    def register_credential_type(
        self,
        name: str,
        cls: type,
        parser: CredentialParser,
    ) -> None:
        """Register a custom credential type.

        Args:
            name: The ``credential_type`` string (e.g. ``"my_custom_cred"``).
            cls: The credential dataclass.
            parser: A callable ``(dict) -> Credential`` that constructs the class.
        """
        self._ensure_initialized()
        self._registry[name] = (cls, parser)

    def parse(self, type_name: str, data: dict[str, Any]) -> Credential:
        """Parse a raw dict into a typed Credential.

        Args:
            type_name: The ``credential_type`` string.
            data: Raw credential data from the secret store.

        Returns:
            A typed Credential instance.

        Raises:
            CredentialParseError: If ``type_name`` is not registered or parsing fails.
        """
        self._ensure_initialized()
        from application_sdk.credentials.errors import CredentialParseError

        entry = self._registry.get(type_name)
        if entry is None:
            raise CredentialParseError(
                f"No parser registered for credential type '{type_name}'",
            )
        _, parser = entry
        try:
            return parser(data)
        except Exception as exc:
            raise CredentialParseError(
                f"Failed to parse credential of type '{type_name}': {exc}",
                cause=exc,
            ) from exc

    def get_class(self, type_name: str) -> type | None:
        """Return the class registered for ``type_name``, or None."""
        self._ensure_initialized()
        entry = self._registry.get(type_name)
        return entry[0] if entry else None

    def registered_types(self) -> list[str]:
        """Return a sorted list of all registered type names."""
        self._ensure_initialized()
        return sorted(self._registry.keys())


# Module-level singleton accessor
def get_registry() -> CredentialTypeRegistry:
    """Return the global CredentialTypeRegistry singleton."""
    return CredentialTypeRegistry()


def register_credential_type(
    name: str,
    cls: type,
    parser: CredentialParser,
) -> None:
    """Register a custom credential type in the global registry.

    Convenience wrapper around ``get_registry().register_credential_type()``.
    """
    get_registry().register_credential_type(name, cls, parser)
