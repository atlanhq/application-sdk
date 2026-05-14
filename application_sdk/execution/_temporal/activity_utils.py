"""SDK-internal Temporal activity utilities.

.. note::
    **For app authors and AI agents generating app code:** the helpers in
    this module are SDK-internal. They exist for framework dispatch and
    cleanup activities, where reaching into ``activity.info()`` is
    unavoidable. App code should not import from
    ``application_sdk.execution._temporal.activity_utils``.

    The canonical way for an app to access the workflow ID inside a
    task is to read it from the typed ``Input`` contract — the
    framework populates ``input.workflow_id`` at dispatch time
    (``application_sdk.handler.service`` sets it before the workflow
    starts):

    .. code-block:: python

        from application_sdk.contracts import Input

        class ExtractInput(Input):
            source_url: str

        @task(timeout_seconds=300)
        async def extract(self, input: ExtractInput) -> ExtractOutput:
            wf_id = input.workflow_id   # ← do this
            # not: from application_sdk.execution._temporal.activity_utils
            #      import get_workflow_id

    See ``application_sdk.contracts.base.Input.workflow_id`` and the
    ``atlan-openapi-app`` reference connector for the canonical
    pattern, including how to compose run-scoped object-store paths
    from ``input.workflow_id``.
"""

import os
from collections.abc import Callable
from typing import Any, TypeVar

from temporalio import activity

from application_sdk.constants import (
    APPLICATION_NAME,
    TEMPORARY_PATH,
    WORKFLOW_OUTPUT_PATH_TEMPLATE,
)
from application_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)


F = TypeVar("F", bound=Callable[..., Any])


def get_workflow_id() -> str:
    """SDK-internal: read the workflow ID from the current activity.

    .. note::
        **Apps and AI agents:** do not call this from app code. Read
        ``input.workflow_id`` from the task's typed ``Input`` contract
        instead — the framework populates it at dispatch time. See
        ``application_sdk.contracts.base.Input.workflow_id`` and the
        ``atlan-openapi-app`` reference connector. Reaching into
        ``execution._temporal`` couples the app to a private module
        path; reading the input contract keeps the activity decoupled
        from the Temporal runtime and trivially testable.

    Used by SDK framework code (cleanup activities, dispatch glue)
    where there is no ``Input`` parameter to read from.

    Returns:
        The workflow ID of the current activity.

    Raises:
        WorkflowIdError: If called outside an activity context, or if the
            workflow ID cannot be retrieved.
    """
    from application_sdk.execution._temporal._activity_errors import (  # noqa: PLC0415
        WorkflowIdError,
    )

    try:
        wf_id = activity.info().workflow_id
    except Exception as exc:
        raise WorkflowIdError(cause=exc) from exc
    if wf_id is None:
        raise WorkflowIdError(
            message="workflow_id unavailable outside a workflow-backed activity"
        )
    return wf_id


def get_workflow_run_id() -> str:
    """SDK-internal: read the workflow run ID from the current activity.

    .. note::
        **Apps and AI agents:** do not call this from app code. Apps
        generally do not need the per-attempt run ID — read
        ``input.workflow_id`` from the typed ``Input`` contract for
        run-scoped paths and identifiers. See
        ``application_sdk.contracts.base.Input.workflow_id`` and the
        ``atlan-openapi-app`` reference connector.

    Used by SDK framework code that genuinely needs the per-attempt
    run ID (e.g. cleanup of artifacts written under the current
    attempt's prefix).
    """
    from application_sdk.execution._temporal._activity_errors import (  # noqa: PLC0415
        WorkflowRunIdError,
    )

    try:
        wf_run_id = activity.info().workflow_run_id
    except Exception as exc:
        raise WorkflowRunIdError(cause=exc) from exc
    if wf_run_id is None:
        raise WorkflowRunIdError(
            message="workflow_run_id unavailable outside a workflow-backed activity"
        )
    return wf_run_id


def build_output_path() -> str:
    """SDK-internal: compose the standardized output path for the current run.

    Returns the path under ``WORKFLOW_OUTPUT_PATH_TEMPLATE`` for the
    workflow currently executing this activity (e.g.
    ``artifacts/apps/appName/workflows/wf-123/run-456``).

    .. note::
        **Apps and AI agents:** do not call this from app code. Compose
        run-scoped paths from ``input.workflow_id`` (the input
        contract field populated by the framework at dispatch time)
        instead of importing this helper. See
        ``application_sdk.contracts.base.Input.workflow_id`` and the
        ``atlan-openapi-app`` reference connector for the canonical
        pattern.

    Used by SDK framework code (``cleanup_files``, ``cleanup_storage``)
    that runs as a generic activity and has no ``Input`` parameter to
    read the workflow ID from.
    """
    return WORKFLOW_OUTPUT_PATH_TEMPLATE.format(
        application_name=APPLICATION_NAME,
        workflow_id=get_workflow_id(),
        run_id=get_workflow_run_id(),
    )


def get_object_store_prefix(path: str) -> str:
    """Convert a local-path or object-store-path into an object-store prefix.

    Handles two input shapes:

    1. Paths under ``TEMPORARY_PATH`` — converted to relative object-store keys.
    2. User-provided paths — returned as-is (already relative object-store keys).

    .. note::
        This is a path-shape utility, not a "where am I in the workflow"
        helper. App authors and AI agents should compose run-scoped
        paths from ``input.workflow_id`` (the typed ``Input`` contract
        field) rather than pairing this function with
        ``build_output_path()``. See
        ``application_sdk.contracts.base.Input.workflow_id`` and the
        ``atlan-openapi-app`` reference connector.

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
