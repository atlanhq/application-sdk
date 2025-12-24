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
        """Check if a string value is a valid StateType member.

        Args:
            type (str): The string value to check.

        Returns:
            bool: True if the value is a valid StateType, False otherwise.

        Examples:
            >>> StateType.is_member("workflows")
            True
            >>> StateType.is_member("invalid")
            False
        """
        return type in cls._value2member_map_


def build_state_store_path(id: str, state_type: StateType) -> str:
    """Build the state file path for the given id and type.

    Args:
        id (str): The unique identifier for the state.
        state_type (StateType): The type of state (WORKFLOWS or CREDENTIALS).

    Returns:
        str: The constructed state file path.

    Examples:
        >>> from application_sdk.services.statestore import build_state_store_path, StateType

        >>> # Workflow state path
        >>> path = build_state_store_path("workflow-123", StateType.WORKFLOWS)
        >>> print(path)
        './local/tmp/persistent-artifacts/apps/appName/workflows/workflow-123/config.json'

        >>> # Credential state path
        >>> cred_path = build_state_store_path("db-cred-456", StateType.CREDENTIALS)
        >>> print(cred_path)
        './local/tmp/persistent-artifacts/apps/appName/credentials/db-cred-456/config.json'
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
            id (str): The unique identifier to retrieve the state for.
            type (StateType): The type of state to retrieve (WORKFLOWS or CREDENTIALS).

        Returns:
            Dict[str, Any]: The retrieved state data. Returns empty dict if no state found.

        Raises:
            IOError: If there's an error with the object store operations.
            Exception: If there's an unexpected error during state retrieval.

        Examples:
            >>> from application_sdk.services.statestore import StateStore, StateType

            >>> # Get workflow state
            >>> state = await StateStore.get_state("workflow-123", StateType.WORKFLOWS)
            >>> print(f"Current status: {state.get('status', 'unknown')}")

            >>> # Get credential configuration
            >>> creds = await StateStore.get_state("db-cred-456", StateType.CREDENTIALS)
            >>> print(f"Database: {creds.get('database')}")
        """
        state_file_path = build_state_store_path(id, type)
        try:
            object_store_content = await ObjectStore.get_content(
                get_object_store_prefix(state_file_path),
                store_name=UPSTREAM_OBJECT_STORE_NAME,
                suppress_error=True,
            )
            if not object_store_content:
                logger.warning(
                    f"No state found for {type.value} with id '{id}', returning empty dict"
                )
                return {}

            state = json.loads(object_store_content)
            logger.info(f"State object retrieved for {id} with type {type}")

            return state
        except Exception as e:
            logger.error(f"Failed to extract state: {str(e)}")
            raise

    @classmethod
    async def save_state(cls, key: str, value: Any, id: str, type: StateType) -> None:
        """Save a single state value to the store.

        This method updates a specific key within the state object, merging with existing state.

        Args:
            key (str): The key to store the state value under.
            value (Any): The value to store (can be any JSON-serializable type).
            id (str): The unique identifier for the state object.
            type (StateType): The type of state (WORKFLOWS or CREDENTIALS).

        Raises:
            Exception: If there's an error with the object store operations.

        Examples:
            >>> from application_sdk.services.statestore import StateStore, StateType

            >>> # Update workflow progress
            >>> await StateStore.save_state(
            ...     key="progress",
            ...     value=75,
            ...     id="workflow-123",
            ...     type=StateType.WORKFLOWS
            ... )

            >>> # Update workflow status with dict
            >>> await StateStore.save_state(
            ...     key="execution_info",
            ...     value={"started_at": "2024-01-15T10:00:00Z", "worker_id": "worker-1"},
            ...     id="workflow-123",
            ...     type=StateType.WORKFLOWS
            ... )
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

        This method merges the provided value with existing state and saves the complete object.

        Args:
            id (str): The unique identifier for the state object.
            value (Dict[str, Any]): The state data to save/merge.
            type (StateType): The type of state (WORKFLOWS or CREDENTIALS).

        Returns:
            Dict[str, Any]: The complete updated state after merge.

        Raises:
            Exception: If there's an error with the object store operations.

        Examples:
            >>> from application_sdk.services.statestore import StateStore, StateType

            >>> # Save complete workflow state
            >>> workflow_state = {
            ...     "status": "running",
            ...     "current_step": "data_processing",
            ...     "progress": 50,
            ...     "config": {"batch_size": 1000}
            ... }
            >>> updated = await StateStore.save_state_object(
            ...     id="workflow-123",
            ...     value=workflow_state,
            ...     type=StateType.WORKFLOWS
            ... )
            >>> print(f"Final state has {len(updated)} keys")

            >>> # Save credential configuration
            >>> cred_config = {
            ...     "credential_type": "database",
            ...     "host": "db.example.com",
            ...     "port": 5432
            ... }
            >>> await StateStore.save_state_object(
            ...     id="db-cred-456",
            ...     value=cred_config,
            ...     type=StateType.CREDENTIALS
            ... )
        """
        try:
            logger.info(
                f"Saving state object in object store for {id} with type {type}"
            )
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
            logger.info(
                f"State object created in object store for {id} with type {type}"
            )
            return current_state
        except Exception as e:
            logger.error(f"Failed to store state: {str(e)}")
            raise e
