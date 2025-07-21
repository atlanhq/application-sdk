"""State store for the application."""

import json
import os
from enum import Enum
from typing import Any, Dict

from temporalio import activity

from application_sdk.constants import (
    APPLICATION_NAME,
    STATE_STORE_PATH_TEMPLATE,
    TEMPORARY_PATH,
)
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


def build_state_store_path(id: str, state_type: StateType) -> str:
    """Build the state file path for the given id and type.

    Args:
        id: The unique identifier for the state.
        state_type: The type of state (workflows, credentials, etc.).

    Returns:
        str: The constructed state file path.

    Example:
        >>> build_state_store_path("wf-123", "workflows")
        'persistent-artifacts/apps/my-app/workflows/wf-123/config.json'
    """
    return STATE_STORE_PATH_TEMPLATE.format(
        application_name=APPLICATION_NAME, state_type=state_type.value, id=id
    )


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
            >>> from application_sdk.inputs.statestore import StateStoreInput

            >>> state = StateStoreInput.get_state("wf-123")
            >>> print(state)
            {'test': 'test'}
        """

        state_file_path = build_state_store_path(id, type)
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
            # local error message is "file not found", while in object store it is "object not found"
            if "not found" in str(e).lower():
                logger.debug(
                    f"No state found for {type.value} with id '{id}', returning empty dict"
                )
                pass
            else:
                logger.error(f"Failed to extract state: {str(e)}")
                raise

        return state
