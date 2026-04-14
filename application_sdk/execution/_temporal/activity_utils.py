"""Utility functions for Temporal activities.

This module provides utility functions for working with Temporal activities,
including workflow ID retrieval, automatic heartbeating, and periodic heartbeat sending.
"""

import os
from typing import Any, Callable, TypeVar

from temporalio import activity

from application_sdk.common.exc_utils import rewrap
from application_sdk.constants import (
    APPLICATION_NAME,
    TEMPORARY_PATH,
    WORKFLOW_OUTPUT_PATH_TEMPLATE,
)
from application_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)


F = TypeVar("F", bound=Callable[..., Any])


def get_workflow_id() -> str:
    """Get the workflow ID from the current activity.

    Retrieves the workflow ID from the current activity's context. This function
    must be called from within an activity execution context.

    Returns:
        The workflow ID of the current activity.

    Raises:
        RuntimeError: If called outside of an activity context.
        Exception: If there is an error retrieving the workflow ID.

    Example:
        >>> workflow_id = get_workflow_id()
        >>> print(workflow_id)  # e.g. "my-workflow-123"
    """
    try:
        return activity.info().workflow_id
    except Exception as e:
        raise rewrap(e, "Failed to get workflow id") from e


def get_workflow_run_id() -> str:
    """Get the workflow run ID from the current activity."""
    try:
        return activity.info().workflow_run_id
    except Exception as e:
        raise rewrap(e, "Failed to get workflow run id") from e


def build_output_path() -> str:
    """Build a standardized output path for workflow artifacts.

    This method creates a consistent output path format across all workflows using the WORKFLOW_OUTPUT_PATH_TEMPLATE constant.

    Returns:
        str: The standardized output path.

    Example:
        >>> build_output_path()
        "artifacts/apps/appName/workflows/wf-123/run-456"
    """
    return WORKFLOW_OUTPUT_PATH_TEMPLATE.format(
        application_name=APPLICATION_NAME,
        workflow_id=get_workflow_id(),
        run_id=get_workflow_run_id(),
    )


def get_object_store_prefix(path: str) -> str:
    """Get the object store prefix for the path.

    This function handles two types of paths:
    1. Paths under TEMPORARY_PATH - converts them to relative object store paths
    2. User-provided paths - returns them as-is (already relative object store paths)

    Args:
        path: The path to convert to object store prefix.

    Returns:
        The object store prefix for the path.

    Examples:
        >>> # Temporary path case
        >>> get_object_store_prefix("./local/tmp/artifacts/apps/appName/workflows/wf-123/run-456")
        "artifacts/apps/appName/workflows/wf-123/run-456"

        >>> # User-provided path case
        >>> get_object_store_prefix("datasets/sales/2024/")
        "datasets/sales/2024"
    """
    # Normalize paths for comparison
    abs_path = os.path.abspath(path)
    abs_temp_path = os.path.abspath(TEMPORARY_PATH)

    # Check if path is under TEMPORARY_PATH
    try:
        # Use os.path.commonpath to properly check if path is under temp directory
        # This prevents false positives like '/tmp/local123' matching '/tmp/local'
        common_path = os.path.commonpath([abs_path, abs_temp_path])
        if common_path == abs_temp_path:
            # Path is under temp directory, convert to relative object store path
            relative_path = os.path.relpath(abs_path, abs_temp_path)
            # Normalize path separators to forward slashes for object store
            return relative_path.replace(os.path.sep, "/")
        else:
            # Path is already a relative object store path, return as-is
            return path.strip("/")
    except ValueError:
        # os.path.commonpath or os.path.relpath can raise ValueError on Windows with different drives
        # In this case, treat as user-provided path, return as-is
        return path.strip("/")
