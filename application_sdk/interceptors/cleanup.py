import shutil
from datetime import timedelta
from pathlib import Path
from typing import Any, Dict, Optional, Type

from temporalio import activity, workflow
from temporalio.common import RetryPolicy
from temporalio.worker import (
    ExecuteWorkflowInput,
    Interceptor,
    WorkflowInboundInterceptor,
    WorkflowInterceptorClassInput,
)

from application_sdk.constants import APPLICATION_NAME, CLEANUP_BASE_PATHS
from application_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)


@activity.defn
async def cleanup_app_artifacts() -> Dict[str, bool]:
    """Activity to cleanup application artifacts outside the workflow sandbox.

    Automatically infers all required values from constants and activity context:
    - app_name: from APPLICATION_NAME constant
    - workflow_id: from activity.info().workflow_id
    - run_id: from activity.info().workflow_run_id
    - base_paths: from CLEANUP_BASE_PATHS constant

    Cleans up artifacts from multiple base paths following the pattern:
    base_path/appname/workflow_id/run_id

    Returns:
        Dict[str, bool]: Dictionary with cleanup results for each path

    Example cleanup scenarios:

    Scenario 1 - Cleanup specific run from multiple paths:
    Auto-detected: app_name="my_app", workflow_id="wf_123", run_id="run_456"
    Paths: ["/tmp/artifacts/apps/my_app/wf_123/run_456/", "/storage/temp/apps/my_app/wf_123/run_456/"]

    Scenario 2 - Cleanup entire workflow from multiple paths:
    Auto-detected: app_name="my_app", workflow_id="wf_123", run_id="run_456"
    Paths: ["/tmp/artifacts/apps/my_app/wf_123/run_456/", "/storage/temp/apps/my_app/wf_123/run_456/"]

    Directory Structure Before Cleanup (multiple base paths):
    Base Path 1: /tmp/artifacts/apps/
    +----+----+----+
    | apps/ | my_app/ | wf_123/ | run_456/ | data.parquet |
    |       |         |         | run_789/ | temp.csv     |
    +----+----+----+

    Base Path 2: /storage/temp/apps/
    +----+----+----+
    | apps/ | my_app/ | wf_123/ | run_456/ | backup.json |
    |       |         |         | run_789/ | logs.txt    |
    +----+----+----+

    Directory Structure After Cleanup (run level):
    Base Path 1: /tmp/artifacts/apps/
    +----+----+----+
    | apps/ | my_app/ | wf_123/ | run_789/ | temp.csv |
    +----+----+----+

    Base Path 2: /storage/temp/apps/
    +----+----+----+
    | apps/ | my_app/ | wf_123/ | run_789/ | logs.txt |
    +----+----+----+

    Transformations:
    - Removes specified directories from all base paths
    - Preserves parent directory structure
    - Logs cleanup success/failure for each path
    """
    cleanup_results = {}

    # Get all values from constants and activity context
    app_name = APPLICATION_NAME
    workflow_id = activity.info().workflow_id
    run_id = activity.info().workflow_run_id
    base_paths = CLEANUP_BASE_PATHS

    activity.logger.info(
        f"Auto-detected cleanup context: app={app_name}, workflow={workflow_id}, run={run_id}"
    )
    activity.logger.info(f"Cleanup base paths: {base_paths}")

    for base_path in base_paths:
        try:
            # Build the cleanup path based on auto-detected parameters
            cleanup_path = Path(base_path) / app_name / workflow_id / run_id

            cleanup_path_str = str(cleanup_path)

            if cleanup_path.exists():
                if cleanup_path.is_dir():
                    # Remove the entire directory and its contents
                    shutil.rmtree(cleanup_path_str)
                    activity.logger.info(f"Cleaned up directory: {cleanup_path_str}")
                    cleanup_results[cleanup_path_str] = True
                else:
                    activity.logger.warning(
                        f"Path is not a directory: {cleanup_path_str}"
                    )
                    cleanup_results[cleanup_path_str] = False
            else:
                activity.logger.debug(
                    f"Directory already removed or doesn't exist: {cleanup_path_str}"
                )
                cleanup_results[cleanup_path_str] = True

        except PermissionError as e:
            activity.logger.error(
                f"Permission denied cleaning up {cleanup_path_str}: {e}"
            )
            cleanup_results[cleanup_path_str] = False
        except OSError as e:
            activity.logger.error(f"OS error cleaning up {cleanup_path_str}: {e}")
            cleanup_results[cleanup_path_str] = False
        except Exception as e:
            activity.logger.error(
                f"Unexpected error cleaning up {cleanup_path_str}: {e}"
            )
            cleanup_results[cleanup_path_str] = False

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
        workflow_id = input.info.workflow_id
        workflow_type = input.info.workflow_type
        run_id = input.info.run_id

        workflow.logger.info(
            f"Starting workflow with cleanup tracking: {workflow_type}"
        )

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
                workflow.logger.info(
                    f"Cleaning up artifacts for workflow: {workflow_id}, run: {run_id}"
                )

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
