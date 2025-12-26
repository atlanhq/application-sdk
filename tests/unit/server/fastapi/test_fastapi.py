from typing import Any, Dict
from unittest.mock import AsyncMock, Mock, patch

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
from application_sdk.server.fastapi.models import FileUploadResponse
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

    @pytest.mark.asyncio
    @patch("application_sdk.server.fastapi.upload_file_to_object_store")
    async def test_upload_file_success(self, mock_upload_file):
        """Test successful file upload endpoint."""
        # Arrange
        file_content = b"test file content"
        expected_response = FileUploadResponse(
            id="977f156b-9c78-4bfc-bd74-f603f18c078a",
            version="weathered-firefly-9025",
            isActive=True,
            createdAt=1764265919324,
            updatedAt=1764265919324,
            fileName="28bb016e-329f-46e1-b817-3fd315bdd7f0.csv",
            rawName="ddls_export.csv",
            key="28bb016e-329f-46e1-b817-3fd315bdd7f0.csv",
            extension=".csv",
            contentType="text/csv",
            fileSize=39144,
            isEncrypted=False,
            redirectUrl="",
            isUploaded=True,
            uploadedAt="2024-01-01T00:00:00Z",
            isArchived=False,
        )
        mock_upload_file.return_value = expected_response

        # Act
        transport = ASGITransport(app=self.app.app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            files = {"file": ("test.csv", file_content, "text/csv")}
            data = {
                "filename": "test.csv",  # Explicitly provide filename
                "prefix": "workflow_file_upload",
            }
            response = await ac.post("/workflows/v1/file", files=files, data=data)

        # Assert
        assert response.status_code == 200
        response_data = response.json()
        assert response_data["id"] == expected_response.id
        assert response_data["key"] == expected_response.key
        assert response_data["fileName"] == expected_response.fileName
        mock_upload_file.assert_called_once()
        call_kwargs = mock_upload_file.call_args[1]
        # Verify UploadFile object is passed
        from fastapi import UploadFile

        assert isinstance(call_kwargs["file"], UploadFile)
        assert call_kwargs["file"].filename == "test.csv"
        assert call_kwargs["filename"] == "test.csv"  # Should use provided filename
        assert call_kwargs["prefix"] == "workflow_file_upload"

    @pytest.mark.asyncio
    @patch("application_sdk.server.fastapi.upload_file_to_object_store")
    async def test_upload_file_with_json_file(self, mock_upload_file):
        """Test file upload with jsonFile field."""
        # Arrange
        file_content = b'{"test": "json"}'
        expected_response = FileUploadResponse(
            id="test-id",
            version="test-version",
            isActive=True,
            createdAt=1764265919324,
            updatedAt=1764265919324,
            fileName="test-id.json",
            rawName="test.json",
            key="test-id.json",
            extension=".json",
            contentType="application/json",
            fileSize=len(file_content),
            isEncrypted=False,
            redirectUrl="",
            isUploaded=True,
            uploadedAt="2024-01-01T00:00:00Z",
            isArchived=False,
        )
        mock_upload_file.return_value = expected_response

        # Act
        transport = ASGITransport(app=self.app.app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            files = {"file": ("test.json", file_content, "application/json")}
            data = {
                "prefix": "workflow_file_upload",
            }
            response = await ac.post("/workflows/v1/file", files=files, data=data)

        # Assert
        assert response.status_code == 200
        mock_upload_file.assert_called_once()
        call_kwargs = mock_upload_file.call_args[1]
        # Verify UploadFile object is passed
        from fastapi import UploadFile

        assert isinstance(call_kwargs["file"], UploadFile)
        assert call_kwargs["file"].filename == "test.json"
        assert call_kwargs["prefix"] == "workflow_file_upload"

    @pytest.mark.asyncio
    @patch("application_sdk.server.fastapi.upload_file_to_object_store")
    async def test_upload_file_no_handler(self, mock_upload_file):
        """Test file upload works even when handler is not initialized."""
        # Arrange
        file_content = b"test content"
        expected_response = FileUploadResponse(
            id="test-id",
            version="test-version",
            isActive=True,
            createdAt=1764265919324,
            updatedAt=1764265919324,
            fileName="test-id.csv",
            rawName="test.csv",
            key="test-id.csv",
            extension=".csv",
            contentType="text/csv",
            fileSize=len(file_content),
            isEncrypted=False,
            redirectUrl="",
            isUploaded=True,
            uploadedAt="2024-01-01T00:00:00Z",
            isArchived=False,
        )
        mock_upload_file.return_value = expected_response
        app_no_handler = APIServer(handler=None)

        # Act
        transport = ASGITransport(app=app_no_handler.app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            files = {"file": ("test.csv", file_content, "text/csv")}
            data = {
                "prefix": "workflow_file_upload",
            }
            response = await ac.post("/workflows/v1/file", files=files, data=data)

        # Assert - file upload no longer requires handler
        assert response.status_code == 200
        mock_upload_file.assert_called_once()

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
    @patch("application_sdk.server.fastapi.upload_file_to_object_store")
    async def test_upload_file_utility_error(self, mock_upload_file):
        """Test file upload when utility function raises an error."""
        # Arrange
        mock_upload_file.side_effect = Exception("Upload failed")

        # Act
        transport = ASGITransport(app=self.app.app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            file_content = b"test content"
            files = {"file": ("test.csv", file_content, "text/csv")}
            data = {
                "prefix": "workflow_file_upload",
            }
            response = await ac.post("/workflows/v1/file", files=files, data=data)

        # Assert
        assert response.status_code == 500
        assert "File upload failed" in response.json()["detail"]

    @pytest.mark.asyncio
    @patch("application_sdk.server.fastapi.upload_file_to_object_store")
    async def test_upload_file_default_prefix(self, mock_upload_file):
        """Test file upload with default prefix when prefix is not provided."""
        # Arrange
        file_content = b"test file content"
        expected_response = FileUploadResponse(
            id="977f156b-9c78-4bfc-bd74-f603f18c078a",
            version="weathered-firefly-9025",
            isActive=True,
            createdAt=1764265919324,
            updatedAt=1764265919324,
            fileName="28bb016e-329f-46e1-b817-3fd315bdd7f0.csv",
            rawName="ddls_export.csv",
            key="28bb016e-329f-46e1-b817-3fd315bdd7f0.csv",
            extension=".csv",
            contentType="text/csv",
            fileSize=39144,
            isEncrypted=False,
            redirectUrl="",
            isUploaded=True,
            uploadedAt="2024-01-01T00:00:00Z",
            isArchived=False,
        )
        mock_upload_file.return_value = expected_response

        # Act - no prefix provided, should default to "workflow_file_upload"
        transport = ASGITransport(app=self.app.app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            files = {"file": ("test.csv", file_content, "text/csv")}
            data = {}
            response = await ac.post("/workflows/v1/file", files=files, data=data)

        # Assert
        assert response.status_code == 200
        mock_upload_file.assert_called_once()
        call_kwargs = mock_upload_file.call_args[1]
        assert call_kwargs["prefix"] == "workflow_file_upload"

    @pytest.mark.asyncio
    @patch("application_sdk.server.fastapi.upload_file_to_object_store")
    async def test_upload_file_with_custom_prefix(self, mock_upload_file):
        """Test file upload with custom prefix."""
        # Arrange
        file_content = b"test file content"
        expected_response = FileUploadResponse(
            id="977f156b-9c78-4bfc-bd74-f603f18c078a",
            version="weathered-firefly-9025",
            isActive=True,
            createdAt=1764265919324,
            updatedAt=1764265919324,
            fileName="28bb016e-329f-46e1-b817-3fd315bdd7f0.csv",
            rawName="ddls_export.csv",
            key="28bb016e-329f-46e1-b817-3fd315bdd7f0.csv",
            extension=".csv",
            contentType="text/csv",
            fileSize=39144,
            isEncrypted=False,
            redirectUrl="",
            isUploaded=True,
            uploadedAt="2024-01-01T00:00:00Z",
            isArchived=False,
        )
        mock_upload_file.return_value = expected_response

        # Act - include custom prefix
        transport = ASGITransport(app=self.app.app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            files = {"file": ("test.csv", file_content, "text/csv")}
            data = {
                "prefix": "custom_prefix",
            }
            response = await ac.post("/workflows/v1/file", files=files, data=data)

        # Assert
        assert response.status_code == 200
        mock_upload_file.assert_called_once()
        call_kwargs = mock_upload_file.call_args[1]
        # Verify UploadFile object is passed
        from fastapi import UploadFile

        assert isinstance(call_kwargs["file"], UploadFile)
        assert call_kwargs["filename"] is None  # Not provided in form data
        assert call_kwargs["prefix"] == "custom_prefix"
