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
        self.mock_handler.upload_file = AsyncMock()
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

    @pytest.mark.asyncio
    async def test_upload_file_success(self):
        """Test successful file upload endpoint."""
        # Arrange
        file_content = b"test file content"
        expected_metadata = {
            "id": "977f156b-9c78-4bfc-bd74-f603f18c078a",
            "version": "weathered-firefly-9025",
            "isActive": True,
            "createdAt": 1764265919324,
            "updatedAt": 1764265919324,
            "fileName": "28bb016e-329f-46e1-b817-3fd315bdd7f0.csv",
            "rawName": "ddls_export.csv",
            "key": "28bb016e-329f-46e1-b817-3fd315bdd7f0.csv",
            "extension": ".csv",
            "contentType": "text/csv",
            "fileSize": 39144,
            "isEncrypted": False,
            "redirectUrl": "",
            "isUploaded": True,
            "uploadedAt": "2024-01-01T00:00:00Z",
            "isArchived": False,
        }
        self.mock_handler.upload_file.return_value = expected_metadata

        # Act
        transport = ASGITransport(app=self.app.app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            files = {"file": ("test.csv", file_content, "text/csv")}
            data = {
                "prefix": "workflow_file_upload",
                "force": "false",
                # excludePrefix and isJsonFile are still accepted by endpoint for backward compatibility
                # but not passed to handler (heracles processing decisions)
            }
            response = await ac.post("/workflows/v1/file", files=files, data=data)

        # Assert
        assert response.status_code == 200
        response_data = response.json()
        assert response_data["id"] == expected_metadata["id"]
        assert response_data["key"] == expected_metadata["key"]
        assert response_data["fileName"] == expected_metadata["fileName"]
        self.mock_handler.upload_file.assert_called_once()
        call_kwargs = self.mock_handler.upload_file.call_args[1]
        assert call_kwargs["file_content"] == file_content
        assert call_kwargs["filename"] == "test.csv"
        assert call_kwargs["content_type"] == "text/csv"
        assert call_kwargs["size"] == len(
            file_content
        )  # Model uses 'size', not 'file_size'
        assert call_kwargs["prefix"] == "workflow_file_upload"
        # force is not used by handler (always false from widget), so not checked

    @pytest.mark.asyncio
    async def test_upload_file_with_json_file(self):
        """Test file upload with jsonFile field."""
        # Arrange
        file_content = b'{"test": "json"}'
        expected_metadata = {
            "id": "test-id",
            "version": "test-version",
            "isActive": True,
            "createdAt": 1764265919324,
            "updatedAt": 1764265919324,
            "fileName": "test-id.json",
            "rawName": "test.json",
            "key": "test-id.json",
            "extension": ".json",
            "contentType": "application/json",
            "fileSize": len(file_content),
            "isEncrypted": False,
            "redirectUrl": "",
            "isUploaded": True,
            "uploadedAt": "2024-01-01T00:00:00Z",
            "isArchived": False,
        }
        self.mock_handler.upload_file.return_value = expected_metadata

        # Act
        transport = ASGITransport(app=self.app.app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            files = {"jsonFile": ("test.json", file_content, "application/json")}
            data = {
                "prefix": "workflow_file_upload",
                "force": "false",
                # excludePrefix and isJsonFile are still accepted by endpoint for backward compatibility
                # but not passed to handler (heracles processing decisions)
            }
            response = await ac.post("/workflows/v1/file", files=files, data=data)

        # Assert
        assert response.status_code == 200
        self.mock_handler.upload_file.assert_called_once()
        call_kwargs = self.mock_handler.upload_file.call_args[1]
        assert call_kwargs["file_content"] == file_content
        assert call_kwargs["filename"] == "test.json"
        assert call_kwargs["prefix"] == "workflow_file_upload"
        # force is not used by handler (always false from widget), so not checked

    @pytest.mark.asyncio
    async def test_upload_file_no_handler(self):
        """Test file upload when handler is not initialized."""
        # Arrange
        app_no_handler = APIServer(handler=None)

        # Act
        transport = ASGITransport(app=app_no_handler.app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            files = {"file": ("test.csv", b"test content", "text/csv")}
            data = {
                "prefix": "workflow_file_upload",
                "force": "false",
                "excludePrefix": "false",
                "isJsonFile": "false",
            }
            response = await ac.post("/workflows/v1/file", files=files, data=data)

        # Assert
        assert response.status_code == 500
        assert "Handler not initialized" in response.json()["detail"]

    @pytest.mark.asyncio
    async def test_upload_file_missing_file(self):
        """Test file upload when file field is missing."""
        # Arrange
        # Act
        transport = ASGITransport(app=self.app.app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            data = {
                "prefix": "workflow_file_upload",
                "force": "false",
                "excludePrefix": "false",
                "isJsonFile": "false",
            }
            response = await ac.post("/workflows/v1/file", data=data)

        # Assert
        assert response.status_code == 422  # FastAPI validation error

    @pytest.mark.asyncio
    async def test_upload_file_handler_error(self):
        """Test file upload when handler raises an error."""
        # Arrange
        self.mock_handler.upload_file.side_effect = Exception("Upload failed")

        # Act
        transport = ASGITransport(app=self.app.app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            files = {"file": ("test.csv", b"test content", "text/csv")}
            data = {
                "prefix": "workflow_file_upload",
                "force": "false",
                "excludePrefix": "false",
                "isJsonFile": "false",
            }
            response = await ac.post("/workflows/v1/file", files=files, data=data)

        # Assert
        assert response.status_code == 500
        assert "File upload failed" in response.json()["detail"]

    @pytest.mark.asyncio
    async def test_upload_file_default_prefix(self):
        """Test file upload with default prefix when prefix is not provided."""
        # Arrange
        file_content = b"test file content"
        expected_metadata = {
            "id": "977f156b-9c78-4bfc-bd74-f603f18c078a",
            "version": "weathered-firefly-9025",
            "isActive": True,
            "createdAt": 1764265919324,
            "updatedAt": 1764265919324,
            "fileName": "28bb016e-329f-46e1-b817-3fd315bdd7f0.csv",
            "rawName": "ddls_export.csv",
            "key": "28bb016e-329f-46e1-b817-3fd315bdd7f0.csv",
            "extension": ".csv",
            "contentType": "text/csv",
            "fileSize": 39144,
            "isEncrypted": False,
            "redirectUrl": "",
            "isUploaded": True,
            "uploadedAt": "2024-01-01T00:00:00Z",
            "isArchived": False,
        }
        self.mock_handler.upload_file.return_value = expected_metadata

        # Act - no prefix provided, should default to "workflow_file_upload"
        transport = ASGITransport(app=self.app.app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            files = {"file": ("test.csv", file_content, "text/csv")}
            data = {
                "force": "false",
                "isJsonFile": "false",
            }
            response = await ac.post("/workflows/v1/file", files=files, data=data)

        # Assert
        assert response.status_code == 200
        self.mock_handler.upload_file.assert_called_once()
        call_kwargs = self.mock_handler.upload_file.call_args[1]
        assert call_kwargs["prefix"] == "workflow_file_upload"

    @pytest.mark.asyncio
    async def test_upload_file_with_custom_prefix(self):
        """Test file upload with custom prefix."""
        # Arrange
        file_content = b"test file content"
        expected_metadata = {
            "id": "977f156b-9c78-4bfc-bd74-f603f18c078a",
            "version": "weathered-firefly-9025",
            "isActive": True,
            "createdAt": 1764265919324,
            "updatedAt": 1764265919324,
            "fileName": "28bb016e-329f-46e1-b817-3fd315bdd7f0.csv",
            "rawName": "ddls_export.csv",
            "key": "28bb016e-329f-46e1-b817-3fd315bdd7f0.csv",
            "extension": ".csv",
            "contentType": "text/csv",
            "fileSize": 39144,
            "isEncrypted": False,
            "redirectUrl": "",
            "isUploaded": True,
            "uploadedAt": "2024-01-01T00:00:00Z",
            "isArchived": False,
        }
        self.mock_handler.upload_file.return_value = expected_metadata

        # Act - include custom prefix
        transport = ASGITransport(app=self.app.app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            files = {"file": ("test.csv", file_content, "text/csv")}
            data = {
                "prefix": "custom_prefix",
                "force": "false",
                "isJsonFile": "false",
            }
            response = await ac.post("/workflows/v1/file", files=files, data=data)

        # Assert
        assert response.status_code == 200
        self.mock_handler.upload_file.assert_called_once()
        call_kwargs = self.mock_handler.upload_file.call_args[1]
        # Only fields used by handler are checked
        assert call_kwargs["prefix"] == "custom_prefix"
        # name and force are not used by handler (force always false from widget)
