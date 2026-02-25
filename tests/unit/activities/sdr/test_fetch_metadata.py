"""Tests for FetchMetadataActivities."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from application_sdk.activities.sdr.fetch_metadata import FetchMetadataActivities


class TestFetchMetadataActivities:
    @pytest.fixture
    def activities(self) -> FetchMetadataActivities:
        return FetchMetadataActivities(
            client_class=MagicMock(),  # type: ignore[arg-type]
            handler_class=MagicMock(),  # type: ignore[arg-type]
        )

    @pytest.mark.asyncio
    @patch("application_sdk.activities.sdr.fetch_metadata.create_handler")
    async def test_fetch_metadata_passes_handler_relevant_keys(
        self, mock_create_handler: AsyncMock, activities: FetchMetadataActivities
    ):
        expected = [
            {"name": "db1", "type": "database"},
            {"name": "schema1", "type": "schema"},
        ]
        mock_client = AsyncMock()
        mock_handler = AsyncMock()
        mock_handler.fetch_metadata.return_value = expected
        mock_create_handler.return_value = (mock_client, mock_handler)

        workflow_args = {
            "credentials": {"user": "test"},
            "metadata_type": "all",
            "database": "mydb",
            "workflow_id": "wf-123",
            "output_path": "/tmp/out",
        }
        result = await activities.fetch_metadata(workflow_args)

        mock_create_handler.assert_awaited_once_with(
            activities.client_class, activities.handler_class, workflow_args
        )
        # Only metadata_type and database are forwarded, not infrastructure
        # keys like workflow_id or output_path that would cause a TypeError
        # in handlers with strict signatures (e.g. BaseSQLHandler).
        mock_handler.fetch_metadata.assert_awaited_once_with(
            metadata_type="all", database="mydb"
        )
        mock_client.close.assert_awaited_once()
        assert result == expected

    @pytest.mark.asyncio
    @patch("application_sdk.activities.sdr.fetch_metadata.create_handler")
    async def test_fetch_metadata_closes_client_on_exception(
        self, mock_create_handler: AsyncMock, activities: FetchMetadataActivities
    ):
        mock_client = AsyncMock()
        mock_handler = AsyncMock()
        mock_handler.fetch_metadata.side_effect = TimeoutError("timed out")
        mock_create_handler.return_value = (mock_client, mock_handler)

        with pytest.raises(TimeoutError, match="timed out"):
            await activities.fetch_metadata({"credentials": {"user": "test"}})

        mock_client.close.assert_awaited_once()
