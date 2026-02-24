"""Tests for PreflightCheckActivities."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from application_sdk.activities.sdr.preflight import PreflightCheckActivities


class TestPreflightCheckActivities:
    @pytest.fixture
    def activities(self):
        return PreflightCheckActivities(
            client_class=MagicMock(),
            handler_class=MagicMock(),
        )

    @pytest.mark.asyncio
    @patch("application_sdk.activities.sdr.preflight.create_handler")
    async def test_preflight_returns_check_results(
        self, mock_create_handler, activities
    ):
        expected = {
            "connectivity": {"success": True},
            "permissions": {"success": True},
        }
        mock_handler = AsyncMock()
        mock_handler.preflight_check.return_value = expected
        mock_create_handler.return_value = mock_handler

        workflow_args = {"credentials": {"user": "test"}}
        result = await activities.preflight(workflow_args)

        mock_create_handler.assert_awaited_once_with(
            activities.client_class, activities.handler_class, workflow_args
        )
        mock_handler.preflight_check.assert_awaited_once_with(workflow_args)
        assert result == expected

    @pytest.mark.asyncio
    @patch("application_sdk.activities.sdr.preflight.create_handler")
    async def test_preflight_propagates_exception(
        self, mock_create_handler, activities
    ):
        mock_handler = AsyncMock()
        mock_handler.preflight_check.side_effect = RuntimeError("check failed")
        mock_create_handler.return_value = mock_handler

        with pytest.raises(RuntimeError, match="check failed"):
            await activities.preflight({"credentials": {"user": "test"}})
