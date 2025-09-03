"""Unified secret store service for the application."""

import collections.abc
import copy
import json
import uuid
from typing import Any, Dict

from dapr.clients import DaprClient

from application_sdk.common.dapr_utils import is_component_registered
from application_sdk.common.error_codes import CommonError
from application_sdk.constants import (
    DEPLOYMENT_NAME,
    DEPLOYMENT_SECRET_PATH,
    DEPLOYMENT_SECRET_STORE_NAME,
    LOCAL_ENVIRONMENT,
    SECRET_STORE_NAME,
)
from application_sdk.observability.logger_adaptor import get_logger
from application_sdk.services.statestore import StateStore, StateType

logger = get_logger(__name__)


class SecretStore:
    """Unified secret store service for handling secret management."""

    @classmethod
    async def get_credentials(cls, credential_guid: str) -> Dict[str, Any]:
        """Resolve credentials based on credential source with automatic secret substitution.

        This method retrieves credential configuration from the state store and resolves
        any secret references by fetching actual values from the secret store.

        Args:
            credential_guid (str): The unique GUID of the credential configuration to resolve.

        Returns:
            Dict[str, Any]: Complete credential data with secrets resolved.

        Raises:
            CommonError: If credential resolution fails due to missing configuration,
                        secret store errors, or invalid credential format.

        Examples:
            >>> # Resolve database credentials
            >>> creds = await SecretStore.get_credentials("db-cred-abc123")
            >>> print(f"Connecting to {creds['host']}:{creds['port']}")
            >>> # Password is automatically resolved from secret store

            >>> # Handle resolution errors
            >>> try:
            ...     creds = await SecretStore.get_credentials("invalid-guid")
            >>> except CommonError as e:
            ...     logger.error(f"Failed to resolve credentials: {e}")
        """

        async def _get_credentials_async(credential_guid: str) -> Dict[str, Any]:
            """Async helper function to perform async I/O operations."""
            credential_config = await StateStore.get_state(
                credential_guid, StateType.CREDENTIALS
            )

            # Fetch secret data from secret store
            secret_key = credential_config.get("secret-path", credential_guid)
            secret_data = SecretStore.get_secret(secret_key=secret_key)

            # Resolve credentials
            credential_source = credential_config.get("credentialSource", "direct")
            if credential_source == "direct":
                credential_config.update(secret_data)
                return credential_config
            else:
                return cls.resolve_credentials(credential_config, secret_data)

        try:
            # Run async operations directly
            return await _get_credentials_async(credential_guid)
        except Exception as e:
            logger.error(f"Error resolving credentials: {str(e)}")
            raise CommonError(
                CommonError.CREDENTIALS_RESOLUTION_ERROR,
                f"Failed to resolve credentials: {str(e)}",
            )

    @classmethod
    def resolve_credentials(
        cls, credential_config: Dict[str, Any], secret_data: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Resolve credentials by substituting secret references with actual values.

        This method processes credential configuration and replaces any reference
        values with corresponding secrets from the secret data.

        Args:
            credential_config (Dict[str, Any]): Base credential configuration with potential references.
            secret_data (Dict[str, Any]): Secret data containing actual secret values.

        Returns:
            Dict[str, Any]: Credential configuration with all secret references resolved.

        Examples:
            >>> # Basic secret resolution
            >>> config = {"host": "db.example.com", "password": "db_password_key"}
            >>> secrets = {"db_password_key": "actual_secret_password"}
            >>> resolved = SecretStore.resolve_credentials(config, secrets)
            >>> print(resolved)  # {"host": "db.example.com", "password": "actual_secret_password"}

            >>> # Resolution with nested 'extra' fields
            >>> config = {
            ...     "host": "db.example.com",
            ...     "extra": {"ssl_cert": "cert_key"}
            ... }
            >>> secrets = {"cert_key": "-----BEGIN CERTIFICATE-----..."}
            >>> resolved = SecretStore.resolve_credentials(config, secrets)
        """
        credentials = copy.deepcopy(credential_config)

        # Replace values with secret values
        for key, value in list(credentials.items()):
            if isinstance(value, str) and value in secret_data:
                credentials[key] = secret_data[value]

        # Apply the same substitution to the 'extra' dictionary if it exists
        if "extra" in credentials and isinstance(credentials["extra"], dict):
            for key, value in list(credentials["extra"].items()):
                if isinstance(value, str):
                    if value in secret_data:
                        credentials["extra"][key] = secret_data[value]
                    elif value in secret_data.get("extra", {}):
                        credentials["extra"][key] = secret_data["extra"][value]

        return credentials

    @classmethod
    def get_deployment_secret(cls) -> Dict[str, Any]:
        """Get deployment configuration from the deployment secret store.

        Validates that the deployment secret store component is registered
        before attempting to fetch secrets to prevent errors. This method
        is commonly used to retrieve environment-specific configuration.

        Returns:
            Dict[str, Any]: Deployment configuration data, or empty dict if
                          component is unavailable or fetch fails.

        Examples:
            >>> # Get deployment configuration
            >>> config = SecretStore.get_deployment_secret()
            >>> if config:
            ...     print(f"Environment: {config.get('environment')}")
            ...     print(f"Region: {config.get('region')}")
            >>> else:
            ...     print("No deployment configuration available")

            >>> # Use in application initialization
            >>> deployment_config = SecretStore.get_deployment_secret()
            >>> if deployment_config.get('debug_mode'):
            ...     logging.getLogger().setLevel(logging.DEBUG)
        """
        if not is_component_registered(DEPLOYMENT_SECRET_STORE_NAME):
            logger.warning(
                f"Deployment secret store component '{DEPLOYMENT_SECRET_STORE_NAME}' is not registered"
            )
            return {}

        try:
            return cls.get_secret(DEPLOYMENT_SECRET_PATH, DEPLOYMENT_SECRET_STORE_NAME)
        except Exception as e:
            logger.error(f"Failed to fetch deployment config: {e}")
            return {}

    @classmethod
    def get_secret(
        cls, secret_key: str, component_name: str = SECRET_STORE_NAME
    ) -> Dict[str, Any]:
        """Get secret from the Dapr secret store component.

        Retrieves secret data from the specified Dapr component and processes
        it into a standardized dictionary format.

        Args:
            secret_key (str): Key of the secret to fetch from the secret store.
            component_name (str, optional): Name of the Dapr component to fetch from.
                Defaults to SECRET_STORE_NAME.

        Returns:
            Dict[str, Any]: Processed secret data as a dictionary.

        Raises:
            Exception: If the secret cannot be retrieved from the component.

        Note:
            In local development environment, returns empty dict to avoid
            secret store dependency.

        Examples:
            >>> # Get database credentials
            >>> db_secret = SecretStore.get_secret("database-credentials")
            >>> print(f"Host: {db_secret.get('host')}")

            >>> # Get from specific component
            >>> api_secret = SecretStore.get_secret(
            ...     "api-keys",
            ...     component_name="external-secrets"
            ... )
        """
        if DEPLOYMENT_NAME == LOCAL_ENVIRONMENT:
            return {}

        try:
            with DaprClient() as client:
                dapr_secret_object = client.get_secret(
                    store_name=component_name, key=secret_key
                )
                return cls._process_secret_data(dapr_secret_object.secret)
        except Exception as e:
            logger.error(
                f"Failed to fetch secret using component {component_name}: {str(e)}"
            )
            raise

    @classmethod
    def _process_secret_data(cls, secret_data: Any) -> Dict[str, Any]:
        """Process raw secret data into a standardized dictionary format.

        Args:
            secret_data: Raw secret data from various sources.

        Returns:
            Dict[str, Any]: Processed secret data as a dictionary.
        """
        # Convert ScalarMapContainer to dict if needed
        if isinstance(secret_data, collections.abc.Mapping):
            secret_data = dict(secret_data)

        # If the dict has a single key and its value is a JSON string, parse it
        if len(secret_data) == 1 and isinstance(next(iter(secret_data.values())), str):
            try:
                parsed = json.loads(next(iter(secret_data.values())))
                if isinstance(parsed, dict):
                    secret_data = parsed
            except Exception as e:
                logger.error(f"Failed to parse secret data: {e}")
                pass

        return secret_data

    @classmethod
    def apply_secret_values(
        cls, source_data: Dict[str, Any], secret_data: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Apply secret values to source data by substituting references.

        This function replaces values in the source data with actual secret values
        when the source value exists as a key in the secret data. It supports
        nested structures and preserves the original data structure.

        Args:
            source_data (Dict[str, Any]): Original data with potential references to secrets.
            secret_data (Dict[str, Any]): Secret data containing actual secret values.

        Returns:
            Dict[str, Any]: Deep copy of source data with secret references resolved.

        Examples:
            >>> # Simple secret substitution
            >>> source = {
            ...     "database_url": "postgresql://user:${db_password}@localhost/app",
            ...     "api_key": "api_key_ref"
            ... }
            >>> secrets = {
            ...     "api_key_ref": "sk-1234567890abcdef",
            ...     "db_password": "secure_db_password"
            ... }
            >>> resolved = SecretStore.apply_secret_values(source, secrets)

            >>> # With nested extra fields
            >>> source = {
            ...     "host": "api.example.com",
            ...     "extra": {"token": "auth_token_ref"}
            ... }
            >>> secrets = {"auth_token_ref": "bearer_token_123"}
            >>> resolved = SecretStore.apply_secret_values(source, secrets)
        """
        result_data = copy.deepcopy(source_data)

        # Replace values with secret values
        for key, value in list(result_data.items()):
            if isinstance(value, str) and value in secret_data:
                result_data[key] = secret_data[value]

        # Apply the same substitution to the 'extra' dictionary if it exists
        if "extra" in result_data and isinstance(result_data["extra"], dict):
            for key, value in list(result_data["extra"].items()):
                if isinstance(value, str) and value in secret_data:
                    result_data["extra"][key] = secret_data[value]

        return result_data

    @classmethod
    async def save_secret(cls, config: Dict[str, Any]) -> str:
        """Store credentials in the state store (development environment only).

        This method is designed for development and testing purposes only.
        In production environments, secrets should be managed through proper
        secret management systems.

        Args:
            config (Dict[str, Any]): The credential configuration to store.

        Returns:
            str: The generated credential GUID that can be used to retrieve the credentials.

        Raises:
            ValueError: If called in production environment (non-local deployment).
            Exception: If there's an error storing the credentials.

        Examples:
            >>> # Development environment only
            >>> config = {
            ...     "host": "localhost",
            ...     "port": 5432,
            ...     "username": "dev_user",
            ...     "password": "dev_password",
            ...     "database": "app_dev"
            ... }
            >>> guid = await SecretStore.save_secret(config)
            >>> print(f"Stored credentials with GUID: {guid}")

            >>> # Later retrieve these credentials
            >>> retrieved = await SecretStore.get_credentials(guid)
        """
        if DEPLOYMENT_NAME == LOCAL_ENVIRONMENT:
            # NOTE: (development) temporary solution to store the credentials in the state store.
            # In production, dapr doesn't support creating secrets.
            credential_guid = str(uuid.uuid4())
            await StateStore.save_state_object(
                id=credential_guid, value=config, type=StateType.CREDENTIALS
            )
            return credential_guid
        else:
            raise ValueError("Storing credentials is not supported in production.")
