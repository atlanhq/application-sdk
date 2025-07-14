"""State store for the application."""

import json
import os
from typing import Any, Dict

from temporalio import activity

from application_sdk.constants import APPLICATION_NAME, TEMPORARY_PATH
from application_sdk.inputs.statestore import StateStoreInput
from application_sdk.observability.logger_adaptor import get_logger
from application_sdk.outputs.objectstore import ObjectStoreOutput

logger = get_logger(__name__)
activity.logger = logger


class StateStoreOutput:
    @classmethod
    async def save_state(cls, key: str, value: Any, workflow_id: str) -> None:
        """Save state to the store.

        Args:
            key: The key to store the state under.
            value: The dictionary value to store.

        Raises:
            Exception: If there's an error with the Dapr client operations.

        Example:
            ```python
            from application_sdk.outputs.statestore import StateStoreOutput

            await StateStoreOutput.save_state("test", {"test": "test"}, "wf-123")

            # {'test': 'test'}
            ```
        """
        # get the current state from object store
        current_state = StateStoreInput.get_state(workflow_id)
        state_path = f"apps/{APPLICATION_NAME}/data/{workflow_id}/state.json"

        try:
            # update the state with the new value
            current_state[key] = value

            local_state_file_path = os.path.join(TEMPORARY_PATH, state_path)
            os.makedirs(os.path.dirname(local_state_file_path), exist_ok=True)

            # save the state to a local file
            with open(local_state_file_path, "w") as file:
                json.dump(current_state, file)

            # save the state to the object store
            await ObjectStoreOutput.push_file_to_object_store(
                output_prefix=TEMPORARY_PATH, file_path=local_state_file_path
            )

        except Exception as e:
            logger.error(f"Failed to store state: {str(e)}")
            raise e

    @classmethod
    async def store_configuration(cls, config_id: str, config: Dict[str, Any]) -> str:
        """Store configuration in the state store.

        Args:
            config_id: The unique identifier for the workflow.
            config: The configuration to store.

        Returns:
            str: The generated configuration ID.

        Raises:
            Exception: If there's an error with the Dapr client operations.
        """
        await cls.save_state(f"config_{config_id}", config, config_id)
        return config_id
