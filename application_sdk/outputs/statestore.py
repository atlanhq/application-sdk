"""State store for the application."""

import json
from typing import Any, Dict

from dapr.clients import DaprClient
from temporalio import activity

from application_sdk.constants import STATE_STORE_NAME
from application_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)
activity.logger = logger


class StateStoreOutput:
    @classmethod
    def save_state(cls, key: str, value: Dict[str, Any]) -> None:
        """Save state to the store.

        Args:
            key: The key to store the state under.
            value: The dictionary value to store.

        Raises:
            Exception: If there's an error with the Dapr client operations.
        """
        try:
            with DaprClient() as client:
                client.save_state(
                    store_name=STATE_STORE_NAME,
                    key=key,
                    value=json.dumps(value),
                )
                logger.info(f"State stored successfully with key: {key}")
        except Exception as e:
            logger.error(f"Failed to store state: {str(e)}")
            raise e

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
        cls.save_state(f"config_{config_id}", config)
        return config_id
