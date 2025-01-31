"""State store for the application."""

import json
import logging
import uuid
from typing import Any, Dict

from dapr.clients import DaprClient
from temporalio import activity

from application_sdk.common.logger_adaptors import AtlanLoggerAdapter

activity.logger = AtlanLoggerAdapter(logging.getLogger(__name__))


class StateStoreOutput:
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
            >>> StateStoreOutput.store_credentials({"username": "admin", "password": "password"})
            "credential_1234567890"
        """
        credential_guid = str(uuid.uuid4())
        cls._save_state(f"credential_{credential_guid}", config)
        return credential_guid

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
