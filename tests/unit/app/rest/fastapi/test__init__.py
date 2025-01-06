import json
from typing import Any, Dict
from unittest.mock import AsyncMock, Mock

import pytest

from application_sdk.app.rest.fastapi import EventWorkflowTrigger, FastAPIApplication
from application_sdk.app.rest.fastapi.models.workflow import (
    PreflightCheckRequest,
    PreflightCheckResponse,
)
from application_sdk.paas.eventstore.models import AtlanEvent, WorkflowEndEvent
from application_sdk.workflows.builder import WorkflowBuilderInterface
from application_sdk.workflows.controllers import (
    WorkflowPreflightCheckControllerInterface,
)
from application_sdk.workflows.workflow import WorkflowInterface


class SampleWorkflow(AsyncMock):
    pass


class SampleWorkflowBuilder(WorkflowBuilderInterface):
    def build(self) -> WorkflowInterface:
        return SampleWorkflow()


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
    def sample_workflow(self) -> SampleWorkflow:
        return SampleWorkflow()

    @pytest.fixture
    def sample_workflow_builder(self) -> SampleWorkflowBuilder:
        return SampleWorkflowBuilder()

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

    async def test_event_trigger_success(
        self, app: FastAPIApplication, sample_workflow_builder: SampleWorkflowBuilder
    ):
        def should_trigger_workflow(event: AtlanEvent):
            if event.data.event_type == "workflow_end":
                return True
            return False

        sample_workflow = sample_workflow_builder.build()
        app.register_workflow(
            sample_workflow,
            triggers=[
                EventWorkflowTrigger(should_trigger_workflow=should_trigger_workflow)
            ],
        )

        # Act
        event_data = {
            "data": WorkflowEndEvent(
                workflow_id="test-workflow-123",
                workflow_name="test_workflow",
            ),
            "datacontenttype": "application/json",
            "id": "123e4567-e89b-12d3-a456-426614174000",
            "pubsubname": "pubsub",
            "source": "workflow-engine",
            "specversion": "1.0",
            "time": "2024-03-20T12:00:00Z",
            "topic": "workflow-events",
            "traceid": "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",
            "traceparent": "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",
            "tracestate": "",
            "type": "com.dapr.event.sent",
        }

        await app.on_event(event_data)

        # Assert
        sample_workflow.start.assert_called()

    async def test_event_trigger_conditions(
        self, app: FastAPIApplication, sample_workflow_builder: SampleWorkflowBuilder
    ):
        def trigger_workflow_on_start(event: AtlanEvent):
            if event.data.event_type == "workflow_start":
                return True
            return False

        def trigger_workflow_name(event: AtlanEvent):
            if event.data.workflow_name == "wrong_workflow":
                return True
            return False

        sample_workflow = sample_workflow_builder.build()
        app.register_workflow(
            sample_workflow,
            triggers=[
                EventWorkflowTrigger(should_trigger_workflow=trigger_workflow_on_start),
                EventWorkflowTrigger(should_trigger_workflow=trigger_workflow_name),
            ],
        )

        # Act
        event_data = {
            "data": WorkflowEndEvent(
                workflow_id="test-workflow-123",
                workflow_name="test_workflow",
            ),
            "datacontenttype": "application/json",
            "id": "123e4567-e89b-12d3-a456-426614174000",
            "pubsubname": "pubsub",
            "source": "workflow-engine",
            "specversion": "1.0",
            "time": "2024-03-20T12:00:00Z",
            "topic": "workflow-events",
            "traceid": "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",
            "traceparent": "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",
            "tracestate": "",
            "type": "com.dapr.event.sent",
        }

        await app.on_event(event_data)

        # Assert
        sample_workflow.start.assert_not_called()
