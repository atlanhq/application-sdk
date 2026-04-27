"""Utility functions for Temporal activities.

This module provides utility functions for working with Temporal activities,
including workflow ID retrieval, automatic heartbeating, and periodic heartbeat sending.

.. note::
    The public ``get_workflow_id`` / ``get_workflow_run_id`` / ``build_output_path``
    helpers in this module are **deprecated**. Apps should not reach into
    ``execution._temporal.activity_utils``; use the input contract instead:

    .. code-block:: python

        @task(timeout_seconds=300)
        async def extract(self, input: ExtractInput) -> ExtractOutput:
            workflow_id = input.workflow_id  # populated by the framework
            ...

    See ``application_sdk.contracts.base.Input.workflow_id`` and the
    ``atlan-openapi-app`` reference connector for the canonical pattern.

    The private ``_get_workflow_id`` / ``_get_workflow_run_id`` /
    ``_build_output_path`` helpers remain for SDK-internal use (cleanup
    activities, framework dispatch) where reaching for ``activity.info()``
    is unavoidable.
"""

import os
import warnings
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


# ---------------------------------------------------------------------------
# Private helpers — SDK-internal only.
#
# Used by framework code that has no Input parameter (cleanup activities,
# dispatch glue). Apps must not import these. App code should read
# ``input.workflow_id`` from the task's typed Input contract.
# ---------------------------------------------------------------------------


def _get_workflow_id() -> str:
    """Get workflow ID from the current activity (SDK-internal)."""
    try:
        return activity.info().workflow_id
    except Exception as e:
        raise rewrap(e, "Failed to get workflow id") from e


def _get_workflow_run_id() -> str:
    """Get workflow run ID from the current activity (SDK-internal)."""
    try:
        return activity.info().workflow_run_id
    except Exception as e:
        raise rewrap(e, "Failed to get workflow run id") from e


def _build_output_path() -> str:
    """Build output path from current activity context (SDK-internal)."""
    return WORKFLOW_OUTPUT_PATH_TEMPLATE.format(
        application_name=APPLICATION_NAME,
        workflow_id=_get_workflow_id(),
        run_id=_get_workflow_run_id(),
    )


# ---------------------------------------------------------------------------
# Deprecated public API.
#
# These shipped in earlier SDK versions and are still imported by 8+ connector
# apps. They emit ``DeprecationWarning`` at call time so apps and AI agents
# migrate to the input contract pattern.
# ---------------------------------------------------------------------------


def get_workflow_id() -> str:
    """Get the workflow ID from the current activity.

    .. deprecated::
        Apps should read ``input.workflow_id`` from the task's typed
        ``Input`` contract instead of reaching into
        ``execution._temporal.activity_utils``.

        The framework populates ``Input.workflow_id`` at dispatch time
        (see ``application_sdk.handler.service`` and
        ``application_sdk.contracts.base.Input``). Reading it from the
        input keeps activities decoupled from the Temporal runtime, makes
        them trivially testable, and matches the canonical pattern in
        the ``atlan-openapi-app`` reference connector.

        .. code-block:: python

            # Before — couples app to internal Temporal helpers:
            from application_sdk.execution._temporal.activity_utils import get_workflow_id
            wf_id = get_workflow_id()

            # After — uses the public input contract:
            @task(timeout_seconds=300)
            async def extract(self, input: ExtractInput) -> ExtractOutput:
                wf_id = input.workflow_id

        Will be removed in v3.5.0.

    Returns:
        The workflow ID of the current activity.

    Raises:
        Exception: If called outside an activity context, or if the
            workflow ID cannot be retrieved.
    """
    warnings.warn(
        "get_workflow_id() is deprecated. Read input.workflow_id from the "
        "task's typed Input contract instead — the framework populates it "
        "at dispatch time. See application_sdk.contracts.base.Input and "
        "the atlan-openapi-app reference connector. "
        "Will be removed in v3.5.0.",
        DeprecationWarning,
        stacklevel=2,
    )
    return _get_workflow_id()


def get_workflow_run_id() -> str:
    """Get the workflow run ID from the current activity.

    .. deprecated::
        Apps should read ``input.workflow_id`` from the typed ``Input``
        contract for run-scoped paths and identifiers; the run ID is an
        intra-attempt detail of Temporal that apps generally do not need.
        If you do need it, use ``activity.info().workflow_run_id``
        directly inside framework code rather than this helper.
        Will be removed in v3.5.0.
    """
    warnings.warn(
        "get_workflow_run_id() is deprecated. Apps should read "
        "input.workflow_id from the typed Input contract for run-scoped "
        "paths; if you genuinely need the per-attempt run ID, use "
        "activity.info().workflow_run_id directly. "
        "Will be removed in v3.5.0.",
        DeprecationWarning,
        stacklevel=2,
    )
    return _get_workflow_run_id()


def build_output_path() -> str:
    """Build a standardized output path for workflow artifacts.

    .. deprecated::
        Apps should compose paths from ``input.workflow_id`` (the input
        contract field populated by the framework at dispatch time)
        rather than reaching into
        ``execution._temporal.activity_utils``. See the
        ``atlan-openapi-app`` reference connector for the canonical
        pattern. Will be removed in v3.5.0.

    Returns:
        str: The standardized output path
        (``artifacts/apps/{app}/workflows/{wf_id}/{run_id}``).
    """
    warnings.warn(
        "build_output_path() is deprecated. Compose paths from "
        "input.workflow_id (populated by the framework on the typed Input "
        "contract) instead of importing this helper. "
        "See the atlan-openapi-app reference connector. "
        "Will be removed in v3.5.0.",
        DeprecationWarning,
        stacklevel=2,
    )
    return _build_output_path()


def get_object_store_prefix(path: str) -> str:
    """Convert a local-path or object-store-path into an object-store prefix.

    Handles two input shapes:

    1. Paths under ``TEMPORARY_PATH`` — converted to relative object-store keys.
    2. User-provided paths — returned as-is (already relative object-store keys).

    .. note::
        This is a path-shape utility, not a "where am I in the workflow"
        helper. Apps that need workflow identity should read
        ``input.workflow_id`` from the input contract rather than building
        paths via ``build_output_path()`` + this function. See
        ``atlan-openapi-app`` for the canonical pattern.

    Args:
        path: The path to convert to object store prefix.

    Returns:
        The object store prefix for the path.

    Examples:
        >>> get_object_store_prefix("./local/tmp/artifacts/apps/appName/workflows/wf-123/run-456")
        "artifacts/apps/appName/workflows/wf-123/run-456"

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
