import json
from typing import Any, Dict
from unittest.mock import AsyncMock, Mock

import pytest

from application_sdk.app.rest.fastapi import FastAPIApplication
from application_sdk.app.rest.fastapi.models.workflow import (
    PreflightCheckRequest,
    PreflightCheckResponse,
)
from application_sdk.workflows.controllers import (
    WorkflowPreflightCheckControllerInterface,
)


class TestFastAPIApplication:
    @pytest.fixture
    def mock_controller(self) -> WorkflowPreflightCheckControllerInterface:
        controller = Mock(spec=WorkflowPreflightCheckControllerInterface)
        controller.preflight_check = AsyncMock()
        return controller

    @pytest.fixture
    def app(
        self, mock_controller: WorkflowPreflightCheckControllerInterface
    ) -> FastAPIApplication:
        return FastAPIApplication(preflight_check_controller=mock_controller)

    @pytest.fixture
    def sample_payload(self) -> Dict[str, Any]:
        return {
            "credentials": {
                "account_id": "qdgrryr-uv65759",
                "port": 443,
                "role": "ACCOUNTADMIN",
                "warehouse": "COMPUTE_WH",
            },
            "form_data": {
                "include_filter": json.dumps({"^TESTDB$": ["^PUBLIC$"]}),
                "exclude_filter": "{}",
                "temp_table_regex": "",
            },
        }

    @pytest.mark.asyncio
    async def test_preflight_check_success(
        self,
        app: FastAPIApplication,
        mock_controller: WorkflowPreflightCheckControllerInterface,
        sample_payload: Dict[str, Any],
    ) -> None:
        """Test successful preflight check response"""
        # Arrange
        expected_data: Dict[str, Any] = {
            "example": {
                "success": True,
                "data": {
                    "successMessage": "Successfully checked",
                    "failureMessage": "",
                },
            }
        }
        mock_controller.preflight_check.return_value = expected_data

        # Create request object and call the function
        request = PreflightCheckRequest(**sample_payload)
        response = await app.preflight_check(request)

        # Assert
        assert isinstance(request, PreflightCheckRequest)
        assert isinstance(response, PreflightCheckResponse)
        assert response.success is True
        assert response.data == expected_data

        # Verify controller was called with correct arguments
        mock_controller.preflight_check.assert_called_once_with(sample_payload)

    @pytest.mark.asyncio
    async def test_preflight_check_failure(
        self,
        app: FastAPIApplication,
        mock_controller: WorkflowPreflightCheckControllerInterface,
        sample_payload: Dict[str, Any],
    ) -> None:
        """Test preflight check with failed controller response"""
        # Arrange
        mock_controller.preflight_check.side_effect = Exception(
            "Failed to fetch metadata"
        )

        # Create request object
        request = PreflightCheckRequest(**sample_payload)

        # Act & Assert
        with pytest.raises(Exception) as exc_info:
            await app.preflight_check(request)

        assert str(exc_info.value) == "Failed to fetch metadata"
        mock_controller.preflight_check.assert_called_once_with(sample_payload)
