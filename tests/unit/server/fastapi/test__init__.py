from typing import Any, Dict
from unittest.mock import AsyncMock, Mock

import pytest
from httpx import ASGITransport, AsyncClient
from hypothesis import HealthCheck, given, settings

from application_sdk.handlers import HandlerInterface
from application_sdk.server.fastapi import (
    APIServer,
    EventWorkflowTrigger,
    PreflightCheckRequest,
    PreflightCheckResponse,
)
from application_sdk.test_utils.hypothesis.strategies.server.fastapi import (
    payload_strategy,
)
from application_sdk.workflows import WorkflowInterface


class SampleWorkflow(WorkflowInterface):
    pass


class TestServer:
    @pytest.fixture(autouse=True)
    def setup_method(self):
        """Setup method that runs before each test method"""
        self.mock_handler = Mock(spec=HandlerInterface)
        self.mock_handler.preflight_check = AsyncMock()
        self.app = APIServer(handler=self.mock_handler)

    @pytest.mark.asyncio
    @given(payload=payload_strategy)
    @settings(suppress_health_check=[HealthCheck.function_scoped_fixture])
    async def test_preflight_check_success(
        self,
        payload: Dict[str, Any],
    ) -> None:
        """Test successful preflight check response with hypothesis generated payloads"""

        self.mock_handler.preflight_check.reset_mock()  # Resets call history for preflight_check so that assert_called_once_with works correctly ( since hypothesis will create multiple calls, one for each example)

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
        self.mock_handler.preflight_check.return_value = expected_data

        # Create request object and call the function
        request = PreflightCheckRequest(**payload)
        response = await self.app.preflight_check(request)

        # Assert
        assert isinstance(request, PreflightCheckRequest)
        assert isinstance(response, PreflightCheckResponse)
        assert response.success is True
        assert response.data == expected_data

        # Verify handler was called with correct arguments
        self.mock_handler.preflight_check.assert_called_once_with(payload)

    @pytest.mark.asyncio
    @given(payload=payload_strategy)
    @settings(suppress_health_check=[HealthCheck.function_scoped_fixture])
    async def test_preflight_check_failure(
        self,
        payload: Dict[str, Any],
    ) -> None:
        """Test preflight check with failed handler response using hypothesis generated payloads"""
        # Reset mock for each example
        self.mock_handler.preflight_check.reset_mock()

        # Arrange
        self.mock_handler.preflight_check.side_effect = Exception(
            "Failed to fetch metadata"
        )

        # Create request object
        request = PreflightCheckRequest(**payload)

        # Act & Assert
        with pytest.raises(Exception) as exc_info:
            await self.app.preflight_check(request)

        assert str(exc_info.value) == "Failed to fetch metadata"
        self.mock_handler.preflight_check.assert_called_once_with(payload)

    @pytest.mark.asyncio
    async def test_event_trigger_success(self):
        """Test event trigger with hypothesis generated event data"""
        event_data = {
            "data": {
                "event_type": "test_event_type",
                "event_name": "test_event_name",
                "data": {},
            },
            "datacontenttype": "application/json",
            "id": "some-id",
            "source": "test-source",
            "specversion": "1.0",
            "time": "2024-06-13T00:00:00Z",
            "type": "test_event_type",
            "topic": "test_topic",
        }

        temporal_client = AsyncMock()
        temporal_client.start_workflow = AsyncMock()

        self.app.workflow_client = temporal_client
        self.app.event_triggers = []

        self.app.register_workflow(
            SampleWorkflow,
            triggers=[
                EventWorkflowTrigger(
                    event_id="test_event_id",
                    event_type="test_event_type",
                    event_name="test_event_name",
                    event_filters=[],
                    workflow_class=SampleWorkflow,
                )
            ],
        )

        # Act
        # Use the FastAPI app for testing
        transport = ASGITransport(app=self.app.app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            response = await ac.post(
                "/events/v1/event/test_event_id",
                json=event_data,
            )

            assert response.status_code == 200

        # Assert
        temporal_client.start_workflow.assert_called_once()

    @pytest.mark.asyncio
    async def test_event_trigger_conditions(self):
        """Test event trigger conditions with hypothesis generated event data"""
        event_data = {
            "data": {
                "event_type": "test_event_type",
                "event_name": "test_event_name",
                "data": {},
            },
            "datacontenttype": "application/json",
            "id": "some-id",
            "source": "test-source",
            "specversion": "1.0",
            "time": "2024-06-13T00:00:00Z",
            "type": "test_event_type",
            "topic": "test_topic",
        }

        temporal_client = AsyncMock()
        temporal_client.start_workflow = AsyncMock()

        self.app.workflow_client = temporal_client
        self.app.event_triggers = []

        self.app.register_workflow(
            SampleWorkflow,
            triggers=[
                EventWorkflowTrigger(
                    event_id="test_event_id_invalid",
                    event_type="test_event_type",
                    event_name="test_event_name",
                    event_filters=[],
                    workflow_class=SampleWorkflow,
                )
            ],
        )

        # Act
        transport = ASGITransport(app=self.app.app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            response = await ac.post(
                "/events/v1/event/test_event_id",
                json=event_data,
            )

        # Assert
        assert response.status_code == 404
