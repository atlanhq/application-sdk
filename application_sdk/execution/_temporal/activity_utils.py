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
import warnings
from collections.abc import Callable
from typing import Any, TypeVar

from temporalio import activity

from application_sdk import constants
from application_sdk.app.registry import AppRegistry
from application_sdk.constants import WORKFLOW_OUTPUT_PATH_TEMPLATE
from application_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)

#: name -> (replacement, why). These names stopped being used here and were importable
#: from here only by accident; they are served once more so imports of them
#: keep working, with a warning pointing at their home.
_DEPRECATED_CONSTANTS: dict[str, tuple[str, str]] = {
    "TEMPORARY_PATH": (
        "application_sdk.constants.TEMPORARY_PATH",
        "it was only ever re-exported here as a side effect of an import",
    ),
}


def __getattr__(name: str) -> object:
    """Serve the removed re-exports once more, with a deprecation warning (PEP 562)."""
    entry = _DEPRECATED_CONSTANTS.get(name)
    if entry is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    replacement, note = entry
    warnings.warn(
        f"{name} is deprecated here; use {replacement} instead — {note}. "
        "Will be removed in v4.0.0.",
        DeprecationWarning,
        stacklevel=2,
    )
    module_name, _, attr = replacement.rpartition(".")
    from importlib import import_module  # noqa: PLC0415 — resolved on access only

    return getattr(import_module(module_name), attr)


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
    # conformance: ignore[E004] re-raises as typed WorkflowIdError; no swallowing occurs
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
    # conformance: ignore[E004,E007] re-raises immediately as typed WorkflowRunIdError; broad catch is intentional to wrap any unexpected SDK error
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
        application_name=AppRegistry.resolve_running_app_name(),
        workflow_id=get_workflow_id(),
        run_id=get_workflow_run_id(),
    )


def run_temp_dir() -> str:
    """SDK-internal: the local scratch directory of the current run.

    ``{TEMPORARY_PATH}/{build_output_path()}``, e.g.
    ``./local/tmp/artifacts/apps/appName/workflows/wf-123/run-456``. It is the
    directory ``cleanup_files`` removes by default, so anything the SDK writes
    under it on the run's behalf goes with the run. ``TEMPORARY_PATH`` is read
    at call time so a redirect of the constant is honoured.

    The workflow ID is caller-chosen (``/workflows/v1/start`` accepts it
    verbatim), so the result is checked to resolve strictly *inside*
    ``TEMPORARY_PATH``: not outside it via ``..`` segments, and not the root
    itself, whose removal would take every other run's scratch with it.

    Raises:
        WorkflowIdError: Outside an activity context.
        WorkflowRunIdError: Outside a workflow-backed activity.
        RunTempDirEscapeError: If the directory would not be strictly inside
            ``TEMPORARY_PATH``.
    """
    from application_sdk.execution._temporal._activity_errors import (  # noqa: PLC0415
        RunTempDirEscapeError,
    )

    path = os.path.join(constants.TEMPORARY_PATH, build_output_path())
    root = os.path.realpath(constants.TEMPORARY_PATH)
    resolved = os.path.realpath(path)
    try:
        contained = resolved != root and os.path.commonpath([root, resolved]) == root
    except ValueError:  # different drives on Windows: not contained
        contained = False
    if not contained:
        raise RunTempDirEscapeError()
    return path


def get_object_store_prefix(path: str) -> str:
    """Convert a local-path or object-store-path into an object-store prefix.

    A delegate to :func:`application_sdk.storage.ops.normalize_key`, the SDK's
    single local-path → store-key normaliser. Kept as a public name so existing
    callers keep working; the two used to diverge (the staging root mapped to
    ``'.'`` here, and backslashes survived on POSIX).

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
    from application_sdk.storage.ops import (  # noqa: PLC0415 — activity frame only; keep obstore out of the workflow-sandbox import set
        normalize_key,
    )

    return normalize_key(path)
