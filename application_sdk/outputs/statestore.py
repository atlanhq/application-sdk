"""State store for the application."""

import json
import os
from typing import Any, Dict

from temporalio import activity

from application_sdk.constants import TEMPORARY_PATH, UPSTREAM_OBJECT_STORE_NAME
from application_sdk.inputs.statestore import (
    StateStoreInput,
    StateType,
    build_state_store_path,
)
from application_sdk.observability.logger_adaptor import get_logger
from application_sdk.outputs.objectstore import ObjectStoreOutput

logger = get_logger(__name__)
activity.logger = logger


class StateStoreOutput:
    @classmethod
    async def save_state(cls, key: str, value: Any, id: str, type: StateType) -> None:
        """Save state to the store.

        Args:
            key: The key to store the state under.
            value: The dictionary value to store.

        Raises:
            Exception: If there's an error with the Dapr client operations.

        Example:
            >>> from application_sdk.outputs.statestore import StateStoreOutput

            >>> await StateStoreOutput.save_state("test", {"test": "test"}, "wf-123")
        """
        try:
            # get the current state from object store
            current_state = StateStoreInput.get_state(id, type)
            state_file_path = build_state_store_path(id, type)

            # update the state with the new value
            current_state[key] = value

            local_state_file_path = os.path.join(TEMPORARY_PATH, state_file_path)
            os.makedirs(os.path.dirname(local_state_file_path), exist_ok=True)

            # save the state to a local file
            with open(local_state_file_path, "w") as file:
                json.dump(current_state, file)

            # save the state to the object store
            await ObjectStoreOutput.push_file_to_object_store(
                output_prefix=TEMPORARY_PATH,
                file_path=local_state_file_path,
                object_store_name=UPSTREAM_OBJECT_STORE_NAME,
            )

        except Exception as e:
            logger.error(f"Failed to store state: {str(e)}")
            raise e

    @classmethod
    async def save_state_object(
        cls, id: str, value: Dict[str, Any], type: StateType
    ) -> Dict[str, Any]:
        """Save the entire state object to the object store.

        Args:
            id: The id of the state.
            value: The value of the state.
            type: The type of the state.

        Returns:
            Dict[str, Any]: The updated state.

        Raises:
            ValueError: If the type is invalid.
            Exception: If there's an error with the Dapr client operations.

        Example:
            >>> from application_sdk.outputs.statestore import StateStoreOutput
            >>> await StateStoreOutput.save_state_object("wf-123", {"test": "test"}, "workflow")
        """
        try:
            logger.info(f"Saving state object for {id} with type {type}")
            # get the current state from object store
            current_state = StateStoreInput.get_state(id, type)
            state_file_path = build_state_store_path(id, type)

            # update the state with the new value
            current_state.update(value)

            local_state_file_path = os.path.join(TEMPORARY_PATH, state_file_path)
            os.makedirs(os.path.dirname(local_state_file_path), exist_ok=True)

            # save the state to a local file
            with open(local_state_file_path, "w") as file:
                json.dump(current_state, file)

            # save the state to the object store
            await ObjectStoreOutput.push_file_to_object_store(
                output_prefix=TEMPORARY_PATH,
                file_path=local_state_file_path,
                object_store_name=UPSTREAM_OBJECT_STORE_NAME,
            )
            logger.info(f"State object saved for {id} with type {type}")
            return current_state
        except Exception as e:
            logger.error(f"Failed to store state: {str(e)}")
            raise e
