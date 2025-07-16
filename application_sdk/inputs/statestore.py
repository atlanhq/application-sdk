"""State store for the application."""

import json
import os
from enum import Enum
from typing import Any, Dict

from temporalio import activity

from application_sdk.constants import APPLICATION_NAME, TEMPORARY_PATH
from application_sdk.inputs.objectstore import ObjectStoreInput
from application_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)
activity.logger = logger


class StateType(Enum):
    WORKFLOWS = "workflows"
    CREDENTIALS = "credentials"

    @classmethod
    def is_member(cls, type: str) -> bool:
        return type in cls._value2member_map_


class StateStoreInput:
    @classmethod
    def get_state(cls, id: str, type: StateType) -> Dict[str, Any]:
        """Get state from the store.

        Args:
            id: The key to retrieve the state for.
            type: The type of state to retrieve.

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

        state_file_path = f"apps/{APPLICATION_NAME}/{type.value}/{id}/config.json"
        state = {}

        try:
            local_state_file_path = os.path.join(TEMPORARY_PATH, state_file_path)
            ObjectStoreInput.download_file_from_object_store(
                download_file_prefix=TEMPORARY_PATH,
                file_path=local_state_file_path,
            )

            with open(local_state_file_path, "r") as file:
                state = json.load(file)

        except Exception as e:
            # FIXME: this needs to be better handled
            # local error message is "file not found", while in object store it is "object not found"
            if "not found" in str(e).lower():
                pass
            else:
                logger.error(f"Failed to extract state: {str(e)}")
                raise

        return state
