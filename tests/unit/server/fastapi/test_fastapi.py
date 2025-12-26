from typing import Any, Dict
from unittest.mock import AsyncMock, Mock, patch

import pytest
from httpx import ASGITransport, AsyncClient
from hypothesis import HealthCheck, given, settings
from pydantic import ValidationError

from application_sdk.handlers import HandlerInterface
from application_sdk.server.fastapi import (
    APIServer,
    EventWorkflowTrigger,
    PreflightCheckRequest,
    PreflightCheckResponse,
)
from application_sdk.server.fastapi.models import FileUploadResponse, Subscription
from application_sdk.test_utils.hypothesis.strategies.server.fastapi import (
    payload_strategy,
)
from application_sdk.workflows import WorkflowInterface


class SampleWorkflow(WorkflowInterface):
    pass


class TestSubscriptionModel:
    """Test suite for Subscription model validation."""

    def test_subscription_with_required_fields(self):
        """Test Subscription creation with all required fields."""

        def sync_handler(message: Dict[str, Any]) -> dict:
            return {"status": "processed"}

        subscription = Subscription(
            component_name="pubsub",
            topic="test-topic",
            route="test-route",
            handler=sync_handler,
        )

        assert subscription.component_name == "pubsub"
        assert subscription.topic == "test-topic"
        assert subscription.route == "test-route"
        assert subscription.handler == sync_handler
        assert subscription.bulk_config is None
        assert subscription.dead_letter_topic is None

    def test_subscription_with_async_handler(self):
        """Test Subscription creation with an async message handler."""

        async def async_handler(message: Dict[str, Any]) -> dict:
            return {"status": "processed"}

        subscription = Subscription(
            component_name="pubsub",
            topic="test-topic",
            route="test-route",
            handler=async_handler,
        )

        assert subscription.handler == async_handler

    def test_subscription_with_bulk_config(self):
        """Test Subscription creation with bulk configuration."""

        def handler(message: Dict[str, Any]) -> dict:
            return {"status": "processed"}

        bulk_config = Subscription.BulkConfig(
            enabled=True,
            max_messages_count=50,
            max_await_duration_ms=100,
        )

        subscription = Subscription(
            component_name="pubsub",
            topic="test-topic",
            route="test-route",
            handler=handler,
            bulk_config=bulk_config,
        )

        assert subscription.bulk_config is not None
        assert subscription.bulk_config.enabled is True
        assert subscription.bulk_config.max_messages_count == 50
        assert subscription.bulk_config.max_await_duration_ms == 100

    def test_subscription_with_dead_letter_topic(self):
        """Test Subscription creation with dead letter topic."""

        def handler(message: Dict[str, Any]) -> dict:
            return {"status": "processed"}

        subscription = Subscription(
            component_name="pubsub",
            topic="test-topic",
            route="test-route",
            handler=handler,
            dead_letter_topic="test-dlq",
        )

        assert subscription.dead_letter_topic == "test-dlq"

    def test_subscription_with_all_optional_fields(self):
        """Test Subscription creation with all optional fields configured."""

        def handler(message: Dict[str, Any]) -> dict:
            return {"status": "processed"}

        bulk_config = Subscription.BulkConfig(
            enabled=True,
            max_messages_count=200,
            max_await_duration_ms=50,
        )

        subscription = Subscription(
            component_name="my-pubsub",
            topic="orders",
            route="process-orders",
            handler=handler,
            bulk_config=bulk_config,
            dead_letter_topic="orders-dlq",
        )

        assert subscription.component_name == "my-pubsub"
        assert subscription.topic == "orders"
        assert subscription.route == "process-orders"
        assert subscription.bulk_config is not None
        assert subscription.bulk_config.enabled is True
        assert subscription.bulk_config.max_messages_count == 200
        assert subscription.dead_letter_topic == "orders-dlq"

    def test_subscription_missing_required_field(self):
        """Test Subscription validation fails when required fields are missing."""

        def handler(message: Dict[str, Any]) -> dict:
            return {"status": "processed"}

        with pytest.raises(ValidationError) as exc_info:
            Subscription(
                component_name="pubsub",
                topic="test-topic",
                # Missing route
                handler=handler,
            )

        assert "route" in str(exc_info.value)

    def test_subscription_bulk_config_defaults(self):
        """Test Subscription.BulkConfig model default values."""
        bulk_config = Subscription.BulkConfig()

        assert bulk_config.enabled is False
        assert bulk_config.max_messages_count == 100
        assert bulk_config.max_await_duration_ms == 40

    def test_subscription_bulk_config_with_custom_values(self):
        """Test Subscription.BulkConfig model with custom values."""
        bulk_config = Subscription.BulkConfig(
            enabled=True,
            max_messages_count=500,
            max_await_duration_ms=200,
        )

        assert bulk_config.enabled is True
        assert bulk_config.max_messages_count == 500
        assert bulk_config.max_await_duration_ms == 200


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


