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
        "application_sdk.workflows.metadata_extraction.lakehouse.ENABLE_LAKEHOUSE_LOAD",
        True,
    )
    @patch(
        "application_sdk.workflows.metadata_extraction.lakehouse.LH_LOAD_TRANSFORMED_NAMESPACE",
        "entity_metadata",
    )
    @patch(
        "application_sdk.workflows.metadata_extraction.lakehouse.LH_LOAD_TRANSFORMED_MODE",
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
            assert call_args[0][0] == BaseMetadataExtractionActivities.load_to_lakehouse
            lh_config = call_args[1]["args"][0]["lh_load_config"]
            assert lh_config["namespace"] == "entity_metadata"
            assert lh_config["table_name"] == iceberg_table
            assert lh_config["file_extension"] == ".jsonl"
            assert lh_config["output_path"].endswith(f"/transformed/{typename}")

    @patch("application_sdk.workflows.metadata_extraction.ENABLE_LAKEHOUSE_LOAD", True)
    @patch(
        "application_sdk.workflows.metadata_extraction.lakehouse.ENABLE_LAKEHOUSE_LOAD",
        True,
    )
    @patch(
        "application_sdk.workflows.metadata_extraction.lakehouse.LH_LOAD_TRANSFORMED_NAMESPACE",
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
        "application_sdk.workflows.metadata_extraction.lakehouse.ENABLE_LAKEHOUSE_LOAD",
        True,
    )
    @patch(
        "application_sdk.workflows.metadata_extraction.lakehouse.LH_LOAD_TRANSFORMED_NAMESPACE",
        "entity_metadata",
    )
    @patch("application_sdk.workflows.metadata_extraction.ENABLE_ATLAN_UPLOAD", False)
    @patch("temporalio.workflow.execute_activity_method")
    async def test_run_exit_activities_lakehouse_loads_any_typename(
        self,
        mock_execute_activity,
        workflow,
        workflow_args,
    ):
        """Test any typename is accepted — table name = typename.lower()."""
        mock_execute_activity.return_value = {"status": "success"}
        workflow_args["_extracted_typenames"] = ["table", "LookerDashboard"]

        await workflow.run_exit_activities(workflow_args)

        assert mock_execute_activity.call_count == 2
        lh_config_0 = mock_execute_activity.call_args_list[0][1]["args"][0][
            "lh_load_config"
        ]
        assert lh_config_0["table_name"] == "table"
        lh_config_1 = mock_execute_activity.call_args_list[1][1]["args"][0][
            "lh_load_config"
        ]
        assert lh_config_1["table_name"] == "lookerdashboard"

    @patch("application_sdk.workflows.metadata_extraction.ENABLE_LAKEHOUSE_LOAD", True)
    @patch(
        "application_sdk.workflows.metadata_extraction.lakehouse.ENABLE_LAKEHOUSE_LOAD",
        True,
    )
    @patch(
        "application_sdk.workflows.metadata_extraction.lakehouse.LH_LOAD_TRANSFORMED_NAMESPACE",
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


class TestLoadRawToLakehouse:
    """Test cases for LakehouseLoadMixin.load_raw_to_lakehouse."""

    @patch(
        "application_sdk.workflows.metadata_extraction.lakehouse.ENABLE_LAKEHOUSE_LOAD",
        True,
    )
    @patch(
        "application_sdk.workflows.metadata_extraction.lakehouse.LH_LOAD_RAW_NAMESPACE",
        "entity_raw",
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
        assert lh_config["namespace"] == "entity_raw"
        assert lh_config["table_name"] == "postgres"
        assert lh_config["mode"] == "APPEND"
        assert lh_config["file_extension"] == ".jsonl"

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
        "entity_raw",
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
