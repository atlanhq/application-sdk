"""Unit tests for MetadataExtractionWorkflow run_exit_activities."""

from unittest.mock import patch

import pytest

from application_sdk.activities.metadata_extraction.base import (
    BaseMetadataExtractionActivities,
)
from application_sdk.workflows.metadata_extraction import MetadataExtractionWorkflow


@pytest.fixture
def workflow():
    """Create a MetadataExtractionWorkflow instance."""
    wf = MetadataExtractionWorkflow()
    wf.activities_cls = BaseMetadataExtractionActivities
    return wf


@pytest.fixture
def workflow_args():
    """Sample workflow arguments."""
    return {
        "output_path": "/tmp/test-workflow/output",
        "workflow_id": "test-workflow-123",
    }


class TestRunExitActivities:
    """Test cases for run_exit_activities method."""

    @patch("application_sdk.workflows.metadata_extraction.ENABLE_ATLAN_UPLOAD", True)
    @patch("temporalio.workflow.execute_activity_method")
    async def test_run_exit_activities_upload_enabled(
        self,
        mock_execute_activity,
        workflow,
        workflow_args,
    ):
        """Test run_exit_activities calls upload_to_atlan when enabled."""
        mock_execute_activity.return_value = {"status": "success"}

        await workflow.run_exit_activities(workflow_args)

        mock_execute_activity.assert_called_once()
        call_args = mock_execute_activity.call_args

        # Verify the activity method being called
        assert call_args[0][0] == BaseMetadataExtractionActivities.upload_to_atlan

        # Verify workflow_args includes typename
        called_workflow_args = call_args[1]["args"][0]
        assert called_workflow_args["typename"] == "atlan-upload"

    @patch("application_sdk.workflows.metadata_extraction.ENABLE_ATLAN_UPLOAD", False)
    @patch("temporalio.workflow.execute_activity_method")
    async def test_run_exit_activities_upload_disabled(
        self,
        mock_execute_activity,
        workflow,
        workflow_args,
    ):
        """Test run_exit_activities skips upload when disabled."""
        await workflow.run_exit_activities(workflow_args)

        # Should not call execute_activity_method when disabled
        mock_execute_activity.assert_not_called()

    @patch("application_sdk.workflows.metadata_extraction.ENABLE_ATLAN_UPLOAD", True)
    @patch("temporalio.workflow.execute_activity_method")
    async def test_run_exit_activities_retry_policy(
        self,
        mock_execute_activity,
        workflow,
        workflow_args,
    ):
        """Test run_exit_activities uses correct retry policy."""
        mock_execute_activity.return_value = {"status": "success"}

        await workflow.run_exit_activities(workflow_args)

        call_args = mock_execute_activity.call_args
        retry_policy = call_args[1]["retry_policy"]

        # Verify retry policy settings
        assert retry_policy.maximum_attempts == 6
        assert retry_policy.backoff_coefficient == 2

    @patch("application_sdk.workflows.metadata_extraction.ENABLE_LAKEHOUSE_LOAD", True)
    @patch(
        "application_sdk.workflows.metadata_extraction.LH_LOAD_TRANSFORMED_NAMESPACE",
        "test_ns",
    )
    @patch(
        "application_sdk.workflows.metadata_extraction.LH_LOAD_TRANSFORMED_TABLE_NAME",
        "test_table",
    )
    @patch(
        "application_sdk.workflows.metadata_extraction.LH_LOAD_TRANSFORMED_MODE",
        "APPEND",
    )
    @patch("application_sdk.workflows.metadata_extraction.ENABLE_ATLAN_UPLOAD", False)
    @patch("temporalio.workflow.execute_activity_method")
    async def test_run_exit_activities_lakehouse_enabled(
        self,
        mock_execute_activity,
        workflow,
        workflow_args,
    ):
        """Test run_exit_activities calls load_to_lakehouse when enabled."""
        mock_execute_activity.return_value = {"status": "success"}

        await workflow.run_exit_activities(workflow_args)

        # Should have been called once for lakehouse load
        mock_execute_activity.assert_called_once()
        call_args = mock_execute_activity.call_args

        # Verify the activity method being called is load_to_lakehouse
        assert call_args[0][0] == BaseMetadataExtractionActivities.load_to_lakehouse

        # Verify lh_load_config is in the args
        called_args = call_args[1]["args"][0]
        assert "lh_load_config" in called_args
        assert called_args["lh_load_config"]["namespace"] == "test_ns"
        assert called_args["lh_load_config"]["table_name"] == "test_table"
        assert called_args["lh_load_config"]["file_extension"] == ".jsonl"

    @patch("application_sdk.workflows.metadata_extraction.ENABLE_LAKEHOUSE_LOAD", False)
    @patch("application_sdk.workflows.metadata_extraction.ENABLE_ATLAN_UPLOAD", False)
    @patch("temporalio.workflow.execute_activity_method")
    async def test_run_exit_activities_lakehouse_disabled(
        self,
        mock_execute_activity,
        workflow,
        workflow_args,
    ):
        """Test run_exit_activities skips lakehouse load when disabled."""
        await workflow.run_exit_activities(workflow_args)

        # Should not call any activity when both are disabled
        mock_execute_activity.assert_not_called()
