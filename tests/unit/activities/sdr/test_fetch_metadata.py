"""Tests for FetchMetadataActivities."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from application_sdk.activities.sdr.fetch_metadata import FetchMetadataActivities


class TestFetchMetadataActivities:
    @pytest.fixture
    def activities(self):
        return FetchMetadataActivities(
            client_class=MagicMock(),
            handler_class=MagicMock(),
        )

    @pytest.mark.asyncio
    @patch("application_sdk.activities.sdr.fetch_metadata.create_handler")
    async def test_fetch_metadata_returns_items(self, mock_create_handler, activities):
        expected = [
            {"name": "db1", "type": "database"},
            {"name": "schema1", "type": "schema"},
        ]
        mock_handler = AsyncMock()
        mock_handler.fetch_metadata.return_value = expected
        mock_create_handler.return_value = mock_handler

        workflow_args = {"credentials": {"user": "test"}}
        result = await activities.fetch_metadata(workflow_args)

        mock_create_handler.assert_awaited_once_with(
            activities.client_class, activities.handler_class, workflow_args
        )
        mock_handler.fetch_metadata.assert_awaited_once()
        assert result == expected

    @pytest.mark.asyncio
    @patch("application_sdk.activities.sdr.fetch_metadata.create_handler")
    async def test_fetch_metadata_propagates_exception(
        self, mock_create_handler, activities
    ):
        mock_handler = AsyncMock()
        mock_handler.fetch_metadata.side_effect = TimeoutError("timed out")
        mock_create_handler.return_value = mock_handler

        with pytest.raises(TimeoutError, match="timed out"):
            await activities.fetch_metadata({"credentials": {"user": "test"}})
