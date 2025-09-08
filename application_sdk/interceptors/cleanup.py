import os
import shutil
from datetime import timedelta
from typing import Any, Dict, Optional, Type

from temporalio import activity, workflow
from temporalio.common import RetryPolicy
from temporalio.worker import (
    ExecuteWorkflowInput,
    Interceptor,
    WorkflowInboundInterceptor,
    WorkflowInterceptorClassInput,
)

from application_sdk.constants import (
    APPLICATION_NAME,
    CLEANUP_BASE_PATHS,
    TEMPORARY_PATH,
)
from application_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)


@activity.defn
async def cleanup_app_artifacts() -> Dict[str, bool]:
    """Activity to cleanup all temporary artifacts from configured base paths.

    Removes all contents from each configured base path, leaving the directories empty.
    This provides a complete cleanup of all temporary files and directories.

    Uses CLEANUP_BASE_PATHS constant to determine which directories to clean.
    If no paths are configured, defaults to: {TEMPORARY_PATH}artifacts/apps/{app_name}/workflows/{workflow_id}/
    The workflow_id is automatically obtained from the activity context.

    Returns:
        Dict[str, bool]: Dictionary with cleanup results for each base path

    Example cleanup scenarios:

    Scenario 1 - Using default path (no CLEANUP_BASE_PATHS configured):
    APPLICATION_NAME = "my-publish-app"
    workflow_id = "wf-12345"
    Default cleanup path: "./local/tmp/artifacts/apps/my-publish-app/workflows/wf-12345/"

    Scenario 2 - Using configured paths:
    CLEANUP_BASE_PATHS = ["./local/tmp", "/storage/temp"]

    Directory Structure Before Cleanup:
    Base Path 1: ./local/tmp/
    +----+----+----+
    | artifacts/ | apps/ | my_app/ | wf_123/ | data.parquet |
    | logs/      | temp/ | cache/  | file.txt |
    +----+----+----+

    Base Path 2: /storage/temp/
    +----+----+----+
    | backup/    | old/  | archive/ | backup.json |
    | downloads/ | misc/ | temp.csv |
    +----+----+----+

    Directory Structure After Cleanup:
    Base Path 1: ./local/tmp/
    (empty directory)

    Base Path 2: /storage/temp/
    (empty directory)

    Transformations:
    - Removes all files and subdirectories from each base path
    - Preserves the base path directories themselves
    - Logs cleanup success/failure for each path
    """
    cleanup_results = {}

    # Get workflow_id from activity context
    workflow_id = activity.info().workflow_id

    # Use configured paths or default to workflow-specific artifacts directory
    if CLEANUP_BASE_PATHS:
        base_paths = CLEANUP_BASE_PATHS
    else:
        default_path = (
            f"{TEMPORARY_PATH}artifacts/apps/{APPLICATION_NAME}/workflows/{workflow_id}"
        )
        base_paths = [default_path]
        activity.logger.info(
            f"No CLEANUP_BASE_PATHS configured, using default workflow path: {default_path}"
        )

    activity.logger.info(f"Cleaning up all contents from base paths: {base_paths}")

    for base_path in base_paths:
        try:
            cleanup_path = str(base_path)

            if os.path.exists(cleanup_path):
                if os.path.isdir(cleanup_path):
                    # Remove all contents inside the base path, but keep the directory itself
                    for item in os.listdir(cleanup_path):
                        item_path = os.path.join(cleanup_path, item)
                        if os.path.isdir(item_path):
                            shutil.rmtree(item_path)
                        else:
                            os.remove(item_path)
                    activity.logger.info(
                        f"Cleaned up all contents from: {cleanup_path}"
                    )
                    cleanup_results[cleanup_path] = True
                else:
                    activity.logger.warning(f"Path is not a directory: {cleanup_path}")
                    cleanup_results[cleanup_path] = False
            else:
                activity.logger.debug(f"Directory doesn't exist: {cleanup_path}")
                cleanup_results[cleanup_path] = True

        except PermissionError as e:
            activity.logger.error(f"Permission denied cleaning up {cleanup_path}: {e}")
            cleanup_results[cleanup_path] = False
        except OSError as e:
            activity.logger.error(f"OS error cleaning up {cleanup_path}: {e}")
            cleanup_results[cleanup_path] = False
        except Exception as e:
            activity.logger.error(f"Unexpected error cleaning up {cleanup_path}: {e}")
            cleanup_results[cleanup_path] = False

    return cleanup_results


