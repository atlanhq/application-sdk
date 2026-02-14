from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any, Dict, List

if TYPE_CHECKING:
    from application_sdk.credentials import Credential


class HandlerInterface(ABC):
    """
    Abstract base class for workflow handlers.

    Handlers define the credential requirements for their workflows by
    implementing the `declare_credentials()` class method. This allows
    the system to:
    - Generate UI forms for credential input
    - Validate credentials at runtime
    - Automatically inject credentials into workflows

    Example:
        >>> from application_sdk.credentials import Credential, AuthMode
        >>>
        >>> class MyHandler(HandlerInterface):
        ...     @classmethod
        ...     def declare_credentials(cls):
        ...         return [
        ...             Credential(name="api", auth=AuthMode.API_KEY),
        ...             Credential(name="database", auth=AuthMode.DATABASE),
        ...         ]
    """

    @classmethod
    def declare_credentials(cls) -> List["Credential"]:
        """Declare credential requirements for this handler.

        Override this method to specify what credentials your handler needs.
        The returned list of Credential objects defines:
        - Slot names for credential binding
        - Authentication modes (API_KEY, OAUTH2, DATABASE, etc.)
        - Field customizations and validation rules

        Returns:
            List of Credential declarations. Empty list if no credentials needed.

        Example:
            >>> from application_sdk.credentials import Credential, AuthMode, FieldSpec
            >>>
            >>> @classmethod
            ... def declare_credentials(cls):
            ...     return [
            ...         # Simple API key
            ...         Credential(name="api", auth=AuthMode.API_KEY),
            ...
            ...         # Database with custom fields
            ...         Credential(
            ...             name="database",
            ...             auth=AuthMode.DATABASE,
            ...             fields={
            ...                 "host": FieldSpec(
            ...                     name="host",
            ...                     display_name="Database Host",
            ...                     placeholder="db.example.com",
            ...                 ),
            ...             },
            ...         ),
            ...
            ...         # OAuth2 with client credentials
            ...         Credential(
            ...             name="oauth_api",
            ...             auth=AuthMode.OAUTH2_CLIENT_CREDENTIALS,
            ...             config_override={"token_url": "https://auth.api.com/token"},
            ...         ),
            ...     ]
        """
        return []

    @classmethod
    def get_credentials_configmap(cls) -> Dict[str, Any]:
        """Get the credential configmap schema for this handler.

        This method generates a schema that describes all credential
        requirements declared by this handler. The schema is used by:
        - Frontend UI to render credential input forms
        - Credential Profile binding to map slots to credentials
        - Validation to ensure all required credentials are provided

        Returns:
            Dictionary with credential configmap schema containing:
            - credentials: List of credential schemas with fields
            - handler_name: Name of the handler class

        Example:
            >>> schema = MyHandler.get_credentials_configmap()
            >>> print(schema)
            {
                "handler_name": "MyHandler",
                "credentials": [
                    {
                        "name": "api",
                        "auth_mode": "api_key",
                        "protocol_type": "static_secret",
                        "required": True,
                        "fields": [
                            {"name": "api_key", "label": "API Key", ...}
                        ]
                    }
                ]
            }
        """
        from application_sdk.credentials.resolver import CredentialResolver

        declarations = cls.declare_credentials()
        credential_schemas = [
            CredentialResolver.get_credential_schema(cred) for cred in declarations
        ]

        return {
            "handler_name": cls.__name__,
            "credentials": credential_schemas,
        }

    @abstractmethod
    async def load(self, *args: Any, **kwargs: Any) -> None:
        """
        Method to load the handler
        """
        pass

    @abstractmethod
    async def test_auth(self, *args: Any, **kwargs: Any) -> bool:
        """
        Abstract method to test the authentication credentials
        To be implemented by the subclass
        """
        raise NotImplementedError("test_auth method not implemented")

    @abstractmethod
    async def preflight_check(self, *args: Any, **kwargs: Any) -> Any:
        """
        Abstract method to perform preflight checks
        To be implemented by the subclass
        """
        raise NotImplementedError("preflight_check method not implemented")

    @abstractmethod
    async def fetch_metadata(self, *args: Any, **kwargs: Any) -> Any:
        """
        Abstract method to fetch metadata
        To be implemented by the subclass
        """
        raise NotImplementedError("fetch_metadata method not implemented")

    @staticmethod
    async def get_configmap(config_map_id: str) -> Dict[str, Any]:
        """
        Static method to get the configmap
        """
        return {}
