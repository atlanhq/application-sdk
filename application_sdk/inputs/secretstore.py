"""Secret store for the application."""

import collections.abc
import copy
import json
from typing import Any, Dict

from dapr.clients import DaprClient

from application_sdk.constants import LOCAL_DEVELOPMENT, SECRET_STORE_NAME
from application_sdk.inputs.statestore import StateStoreInput, StateType
from application_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)


class SecretStoreInput:
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
    async def fetch_secret(
        cls, secret_key: str, component_name: str = SECRET_STORE_NAME
    ) -> Dict[str, Any]:
        """Fetch secret using the Dapr component.

        Args:
            component_name: Name of the Dapr component to fetch from
            secret_key: Key of the secret to fetch

        Returns:
            Dict with processed secret data

        Raises:
            Exception: If secret fetching fails
        """
        try:
            secret = {}
            if not LOCAL_DEVELOPMENT:
                secret = cls.get_secret(secret_key, component_name)

            credential_config = StateStoreInput.get_state(
                secret_key, StateType.CREDENTIALS
            )
            secret.update(credential_config)
            return secret
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
            except Exception:
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
