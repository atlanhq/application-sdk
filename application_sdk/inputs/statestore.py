"""State store for the application."""

import json
import logging
from typing import Any, Dict

from dapr.clients import DaprClient
from temporalio import activity

from application_sdk.common.logger_adaptors import AtlanLoggerAdapter

activity.logger = AtlanLoggerAdapter(logging.getLogger(__name__))


class StateStoreInput:
    STATE_STORE_NAME = "statestore"

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
    def extract_configuration(cls, config_id: str) -> Dict[str, Any]:
        """
        Extract configuration from the state store using the config ID.

        :param config_id: The unique identifier for the configuration.
        :return: The configuration if found.
        :raises ValueError: If the config_id is invalid or configuration is not found.
        :raises Exception: If there's an error with the Dapr client operations.
        """
        if not config_id:
            raise ValueError("Invalid configuration ID provided.")
        config = cls._get_state(f"config_{config_id}")
        return config
