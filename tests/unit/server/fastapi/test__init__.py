from typing import Any, Dict
from unittest.mock import AsyncMock, Mock

import pytest
from hypothesis import HealthCheck, given, settings

from application_sdk.handlers import HandlerInterface
from application_sdk.outputs.eventstore import AtlanEvent, WorkflowEndEvent
from application_sdk.server.fastapi import (
    APIServer,
    EventWorkflowTrigger,
    PreflightCheckRequest,
    PreflightCheckResponse,
)
from application_sdk.test_utils.hypothesis.strategies.server.fastapi import (
    event_data_strategy,
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
    @given(event_data=event_data_strategy)
    @settings(suppress_health_check=[HealthCheck.function_scoped_fixture])
    async def test_event_trigger_success(self, event_data: Dict[str, Any]):
        """Test event trigger with hypothesis generated event data"""

        def should_trigger_workflow(event: AtlanEvent):
            if event.data.event_type == "workflow_end":
                return True
            return False

        temporal_client = AsyncMock()
        temporal_client.start_workflow = AsyncMock()

        self.app.workflow_client = temporal_client
        self.app.event_triggers = []

        self.app.register_workflow(
            SampleWorkflow,
            triggers=[
                EventWorkflowTrigger(
                    should_trigger_workflow=should_trigger_workflow,
                    workflow_class=SampleWorkflow,
                )
            ],
        )

        # Act
        await self.app.on_event(event_data)

        # Assert
        temporal_client.start_workflow.assert_called_once_with(
            workflow_args=event_data,
            workflow_class=SampleWorkflow,
        )

    @pytest.mark.asyncio
    @given(event_data=event_data_strategy)
    @settings(suppress_health_check=[HealthCheck.function_scoped_fixture])
    async def test_event_trigger_conditions(self, event_data: Dict[str, Any]):
        """Test event trigger conditions with hypothesis generated event data"""
        temporal_client = AsyncMock()
        temporal_client.start_workflow = AsyncMock()

        self.app.event_triggers = []
        self.app.workflow_client = temporal_client

        def trigger_workflow_on_start(event: AtlanEvent):
            if event.data.event_type == "workflow_start":
                return True
            return False

        def trigger_workflow_name(event: AtlanEvent):
            if isinstance(event.data, WorkflowEndEvent):
                return event.data.workflow_name == "test_workflow"
            return False

        self.app.register_workflow(
            SampleWorkflow,
            triggers=[
                EventWorkflowTrigger(
                    should_trigger_workflow=trigger_workflow_on_start,
                    workflow_class=SampleWorkflow,
                ),
                EventWorkflowTrigger(
                    should_trigger_workflow=trigger_workflow_name,
                    workflow_class=SampleWorkflow,
                ),
            ],
        )

        # Act
        await self.app.on_event(event_data)

        # Assert
        assert temporal_client.start_workflow.call_count <= 1
        if temporal_client.start_workflow.called:
            temporal_client.start_workflow.assert_called_with(
                workflow_args=event_data,
                workflow_class=SampleWorkflow,
            )
