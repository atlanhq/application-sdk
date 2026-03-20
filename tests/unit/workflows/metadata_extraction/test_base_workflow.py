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
        "entity_metadata",
    )
    @patch(
        "application_sdk.workflows.metadata_extraction.LH_LOAD_TRANSFORMED_MODE",
        "APPEND",
    )
    @patch("application_sdk.workflows.metadata_extraction.ENABLE_ATLAN_UPLOAD", False)
    @patch("temporalio.workflow.execute_activity_method")
    async def test_run_exit_activities_lakehouse_loads_per_entity_type(
        self,
        mock_execute_activity,
        workflow,
        workflow_args,
    ):
        """Test run_exit_activities calls load_to_lakehouse once per extracted typename."""
        mock_execute_activity.return_value = {"status": "success"}
        workflow_args["_extracted_typenames"] = ["database", "table", "column"]

        await workflow.run_exit_activities(workflow_args)

        # Should have been called 3 times (one per typename)
        assert mock_execute_activity.call_count == 3

        # Verify each call targets the correct Iceberg table
        expected = [
            ("database", "database"),
            ("table", "table"),
            ("column", "column"),
        ]
        for i, (typename, iceberg_table) in enumerate(expected):
            call_args = mock_execute_activity.call_args_list[i]
            assert (
                call_args[0][0]
                == BaseMetadataExtractionActivities.load_to_lakehouse
            )
            lh_config = call_args[1]["args"][0]["lh_load_config"]
            assert lh_config["namespace"] == "entity_metadata"
            assert lh_config["table_name"] == iceberg_table
            assert lh_config["file_extension"] == ".jsonl"
            assert lh_config["output_path"].endswith(f"/transformed/{typename}")

    @patch("application_sdk.workflows.metadata_extraction.ENABLE_LAKEHOUSE_LOAD", True)
    @patch(
        "application_sdk.workflows.metadata_extraction.LH_LOAD_TRANSFORMED_NAMESPACE",
        "entity_metadata",
    )
    @patch("application_sdk.workflows.metadata_extraction.ENABLE_ATLAN_UPLOAD", False)
    @patch("temporalio.workflow.execute_activity_method")
    async def test_run_exit_activities_lakehouse_no_typenames(
        self,
        mock_execute_activity,
        workflow,
        workflow_args,
    ):
        """Test run_exit_activities skips lakehouse when no typenames extracted."""
        # No _extracted_typenames key
        await workflow.run_exit_activities(workflow_args)

        mock_execute_activity.assert_not_called()

    @patch("application_sdk.workflows.metadata_extraction.ENABLE_LAKEHOUSE_LOAD", True)
    @patch(
        "application_sdk.workflows.metadata_extraction.LH_LOAD_TRANSFORMED_NAMESPACE",
        "entity_metadata",
    )
    @patch("application_sdk.workflows.metadata_extraction.ENABLE_ATLAN_UPLOAD", False)
    @patch("temporalio.workflow.execute_activity_method")
    async def test_run_exit_activities_lakehouse_skips_unknown_typename(
        self,
        mock_execute_activity,
        workflow,
        workflow_args,
    ):
        """Test unknown typenames are skipped with a warning."""
        mock_execute_activity.return_value = {"status": "success"}
        workflow_args["_extracted_typenames"] = ["table", "unknown-type"]

        await workflow.run_exit_activities(workflow_args)

        # Only "table" should be loaded, "unknown-type" skipped
        assert mock_execute_activity.call_count == 1
        lh_config = mock_execute_activity.call_args[1]["args"][0]["lh_load_config"]
        assert lh_config["table_name"] == "table"

    @patch("application_sdk.workflows.metadata_extraction.ENABLE_LAKEHOUSE_LOAD", True)
    @patch(
        "application_sdk.workflows.metadata_extraction.LH_LOAD_TRANSFORMED_NAMESPACE",
        "entity_metadata",
    )
    @patch("application_sdk.workflows.metadata_extraction.ENABLE_ATLAN_UPLOAD", False)
    @patch("temporalio.workflow.execute_activity_method")
    async def test_run_exit_activities_lakehouse_procedure_mapping(
        self,
        mock_execute_activity,
        workflow,
        workflow_args,
    ):
        """Test extras-procedure maps to procedure Iceberg table."""
        mock_execute_activity.return_value = {"status": "success"}
        workflow_args["_extracted_typenames"] = ["extras-procedure"]

        await workflow.run_exit_activities(workflow_args)

        assert mock_execute_activity.call_count == 1
        lh_config = mock_execute_activity.call_args[1]["args"][0]["lh_load_config"]
        assert lh_config["table_name"] == "procedure"

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
