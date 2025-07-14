"""State store for the application."""

import json
import os
from typing import Any, Dict

from temporalio import activity

from application_sdk.common.error_codes import IOError
from application_sdk.constants import APPLICATION_NAME, TEMPORARY_PATH
from application_sdk.inputs.objectstore import ObjectStoreInput
from application_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)
activity.logger = logger


class StateStoreInput:
    @classmethod
    def get_state(cls, workflow_id: str) -> Dict[str, Any]:
        """Get state from the store.

        Args:
            workflow_id: The key to retrieve the state for.

        Returns:
            Dict[str, Any]: The retrieved state data.

        Raises:
            ValueError: If no state is found for the given key.
            IOError: If there's an error with the Dapr client operations.

        Example:
            ```python
            from application_sdk.inputs.statestore import StateStoreInput

            state = StateStoreInput.get_state("wf-123")
            print(state)
            # {'test': 'test'}
            ```
        """
        state = {}
        state_file_path = f"apps/{APPLICATION_NAME}/data/{workflow_id}/state.json"

        try:
            local_state_file_path = os.path.join(TEMPORARY_PATH, state_file_path)
            ObjectStoreInput.download_file_from_object_store(
                download_file_prefix=TEMPORARY_PATH,
                file_path=local_state_file_path,
            )

            with open(local_state_file_path, "r") as file:
                state = json.load(file)

        except Exception as e:
            if "file not found" in str(e).lower():
                pass
            else:
                logger.error(f"Failed to extract state: {str(e)}")
                raise

        return state

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
