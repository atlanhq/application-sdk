"""State store for the application."""

import json
import logging
import uuid
from typing import Any, Dict

from dapr.clients import DaprClient
from temporalio import activity

from application_sdk.common.logger_adaptors import AtlanLoggerAdapter

activity.logger = AtlanLoggerAdapter(logging.getLogger(__name__))


class StateStore:
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
    def _get_state(cls, key: str) -> Dict[str, Any]:
        """Private method to get state from the store."""
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
        credential_guid = f"credential_{str(uuid.uuid4())}"
        cls._save_state(credential_guid, config)
        return credential_guid

    @classmethod
    def extract_credentials(cls, credential_guid: str) -> Dict[str, Any]:
        """
        Extract credentials from the state store using the credential GUID.

        :param credential_guid: The unique identifier for the credentials.
        :return: The credentials if found.
        :raises ValueError: If the credential_guid is invalid or credentials are not found.
        :raises Exception: If there's an error with the Dapr client operations.

        Note: this method will be moved to secretstore.py

        Usage:
            >>> StateStore.extract_credentials("1234567890")
            {"username": "admin", "password": "password"}
        """
        if not credential_guid:
            raise ValueError("Invalid credential GUID provided.")
        return cls._get_state(f"credential_{credential_guid}")

    @classmethod
    def store_configuration(cls, workflow_id: str, config: Dict[str, Any]) -> str:
        """
        Store configuration in the state store.

        :param config: The configuration to store.
        :param workflow_id: The unique identifier for the workflow.
        :return: The generated configuration GUID.
        :raises Exception: If there's an error with the Dapr client operations.
        """
        config_guid = f"config_{workflow_id}"
        cls._save_state(config_guid, config)
        return config_guid

    @classmethod
    def extract_configuration(cls, config_guid: str) -> Dict[str, Any]:
        """
        Extract configuration from the state store using the config GUID.

        :param config_guid: The unique identifier for the configuration.
        :return: The configuration if found.
        :raises ValueError: If the config_guid is invalid or configuration is not found.
        :raises Exception: If there's an error with the Dapr client operations.
        """
        if not config_guid:
            raise ValueError("Invalid configuration GUID provided.")
        config = cls._get_state(f"config_{config_guid}")
        return config
