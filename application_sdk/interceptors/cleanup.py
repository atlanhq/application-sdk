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

from application_sdk.constants import APPLICATION_NAME, CLEANUP_BASE_PATH
from application_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)


@activity.defn
async def cleanup_app_artifacts(
    app_name: str,
    workflow_id: Optional[str] = None,
    run_id: Optional[str] = None,
    base_path: str = CLEANUP_BASE_PATH,
) -> Dict[str, bool]:
    """Activity to cleanup application artifacts outside the workflow sandbox.

    Cleans up artifacts following the pattern: local/tmp/artifacts/apps/appname/workflow_id/run_id
    Can cleanup at different levels based on provided parameters.

    Args:
        app_name (str): Name of the application
        workflow_id (Optional[str]): Workflow ID to cleanup. If None, cleans entire app directory
        run_id (Optional[str]): Run ID to cleanup. If None, cleans entire workflow directory
        base_path (str): Base path for artifacts (configurable via ATLAN_CLEANUP_BASE_PATH env var)

    Returns:
        Dict[str, bool]: Dictionary with cleanup results

    Example cleanup scenarios:

    Scenario 1 - Cleanup specific run:
    Input: app_name="my_app", workflow_id="wf_123", run_id="run_456"
    Path: {CLEANUP_BASE_PATH}/my_app/wf_123/run_456/

    Scenario 2 - Cleanup entire workflow:
    Input: app_name="my_app", workflow_id="wf_123", run_id=None
    Path: {CLEANUP_BASE_PATH}/my_app/wf_123/

    Scenario 3 - Cleanup entire app:
    Input: app_name="my_app", workflow_id=None, run_id=None
    Path: {CLEANUP_BASE_PATH}/my_app/

    Directory Structure Before Cleanup:
    +----+----+----+
    | apps/ | my_app/ | wf_123/ | run_456/ | data.parquet |
    |       |         |         | run_789/ | temp.csv     |
    |       |         | wf_124/ | run_001/ | output.json  |
    +----+----+----+

    Directory Structure After Cleanup (workflow level):
    +----+----+----+
    | apps/ | my_app/ | wf_124/ | run_001/ | output.json |
    +----+----+----+

    Transformations:
    - Removes all files and subdirectories at target level
    - Preserves parent directory structure
    - Logs cleanup success/failure for each operation
    """
    cleanup_results = {}

    try:
        # Build the cleanup path based on provided parameters
        cleanup_path = Path(base_path) / app_name

        if workflow_id:
            cleanup_path = cleanup_path / workflow_id

        if run_id:
            cleanup_path = cleanup_path / run_id

        cleanup_path_str = str(cleanup_path)

        if cleanup_path.exists():
            if cleanup_path.is_dir():
                # Remove the entire directory and its contents
                shutil.rmtree(cleanup_path_str)
                activity.logger.info(f"Cleaned up directory: {cleanup_path_str}")
                cleanup_results[cleanup_path_str] = True
            else:
                activity.logger.warning(f"Path is not a directory: {cleanup_path_str}")
                cleanup_results[cleanup_path_str] = False
        else:
            activity.logger.debug(
                f"Directory already removed or doesn't exist: {cleanup_path_str}"
            )
            cleanup_results[cleanup_path_str] = True

    except PermissionError as e:
        activity.logger.error(f"Permission denied cleaning up {cleanup_path_str}: {e}")
        cleanup_results[cleanup_path_str] = False
    except OSError as e:
        activity.logger.error(f"OS error cleaning up {cleanup_path_str}: {e}")
        cleanup_results[cleanup_path_str] = False
    except Exception as e:
        activity.logger.error(f"Unexpected error cleaning up {cleanup_path_str}: {e}")
        cleanup_results[cleanup_path_str] = False

    return cleanup_results


class CleanupWorkflowInboundInterceptor(WorkflowInboundInterceptor):
    """Interceptor for workflow-level app artifacts cleanup.

    This interceptor cleans up the entire app directory structure when the workflow
    completes or fails, following the pattern: {CLEANUP_BASE_PATH}/appname/workflow_id/run_id
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
                    args=[APPLICATION_NAME, workflow_id, run_id, CLEANUP_BASE_PATH],
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
    following the directory pattern: {CLEANUP_BASE_PATH}/appname/workflow_id/run_id

    Features:
    - Automatic cleanup of app-specific artifact directories
    - Cleanup on workflow completion or failure
    - Uses APPLICATION_NAME from constants
    - Configurable cleanup path via ATLAN_CLEANUP_BASE_PATH env var
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
