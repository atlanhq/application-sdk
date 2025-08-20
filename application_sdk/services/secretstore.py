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
        """
        Resolve credentials based on credential source.

        Args:
            credential_guid: The GUID of the credential to resolve

        Returns:
            Dict with resolved credentials

        Raises:
            CommonError: If credential resolution fails
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
        """
        Resolve credentials from secret data.

        Args:
            credential_config: The credential configuration.
            secret_data: The secret data.

        Returns:
            The resolved credentials.
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
        """Get deployment config from the deployment secret store.

        Validates that the deployment secret store component is registered
        before attempting to fetch secrets to prevent errors.

        Returns:
            Dict[str, Any]: Deployment configuration data, or empty dict if
                          component is unavailable or fetch fails.
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
        """Get secret from the Dapr component.

        Args:
            secret_key: Key of the secret to fetch
            component_name: Name of the Dapr component to fetch from

        Returns:
            Dict with processed secret data
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

        This function replaces values in the source data with values
        from the secret data when the source value exists as a key in the secrets.

        Args:
            source_data: Original data with potential references to secrets
            secret_data: Secret data containing actual values

        Returns:
            Dict[str, Any]: Data with secret values applied
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
        """Store credentials in the state store.

        Args:
            config: The credentials to store.

        Returns:
            str: The generated credential GUID.

        Raises:
            Exception: If there's an error with the Dapr client operations.

        Examples:
            >>> SecretStore.save_secret({"username": "admin", "password": "password"})
            "1234567890"
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