class CleanupWorkflowInboundInterceptor(WorkflowInboundInterceptor):
    """Interceptor for workflow-level app artifacts cleanup.

    This interceptor cleans up the entire app directory structure when the workflow
    completes or fails, following the pattern: base_path/appname/workflow_id/run_id
    Supports multiple base paths for comprehensive cleanup.
    """

    async def execute_workflow(self, input: ExecuteWorkflowInput) -> Any:
        """Execute a workflow with app artifacts cleanup.

        Args:
            input (ExecuteWorkflowInput): The workflow execution input

        Returns:
            Any: The result of the workflow execution

        Raises:
            Exception: Re-raises any exceptions from workflow execution
        """

        workflow_type = input.type

        output = None
        try:
            output = await super().execute_workflow(input)
            workflow.logger.info(f"Workflow {workflow_type} completed successfully")

        except Exception as e:
            workflow.logger.error(f"Workflow {workflow_type} failed: {e}")
            raise

        finally:
            # Always attempt cleanup regardless of workflow success/failure
            try:
                await workflow.execute_activity(
                    cleanup_app_artifacts,
                    schedule_to_close_timeout=timedelta(minutes=5),
                    retry_policy=RetryPolicy(
                        maximum_attempts=3,
                        initial_interval=timedelta(seconds=1),
                        maximum_interval=timedelta(seconds=10),
                    ),
                )

                workflow.logger.info("Cleanup completed successfully")

            except Exception as e:
                workflow.logger.warning(f"Failed to cleanup artifacts: {e}")
                # Don't re-raise - cleanup failures shouldn't fail the workflow

        return output


class CleanupInterceptor(Interceptor):
    """Temporal interceptor for automatic app artifacts cleanup.

    This interceptor provides cleanup capabilities for application artifacts
    across multiple base paths following the pattern: base_path/appname/workflow_id/run_id

    Features:
    - Automatic cleanup of app-specific artifact directories
    - Cleanup on workflow completion or failure
    - Uses APPLICATION_NAME from constants
    - Supports multiple cleanup paths via ATLAN_CLEANUP_BASE_PATHS env var
    - Simple activity-based cleanup logic
    - Comprehensive error handling and logging

    Example:
        >>> # Register the interceptor with Temporal worker
        >>> worker = Worker(
        ...     client,
        ...     task_queue="my-task-queue",
        ...     workflows=[MyWorkflow],
        ...     activities=[my_activity, cleanup_app_artifacts],
        ...     interceptors=[CleanupInterceptor()]
        ... )

    Environment Configuration:
        >>> # Single path (default)
        >>> ATLAN_CLEANUP_BASE_PATHS="./local/tmp/artifacts/apps"

        >>> # Multiple paths (comma-separated)
        >>> ATLAN_CLEANUP_BASE_PATHS="./local/tmp/artifacts/apps,/storage/temp/apps,/shared/cleanup/apps"
    """

    def workflow_interceptor_class(
        self, input: WorkflowInterceptorClassInput
    ) -> Optional[Type[WorkflowInboundInterceptor]]:
        """Get the workflow interceptor class for cleanup.

        Args:
            input (WorkflowInterceptorClassInput): The interceptor input

        Returns:
            Optional[Type[WorkflowInboundInterceptor]]: The workflow interceptor class
        """
        return CleanupWorkflowInboundInterceptor
