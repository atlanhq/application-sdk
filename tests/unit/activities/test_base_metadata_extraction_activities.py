"""Unit tests for BaseMetadataExtractionActivities upload_to_atlan."""

from unittest.mock import patch

import pytest

from application_sdk.activities.common.models import ActivityStatistics
from application_sdk.activities.metadata_extraction.base import (
    BaseMetadataExtractionActivities,
)
from application_sdk.common.error_codes import ActivityError
from application_sdk.services.atlan_storage import MigrationSummary


@pytest.fixture
def activities():
    """Create a BaseMetadataExtractionActivities instance."""
    return BaseMetadataExtractionActivities()


@pytest.fixture
def workflow_args():
    """Sample workflow arguments with output_path."""
    return {
        "output_path": "/tmp/test-workflow/output",
        "workflow_id": "test-workflow-123",
    }


class TestUploadToAtlan:
    """Test cases for upload_to_atlan activity."""

    @patch(
        "application_sdk.activities.metadata_extraction.base.AtlanStorage.migrate_from_objectstore_to_atlan"
    )
    async def test_upload_to_atlan_success(
        self,
        mock_migrate,
        activities,
        workflow_args,
    ):
        """Test successful upload to Atlan."""
        mock_migrate.return_value = MigrationSummary(
            total_files=10,
            migrated_files=10,
            failed_migrations=0,
            failures=[],
            prefix="test-workflow/output",
        )

        result = await activities.upload_to_atlan(workflow_args)

        assert isinstance(result, ActivityStatistics)
        assert result.total_record_count == 10
        assert result.chunk_count == 10
        assert result.typename == "atlan-upload-completed"

        mock_migrate.assert_called_once_with(prefix=workflow_args["output_path"])

    @patch(
        "application_sdk.activities.metadata_extraction.base.AtlanStorage.migrate_from_objectstore_to_atlan"
    )
    async def test_upload_to_atlan_with_failures(
        self,
        mock_migrate,
        activities,
        workflow_args,
    ):
        """Test upload to Atlan with some failures raises ActivityError."""
        mock_migrate.return_value = MigrationSummary(
            total_files=10,
            migrated_files=8,
            failed_migrations=2,
            failures=[
                {"file": "file1.json", "error": "Connection timeout"},
                {"file": "file2.json", "error": "Permission denied"},
            ],
            prefix="test-workflow/output",
        )

        with pytest.raises(ActivityError) as exc_info:
            await activities.upload_to_atlan(workflow_args)

        assert "Atlan upload failed with 2 errors" in str(exc_info.value)
        assert "Failed migrations: 2" in str(exc_info.value)

    @patch(
        "application_sdk.activities.metadata_extraction.base.AtlanStorage.migrate_from_objectstore_to_atlan"
    )
    async def test_upload_to_atlan_no_files(
        self,
        mock_migrate,
        activities,
        workflow_args,
    ):
        """Test upload to Atlan with no files to migrate."""
        mock_migrate.return_value = MigrationSummary(
            total_files=0,
            migrated_files=0,
            failed_migrations=0,
            failures=[],
            prefix="test-workflow/output",
        )

        result = await activities.upload_to_atlan(workflow_args)

        assert isinstance(result, ActivityStatistics)
        assert result.total_record_count == 0
        assert result.chunk_count == 0
        assert result.typename == "atlan-upload-completed"