class TestMessagingRouterRegistration:
    """Test suite for messaging router registration with valid subscriptions."""

    @pytest.fixture(autouse=True)
    def setup_method(self):
        """Setup method that runs before each test method."""
        self.mock_handler = Mock(spec=HandlerInterface)
        self.mock_handler.preflight_check = AsyncMock()
        self.app = APIServer(handler=self.mock_handler)

    @pytest.mark.asyncio
    async def test_messaging_router_registration_with_sync_handler(self):
        """Test messaging router registration with a sync message handler."""

        def sync_message_handler(message: Dict[str, Any]) -> dict:
            return {"status": "processed", "data": message}

        subscription = Subscription(
            component_name="pubsub",
            topic="test-topic",
            route="process-message",
            handler=sync_message_handler,
        )

        self.app.subscriptions = [subscription]
        self.app.register_routers()

        # Verify the route is registered
        transport = ASGITransport(app=self.app.app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            response = await ac.post(
                "/subscriptions/v1/process-message",
                json={"test": "data"},
            )

            assert response.status_code == 200
            response_data = response.json()
            assert response_data["status"] == "processed"

    @pytest.mark.asyncio
    async def test_messaging_router_registration_with_async_handler(self):
        """Test messaging router registration with an async message handler."""

        async def async_message_handler(message: Dict[str, Any]) -> dict:
            return {"status": "async_processed", "data": message}

        subscription = Subscription(
            component_name="pubsub",
            topic="test-topic",
            route="async-process",
            handler=async_message_handler,
        )

        self.app.subscriptions = [subscription]
        self.app.register_routers()

        transport = ASGITransport(app=self.app.app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            response = await ac.post(
                "/subscriptions/v1/async-process",
                json={"test": "async_data"},
            )

            assert response.status_code == 200
            response_data = response.json()
            assert response_data["status"] == "async_processed"

    @pytest.mark.asyncio
    async def test_messaging_router_registration_with_multiple_subscriptions(self):
        """Test messaging router registration with multiple subscriptions."""

        def handler_one(message: Dict[str, Any]) -> dict:
            return {"handler": "one"}

        def handler_two(message: Dict[str, Any]) -> dict:
            return {"handler": "two"}

        subscriptions = [
            Subscription(
                component_name="pubsub",
                topic="topic-one",
                route="route-one",
                handler=handler_one,
            ),
            Subscription(
                component_name="pubsub",
                topic="topic-two",
                route="route-two",
                handler=handler_two,
            ),
        ]

        self.app.subscriptions = subscriptions
        self.app.register_routers()

        transport = ASGITransport(app=self.app.app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            # Test first route
            response_one = await ac.post(
                "/subscriptions/v1/route-one",
                json={},
            )
            assert response_one.status_code == 200
            assert response_one.json()["handler"] == "one"

            # Test second route
            response_two = await ac.post(
                "/subscriptions/v1/route-two",
                json={},
            )
            assert response_two.status_code == 200
            assert response_two.json()["handler"] == "two"

    @pytest.mark.asyncio
    async def test_messaging_router_not_registered_when_no_subscriptions(self):
        """Test that no messaging routes are registered when subscriptions list is empty."""
        self.app.subscriptions = []
        self.app.register_routers()

        transport = ASGITransport(app=self.app.app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            response = await ac.post(
                "/subscriptions/v1/some-route",
                json={},
            )

            # Should return 404 since route is not registered
            assert response.status_code == 404


class TestDaprSubscriptionEndpointGeneration:
    """Test suite for Dapr subscription endpoint generation."""

    @pytest.fixture(autouse=True)
    def setup_method(self):
        """Setup method that runs before each test method."""
        self.mock_handler = Mock(spec=HandlerInterface)
        self.mock_handler.preflight_check = AsyncMock()
        self.app = APIServer(handler=self.mock_handler)

    @pytest.mark.asyncio
    async def test_dapr_subscriptions_with_basic_subscription(self):
        """Test Dapr subscription generation with basic subscription configuration."""

        def handler(message: Dict[str, Any]) -> dict:
            return {"status": "ok"}

        subscription = Subscription(
            component_name="my-pubsub",
            topic="my-topic",
            route="handle-message",
            handler=handler,
        )

        self.app.subscriptions = [subscription]
        self.app.event_triggers = []

        subscriptions = await self.app.get_dapr_subscriptions()

        assert len(subscriptions) == 1
        assert subscriptions[0]["pubsubname"] == "my-pubsub"
        assert subscriptions[0]["topic"] == "my-topic"
        assert subscriptions[0]["route"] == "/subscriptions/v1/handle-message"
        assert "bulkSubscribe" not in subscriptions[0]
        assert "deadLetterTopic" not in subscriptions[0]

    @pytest.mark.asyncio
    async def test_dapr_subscriptions_with_bulk_subscribe_enabled(self):
        """Test Dapr subscription generation with bulk subscribe enabled."""

        def handler(message: Dict[str, Any]) -> dict:
            return {"status": "ok"}

        bulk_config = Subscription.BulkConfig(
            enabled=True,
            max_messages_count=250,
            max_await_duration_ms=75,
        )

        subscription = Subscription(
            component_name="pubsub",
            topic="bulk-topic",
            route="bulk-handler",
            handler=handler,
            bulk_config=bulk_config,
        )

        self.app.subscriptions = [subscription]
        self.app.event_triggers = []

        subscriptions = await self.app.get_dapr_subscriptions()

        assert len(subscriptions) == 1
        assert "bulkSubscribe" in subscriptions[0]
        assert subscriptions[0]["bulkSubscribe"]["enabled"] is True
        assert subscriptions[0]["bulkSubscribe"]["maxMessagesCount"] == 250
        assert subscriptions[0]["bulkSubscribe"]["maxAwaitDurationMs"] == 75

    @pytest.mark.asyncio
    async def test_dapr_subscriptions_with_bulk_subscribe_disabled(self):
        """Test Dapr subscription generation with bulk subscribe disabled."""

        def handler(message: Dict[str, Any]) -> dict:
            return {"status": "ok"}

        bulk_config = Subscription.BulkConfig(
            enabled=False,
            max_messages_count=100,
            max_await_duration_ms=40,
        )

        subscription = Subscription(
            component_name="pubsub",
            topic="topic",
            route="handler",
            handler=handler,
            bulk_config=bulk_config,
        )

        self.app.subscriptions = [subscription]
        self.app.event_triggers = []

        subscriptions = await self.app.get_dapr_subscriptions()

        assert len(subscriptions) == 1
        # bulkSubscribe should be included even when enabled=False
        assert "bulkSubscribe" in subscriptions[0]
        assert subscriptions[0]["bulkSubscribe"]["enabled"] is False
        assert subscriptions[0]["bulkSubscribe"]["maxMessagesCount"] == 100
        assert subscriptions[0]["bulkSubscribe"]["maxAwaitDurationMs"] == 40

    @pytest.mark.asyncio
    async def test_dapr_subscriptions_with_dead_letter_topic(self):
        """Test Dapr subscription generation with dead letter topic configured."""

        def handler(message: Dict[str, Any]) -> dict:
            return {"status": "ok"}

        subscription = Subscription(
            component_name="pubsub",
            topic="main-topic",
            route="main-handler",
            handler=handler,
            dead_letter_topic="main-topic-dlq",
        )

        self.app.subscriptions = [subscription]
        self.app.event_triggers = []

        subscriptions = await self.app.get_dapr_subscriptions()

        assert len(subscriptions) == 1
        assert "deadLetterTopic" in subscriptions[0]
        assert subscriptions[0]["deadLetterTopic"] == "main-topic-dlq"

    @pytest.mark.asyncio
    async def test_dapr_subscriptions_with_all_options(self):
        """Test Dapr subscription generation with all options configured."""

        def handler(message: Dict[str, Any]) -> dict:
            return {"status": "ok"}

        bulk_config = Subscription.BulkConfig(
            enabled=True,
            max_messages_count=500,
            max_await_duration_ms=150,
        )

        subscription = Subscription(
            component_name="kafka-pubsub",
            topic="orders-topic",
            route="process-orders",
            handler=handler,
            bulk_config=bulk_config,
            dead_letter_topic="orders-dlq",
        )

        self.app.subscriptions = [subscription]
        self.app.event_triggers = []

        subscriptions = await self.app.get_dapr_subscriptions()

        assert len(subscriptions) == 1
        sub = subscriptions[0]
        assert sub["pubsubname"] == "kafka-pubsub"
        assert sub["topic"] == "orders-topic"
        assert sub["route"] == "/subscriptions/v1/process-orders"
        assert sub["bulkSubscribe"]["enabled"] is True
        assert sub["bulkSubscribe"]["maxMessagesCount"] == 500
        assert sub["bulkSubscribe"]["maxAwaitDurationMs"] == 150
        assert sub["deadLetterTopic"] == "orders-dlq"

    @pytest.mark.asyncio
    async def test_dapr_subscriptions_with_multiple_subscriptions(self):
        """Test Dapr subscription generation with multiple subscriptions."""

        def handler_one(message: Dict[str, Any]) -> dict:
            return {"status": "one"}

        def handler_two(message: Dict[str, Any]) -> dict:
            return {"status": "two"}

        subscriptions_list = [
            Subscription(
                component_name="pubsub-a",
                topic="topic-a",
                route="handler-a",
                handler=handler_one,
            ),
            Subscription(
                component_name="pubsub-b",
                topic="topic-b",
                route="handler-b",
                handler=handler_two,
                dead_letter_topic="topic-b-dlq",
            ),
        ]

        self.app.subscriptions = subscriptions_list
        self.app.event_triggers = []

        subscriptions = await self.app.get_dapr_subscriptions()

        assert len(subscriptions) == 2

        # First subscription
        assert subscriptions[0]["pubsubname"] == "pubsub-a"
        assert subscriptions[0]["topic"] == "topic-a"
        assert subscriptions[0]["route"] == "/subscriptions/v1/handler-a"

        # Second subscription
        assert subscriptions[1]["pubsubname"] == "pubsub-b"
        assert subscriptions[1]["topic"] == "topic-b"
        assert subscriptions[1]["route"] == "/subscriptions/v1/handler-b"
        assert subscriptions[1]["deadLetterTopic"] == "topic-b-dlq"

    @pytest.mark.asyncio
    async def test_dapr_subscriptions_endpoint_via_http(self):
        """Test the /dapr/subscribe endpoint returns correct subscription config."""

        def handler(message: Dict[str, Any]) -> dict:
            return {"status": "ok"}

        subscription = Subscription(
            component_name="test-pubsub",
            topic="test-topic",
            route="test-handler",
            handler=handler,
            bulk_config=Subscription.BulkConfig(enabled=True, max_messages_count=100),
        )

        self.app.subscriptions = [subscription]
        self.app.event_triggers = []
        self.app.register_routers()

        transport = ASGITransport(app=self.app.app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            response = await ac.get("/dapr/subscribe")

            assert response.status_code == 200
            subscriptions = response.json()
            assert len(subscriptions) == 1
            assert subscriptions[0]["pubsubname"] == "test-pubsub"
            assert subscriptions[0]["topic"] == "test-topic"
            assert subscriptions[0]["bulkSubscribe"]["enabled"] is True


class TestMessageHandlerCallbackInvocation:
    """Test suite for message handler callback invocation."""

    @pytest.fixture(autouse=True)
    def setup_method(self):
        """Setup method that runs before each test method."""
        self.mock_handler = Mock(spec=HandlerInterface)
        self.mock_handler.preflight_check = AsyncMock()
        self.app = APIServer(handler=self.mock_handler)

    @pytest.mark.asyncio
    async def test_sync_handler_receives_correct_data(self):
        """Test that sync handler receives the correct request data."""
        received_data = []

        def sync_handler(message: Dict[str, Any]) -> dict:
            received_data.append(message)
            return {"received": True}

        subscription = Subscription(
            component_name="pubsub",
            topic="test-topic",
            route="sync-route",
            handler=sync_handler,
        )

        self.app.subscriptions = [subscription]
        self.app.register_routers()

        test_payload = {"message": "hello", "value": 123}

        transport = ASGITransport(app=self.app.app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            response = await ac.post(
                "/subscriptions/v1/sync-route",
                json=test_payload,
            )

            assert response.status_code == 200
            assert response.json()["received"] is True
            assert len(received_data) == 1
            assert received_data[0]["message"] == "hello"
            assert received_data[0]["value"] == 123

    @pytest.mark.asyncio
    async def test_async_handler_receives_correct_data(self):
        """Test that async handler receives the correct request data."""
        received_data = []

        async def async_handler(message: Dict[str, Any]) -> dict:
            received_data.append(message)
            return {"async_received": True}

        subscription = Subscription(
            component_name="pubsub",
            topic="test-topic",
            route="async-route",
            handler=async_handler,
        )

        self.app.subscriptions = [subscription]
        self.app.register_routers()

        test_payload = {"message": "async_hello", "value": 456}

        transport = ASGITransport(app=self.app.app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            response = await ac.post(
                "/subscriptions/v1/async-route",
                json=test_payload,
            )

            assert response.status_code == 200
            assert response.json()["async_received"] is True
            assert len(received_data) == 1
            assert received_data[0]["message"] == "async_hello"
            assert received_data[0]["value"] == 456

    @pytest.mark.asyncio
    async def test_handler_with_call_tracking(self):
        """Test handler invocation with call tracking to verify invocation."""
        call_count = [0]

        def tracked_handler(message: Dict[str, Any]) -> dict:
            call_count[0] += 1
            return {"call_count": call_count[0]}

        subscription = Subscription(
            component_name="pubsub",
            topic="test-topic",
            route="tracked-route",
            handler=tracked_handler,
        )

        self.app.subscriptions = [subscription]
        self.app.register_routers()

        transport = ASGITransport(app=self.app.app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            # First call
            response1 = await ac.post(
                "/subscriptions/v1/tracked-route",
                json={"test": "data"},
            )
            assert response1.status_code == 200
            assert call_count[0] == 1

            # Second call
            response2 = await ac.post(
                "/subscriptions/v1/tracked-route",
                json={"test": "more_data"},
            )
            assert response2.status_code == 200
            assert call_count[0] == 2

    @pytest.mark.asyncio
    async def test_handler_error_propagation(self):
        """Test that handler errors are properly propagated."""

        def error_handler(message: Dict[str, Any]) -> dict:
            raise ValueError("Handler error occurred")

        subscription = Subscription(
            component_name="pubsub",
            topic="test-topic",
            route="error-route",
            handler=error_handler,
        )

        self.app.subscriptions = [subscription]
        self.app.register_routers()

        transport = ASGITransport(app=self.app.app, raise_app_exceptions=False)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            response = await ac.post(
                "/subscriptions/v1/error-route",
                json={},
            )

            # FastAPI returns 500 for unhandled exceptions
            assert response.status_code == 500

    @pytest.mark.asyncio
    async def test_async_handler_error_propagation(self):
        """Test that async handler errors are properly propagated."""

        async def async_error_handler(message: Dict[str, Any]) -> dict:
            raise RuntimeError("Async handler error occurred")

        subscription = Subscription(
            component_name="pubsub",
            topic="test-topic",
            route="async-error-route",
            handler=async_error_handler,
        )

        self.app.subscriptions = [subscription]
        self.app.register_routers()

        transport = ASGITransport(app=self.app.app, raise_app_exceptions=False)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            response = await ac.post(
                "/subscriptions/v1/async-error-route",
                json={},
            )

            # FastAPI returns 500 for unhandled exceptions
            assert response.status_code == 500

    @pytest.mark.asyncio
    async def test_handler_returns_custom_response(self):
        """Test that handler can return custom response data."""

        def custom_response_handler(message: Dict[str, Any]) -> dict:
            return {
                "status": "SUCCESS",
                "processed_at": "2024-01-01T00:00:00Z",
                "items_count": 42,
                "metadata": {"source": "test"},
            }

        subscription = Subscription(
            component_name="pubsub",
            topic="test-topic",
            route="custom-response-route",
            handler=custom_response_handler,
        )

        self.app.subscriptions = [subscription]
        self.app.register_routers()

        transport = ASGITransport(app=self.app.app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            response = await ac.post(
                "/subscriptions/v1/custom-response-route",
                json={},
            )

            assert response.status_code == 200
            response_data = response.json()
            assert response_data["status"] == "SUCCESS"
            assert response_data["items_count"] == 42
            assert response_data["metadata"]["source"] == "test"
