"""State store for the application."""

import json
from typing import Any, Dict

from dapr.clients import DaprClient
from temporalio import activity

from application_sdk.common.error_codes import IOError
from application_sdk.constants import STATE_STORE_NAME
from application_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)
activity.logger = logger


class StateStoreInput:
    @classmethod
    def get_state(cls, key: str) -> Dict[str, Any]:
        """Get state from the store.

        Args:
            key: The key to retrieve the state for.

        Returns:
            Dict[str, Any]: The retrieved state data.

        Raises:
            ValueError: If no state is found for the given key.
            IOError: If there's an error with the Dapr client operations.
        """
        try:
            with DaprClient() as client:
                state = client.get_state(store_name=STATE_STORE_NAME, key=key)
                if not state.data:
                    raise IOError(
                        f"{IOError.STATE_STORE_ERROR}: State not found for key: {key}"
                    )
                return json.loads(state.data)
        except IOError as e:
            logger.error(
                f"{IOError.STATE_STORE_ERROR}: Failed to extract state: {str(e)}",
                error_code=IOError.STATE_STORE_ERROR.code,
            )
            raise  # Re-raise the exception after logging

    @classmethod
    def extract_configuration(cls, config_id: str) -> Dict[str, Any]:
        """Extract configuration from the state store using the config ID.

        Args:
            config_id: The unique identifier for the configuration.

        Returns:
            Dict[str, Any]: The configuration if found.

        Raises:
            ValueError: If the config_id is invalid or configuration is not found.
            IOError: If there's an error with the Dapr client operations.
        """
        if not config_id:
            raise IOError(
                f"{IOError.STATE_STORE_ERROR}: Invalid configuration ID provided."
            )
        config = cls.get_state(f"config_{config_id}")
        return config
