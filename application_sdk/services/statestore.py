"""Unified state store service for the application."""

import json
import os
from enum import Enum
from typing import Any, Dict

from temporalio import activity

from application_sdk.activities.common.utils import get_object_store_prefix
from application_sdk.constants import (
    APPLICATION_NAME,
    STATE_STORE_PATH_TEMPLATE,
    TEMPORARY_PATH,
    UPSTREAM_OBJECT_STORE_NAME,
)
from application_sdk.observability.logger_adaptor import get_logger
from application_sdk.services.objectstore import ObjectStore

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
        './local/tmp/persistent-artifacts/apps/appName/workflows/wf-123/config.json'
    """
    return os.path.join(
        TEMPORARY_PATH,
        STATE_STORE_PATH_TEMPLATE.format(
            application_name=APPLICATION_NAME, state_type=state_type.value, id=id
        ),
    )


class StateStore:
    """Unified state store service for handling state management."""

    @classmethod
    async def get_state(cls, id: str, type: StateType) -> Dict[str, Any]:
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
            >>> from application_sdk.services.statestore import StateStore

            >>> state = StateStore.get_state("wf-123")
            >>> print(state)
            {'test': 'test'}
        """

        state_file_path = build_state_store_path(id, type)
        state = {}

        try:
            logger.info(f"Trying to download state object for {id} with type {type}")
            await ObjectStore.download_file(
                source=get_object_store_prefix(state_file_path),
                destination=state_file_path,
                store_name=UPSTREAM_OBJECT_STORE_NAME,
            )

            with open(state_file_path, "r") as file:
                state = json.load(file)

            logger.info(f"State object downloaded for {id} with type {type}")
        except Exception as e:
            # local error message is "file not found", while in object store it is "object not found"
            if "not found" in str(e).lower():
                logger.info(
                    f"No state found for {type.value} with id '{id}', returning empty dict"
                )
            else:
                logger.error(f"Failed to extract state: {str(e)}")
                raise

        return state

    @classmethod
    async def save_state(cls, key: str, value: Any, id: str, type: StateType) -> None:
        """Save state to the store.

        Args:
            key: The key to store the state under.
            value: The dictionary value to store.

        Raises:
            Exception: If there's an error with the Dapr client operations.

        Example:
            >>> from application_sdk.services.statestore import StateStore

            >>> await StateStore.save_state("test", {"test": "test"}, "wf-123")
        """
        try:
            # get the current state from object store
            current_state = await cls.get_state(id, type)
            state_file_path = build_state_store_path(id, type)

            # update the state with the new value
            current_state[key] = value

            os.makedirs(os.path.dirname(state_file_path), exist_ok=True)

            # save the state to a local file
            with open(state_file_path, "w") as file:
                json.dump(current_state, file)

            # save the state to the object store
            await ObjectStore.upload_file(
                source=state_file_path,
                destination=get_object_store_prefix(state_file_path),
                store_name=UPSTREAM_OBJECT_STORE_NAME,
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
            >>> from application_sdk.services.statestore import StateStore
            >>> await StateStore.save_state_object("wf-123", {"test": "test"}, "workflow")
        """
        try:
            logger.info(f"Saving state object for {id} with type {type}")
            # get the current state from object store
            current_state = await cls.get_state(id, type)
            state_file_path = build_state_store_path(id, type)

            # update the state with the new value
            current_state.update(value)

            os.makedirs(os.path.dirname(state_file_path), exist_ok=True)

            # save the state to a local file
            with open(state_file_path, "w") as file:
                json.dump(current_state, file)

            # save the state to the object store
            await ObjectStore.upload_file(
                source=state_file_path,
                destination=get_object_store_prefix(state_file_path),
                store_name=UPSTREAM_OBJECT_STORE_NAME,
            )
            logger.info(f"State object saved for {id} with type {type}")
            return current_state
        except Exception as e:
            logger.error(f"Failed to store state: {str(e)}")
            raise e
