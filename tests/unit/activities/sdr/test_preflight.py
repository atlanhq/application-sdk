"""Tests for PreflightCheckActivities."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from application_sdk.activities.sdr.preflight import PreflightCheckActivities


class TestPreflightCheckActivities:
    @pytest.fixture
    def activities(self) -> PreflightCheckActivities:
        return PreflightCheckActivities(
            client_class=MagicMock(),  # type: ignore[arg-type]
            handler_class=MagicMock(),  # type: ignore[arg-type]
        )

    @pytest.mark.asyncio
    @patch("application_sdk.activities.sdr.preflight.create_handler")
    async def test_preflight_returns_check_results(
        self, mock_create_handler: AsyncMock, activities: PreflightCheckActivities
    ):
        expected = {
            "connectivity": {"success": True},
            "permissions": {"success": True},
        }
        mock_client = AsyncMock()
        mock_handler = AsyncMock()
        mock_handler.preflight_check.return_value = expected
        mock_create_handler.return_value = (mock_client, mock_handler)

        workflow_args = {"credentials": {"user": "test"}}
        result = await activities.preflight(workflow_args)

        mock_create_handler.assert_awaited_once_with(
            activities.client_class, activities.handler_class, workflow_args
        )
        mock_handler.preflight_check.assert_awaited_once_with(workflow_args)
        mock_client.close.assert_awaited_once()
        assert result == expected

    @pytest.mark.asyncio
    @patch("application_sdk.activities.sdr.preflight.create_handler")
    async def test_preflight_closes_client_on_exception(
        self, mock_create_handler: AsyncMock, activities: PreflightCheckActivities
    ):
        mock_client = AsyncMock()
        mock_handler = AsyncMock()
        mock_handler.preflight_check.side_effect = RuntimeError("check failed")
        mock_create_handler.return_value = (mock_client, mock_handler)

        with pytest.raises(RuntimeError, match="check failed"):
            await activities.preflight({"credentials": {"user": "test"}})

        mock_client.close.assert_awaited_once()
