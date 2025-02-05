"""State store for the application."""

import json
import logging
import uuid
from typing import Any, Dict

from dapr.clients import DaprClient
from temporalio import activity

from application_sdk.common.logger_adaptors import AtlanLoggerAdapter

activity.logger = AtlanLoggerAdapter(logging.getLogger(__name__))
activity.logger.setLevel(logging.INFO)


class StateStore:
    STATE_STORE_NAME = "statestore"

    @classmethod
    def _save_state(cls, key: str, value: Dict[str, Any]) -> None:
        """Private method to save state to the store.

        Args:
            key: The key to store the state under.
            value: The dictionary value to store.

        Raises:
            Exception: If there's an error with the Dapr client operations.
        """
        client = DaprClient()
        try:
            client.save_state(
                store_name=cls.STATE_STORE_NAME,
                key=key,
                value=json.dumps(value),
            )
            activity.logger.info(f"State stored successfully with key: {key}")
        except Exception as e:
            activity.logger.error(f"Failed to store state: {str(e)}")
            raise e
        finally:
            client.close()

    @classmethod
    def _get_state(cls, key: str) -> Dict[str, Any]:
        """Private method to get state from the store.

        Args:
            key: The key to retrieve the state for.

        Returns:
            Dict[str, Any]: The retrieved state data.

        Raises:
            ValueError: If no state is found for the given key.
            Exception: If there's an error with the Dapr client operations.
        """
        client = DaprClient()
        try:
            state = client.get_state(store_name=cls.STATE_STORE_NAME, key=key)
            if not state.data:
                raise ValueError(f"State not found for key: {key}")
            return json.loads(state.data)
        except Exception as e:
            activity.logger.error(f"Failed to extract state: {str(e)}")
            raise e
        finally:
            client.close()

    @classmethod
    def store_credentials(cls, config: Dict[str, Any]) -> str:
        """Store credentials in the state store.

        Note: this method will be moved to secretstore.py

        Args:
            config: The credentials to store.

        Returns:
            str: The generated credential GUID.

        Raises:
            Exception: If there's an error with the Dapr client operations.

        Examples:
            >>> StateStore.store_credentials({"username": "admin", "password": "password"})
            "credential_1234567890"
        """
        credential_guid = str(uuid.uuid4())
        cls._save_state(f"credential_{credential_guid}", config)
        return credential_guid

    @classmethod
    def extract_credentials(cls, credential_guid: str) -> Dict[str, Any]:
        """Extract credentials from the state store using the credential GUID.

        Note: this method will be moved to secretstore.py

        Args:
            credential_guid: The unique identifier for the credentials.

        Returns:
            Dict[str, Any]: The credentials if found.

        Raises:
            ValueError: If the credential_guid is invalid or credentials are not found.
            Exception: If there's an error with the Dapr client operations.

        Examples:
            >>> StateStore.extract_credentials("1234567890")
            {"username": "admin", "password": "password"}
        """
        if not credential_guid:
            raise ValueError("Invalid credential GUID provided.")
        return cls._get_state(f"credential_{credential_guid}")

    @classmethod
    def store_configuration(cls, config_id: str, config: Dict[str, Any]) -> str:
        """Store configuration in the state store.

        Args:
            config_id: The unique identifier for the workflow.
            config: The configuration to store.

        Returns:
            str: The generated configuration ID.

        Raises:
            Exception: If there's an error with the Dapr client operations.
        """
        cls._save_state(f"config_{config_id}", config)
        return config_id

    @classmethod
    def extract_configuration(cls, config_id: str) -> Dict[str, Any]:
        """Extract configuration from the state store using the config ID.

        Args:
            config_id: The unique identifier for the configuration.

        Returns:
            Dict[str, Any]: The configuration if found.

        Raises:
            ValueError: If the config_id is invalid or configuration is not found.
            Exception: If there's an error with the Dapr client operations.
        """
        if not config_id:
            raise ValueError("Invalid configuration ID provided.")
        config = cls._get_state(f"config_{config_id}")
        return config
