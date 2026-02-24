"""Tests for TestAuthActivities."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from application_sdk.activities.sdr.test_auth import TestAuthActivities


class TestTestAuthActivities:
    @pytest.fixture
    def activities(self):
        return TestAuthActivities(
            client_class=MagicMock(),
            handler_class=MagicMock(),
        )

    @pytest.mark.asyncio
    @patch("application_sdk.activities.sdr.test_auth.create_handler")
    async def test_test_auth_returns_true_on_success(
        self, mock_create_handler, activities
    ):
        mock_handler = AsyncMock()
        mock_handler.test_auth.return_value = True
        mock_create_handler.return_value = mock_handler

        workflow_args = {"credentials": {"user": "test"}}
        result = await activities.test_auth(workflow_args)

        mock_create_handler.assert_awaited_once_with(
            activities.client_class, activities.handler_class, workflow_args
        )
        mock_handler.test_auth.assert_awaited_once()
        assert result is True

    @pytest.mark.asyncio
    @patch("application_sdk.activities.sdr.test_auth.create_handler")
    async def test_test_auth_propagates_exception(
        self, mock_create_handler, activities
    ):
        mock_handler = AsyncMock()
        mock_handler.test_auth.side_effect = ConnectionError("auth failed")
        mock_create_handler.return_value = mock_handler

        with pytest.raises(ConnectionError, match="auth failed"):
            await activities.test_auth({"credentials": {"user": "test"}})
