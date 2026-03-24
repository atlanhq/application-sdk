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


class TestLoadRawToLakehouse:
    """Test cases for LakehouseLoadMixin.load_raw_to_lakehouse."""

    @patch(
        "application_sdk.workflows.metadata_extraction.lakehouse.ENABLE_LAKEHOUSE_LOAD",
        True,
    )
    @patch(
        "application_sdk.workflows.metadata_extraction.lakehouse.LH_LOAD_RAW_NAMESPACE",
        "int_entity_raw",
    )
    @patch(
        "application_sdk.workflows.metadata_extraction.lakehouse.LH_LOAD_RAW_TABLE_NAME",
        "postgres",
    )
    @patch(
        "application_sdk.workflows.metadata_extraction.lakehouse.LH_LOAD_RAW_MODE",
        "APPEND",
    )
    @patch("temporalio.workflow.execute_activity_method")
    async def test_load_raw_calls_prepare_then_load(
        self,
        mock_execute_activity,
        workflow,
        workflow_args,
    ):
        """load_raw_to_lakehouse calls prepare_raw_for_lakehouse then load_to_lakehouse."""
        mock_execute_activity.return_value = "/tmp/out/raw_lakehouse"

        await workflow.load_raw_to_lakehouse(workflow_args, ["database", "table"])

        assert mock_execute_activity.call_count == 2

        # First call: prepare_raw_for_lakehouse
        first_call = mock_execute_activity.call_args_list[0]
        assert (
            first_call[0][0]
            == BaseMetadataExtractionActivities.prepare_raw_for_lakehouse
        )

        # Second call: load_to_lakehouse with raw config
        second_call = mock_execute_activity.call_args_list[1]
        assert second_call[0][0] == BaseMetadataExtractionActivities.load_to_lakehouse
        lh_config = second_call[1]["args"][0]["lh_load_config"]
        assert lh_config["namespace"] == "int_entity_raw"
        assert lh_config["table_name"] == "postgres"
        assert lh_config["mode"] == "APPEND"
        assert lh_config["file_extension"] == ".parquet"

    @patch(
        "application_sdk.workflows.metadata_extraction.lakehouse.ENABLE_LAKEHOUSE_LOAD",
        False,
    )
    @patch("temporalio.workflow.execute_activity_method")
    async def test_load_raw_skipped_when_disabled(
        self,
        mock_execute_activity,
        workflow,
        workflow_args,
    ):
        """load_raw_to_lakehouse is a no-op when ENABLE_LAKEHOUSE_LOAD is False."""
        await workflow.load_raw_to_lakehouse(workflow_args, ["database"])

        mock_execute_activity.assert_not_called()

    @patch(
        "application_sdk.workflows.metadata_extraction.lakehouse.ENABLE_LAKEHOUSE_LOAD",
        True,
    )
    @patch(
        "application_sdk.workflows.metadata_extraction.lakehouse.LH_LOAD_RAW_NAMESPACE",
        "int_entity_raw",
    )
    @patch(
        "application_sdk.workflows.metadata_extraction.lakehouse.LH_LOAD_RAW_TABLE_NAME",
        "postgres",
    )
    @patch("temporalio.workflow.execute_activity_method")
    async def test_load_raw_skipped_when_no_typenames(
        self,
        mock_execute_activity,
        workflow,
        workflow_args,
    ):
        """load_raw_to_lakehouse is a no-op with empty typenames list."""
        await workflow.load_raw_to_lakehouse(workflow_args, [])

        mock_execute_activity.assert_not_called()
