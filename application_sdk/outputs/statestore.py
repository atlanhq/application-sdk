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
        """Private method to save state to the store."""
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
        """
        Store credentials in the state store.

        :param config: The credentials to store.
        :return: The generated credential GUID.
        :raises Exception: If there's an error with the Dapr client operations.

        Note: this method will be moved to secretstore.py

        Usage:
            >>> StateStore.store_credentials({"username": "admin", "password": "password"})
            "credential_1234567890"
        """
        credential_guid = str(uuid.uuid4())
        cls._save_state(f"credential_{credential_guid}", config)
        return credential_guid

    @classmethod
    def store_configuration(cls, config_id: str, config: Dict[str, Any]) -> str:
        """
        Store configuration in the state store.

        :param config: The configuration to store.
        :param config_id: The unique identifier for the workflow.
        :return: The generated configuration ID.
        :raises Exception: If there's an error with the Dapr client operations.
        """
        cls._save_state(f"config_{config_id}", config)
        return config_id
