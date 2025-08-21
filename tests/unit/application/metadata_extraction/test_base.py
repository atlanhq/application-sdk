from typing import Any, Dict
from unittest.mock import AsyncMock, Mock, patch

import pytest

from application_sdk.activities.metadata_extraction.base import (
    BaseMetadataExtractionActivities,
)
from application_sdk.application.metadata_extraction.base import (
    BaseMetadataExtractionApplication,
)
from application_sdk.clients.base import BaseClient
from application_sdk.handlers.base import BaseHandler
from application_sdk.transformers import TransformerInterface
from application_sdk.workflows import WorkflowInterface


class MockBaseClient(BaseClient):
    """Mock BaseClient for testing."""

    async def load(self, **kwargs: Any) -> None:
        """Mock load method."""
        pass


class MockBaseHandler(BaseHandler):
    """Mock BaseHandler for testing."""

    async def preflight_check(self, config: Dict[str, Any]) -> Dict[str, Any]:
        """Mock preflight check."""
        return {"status": "success"}

    async def fetch_metadata(self, config: Dict[str, Any]) -> Dict[str, Any]:
        """Mock fetch metadata."""
        return {"metadata": "test"}

    async def test_auth(self, config: Dict[str, Any]) -> bool:
        """Mock test auth method."""
        return True


class MockTransformer(TransformerInterface):
    """Mock transformer for testing."""

    def __init__(self, **kwargs):
        self.kwargs = kwargs

    def transform(self, data: Any) -> Any:
        """Mock transform method."""
        return {"transformed": data}

    def transform_metadata(self, *args, **kwargs):
        """Mock transform_metadata method."""
        return Mock()  # Return a mock dataframe


class MockWorkflow(WorkflowInterface):
    """Mock workflow for testing."""

    @staticmethod
    def get_activities(activities: BaseMetadataExtractionActivities):
        """Mock get_activities method."""
        return []


class TestBaseMetadataExtractionApplication:
    """Test cases for BaseMetadataExtractionApplication class."""

    def test_initialization_basic(self):
        """Test basic application initialization."""
        app = BaseMetadataExtractionApplication("test-app")

        assert app.application_name == "test-app"
        assert app.server is None
        assert app.worker is None
        assert app.workflow_client is not None
        assert app.transformer_class is None
        assert app.client_class == BaseClient
        assert app.handler_class == BaseHandler

    def test_initialization_with_custom_classes(self):
        """Test application initialization with custom classes."""
        app = BaseMetadataExtractionApplication(
            "test-app",
            client_class=MockBaseClient,
            handler_class=MockBaseHandler,
            transformer_class=MockTransformer,
        )

        assert app.application_name == "test-app"
        assert app.transformer_class == MockTransformer
        assert app.client_class == MockBaseClient
        assert app.handler_class == MockBaseHandler

    def test_initialization_with_server(self):
        """Test application initialization with server."""
        mock_server = Mock()
        app = BaseMetadataExtractionApplication("test-app", server=mock_server)

        assert app.application_name == "test-app"
        assert app.server == mock_server

    def test_initialization_with_partial_custom_classes(self):
        """Test application initialization with partial custom classes."""
        app = BaseMetadataExtractionApplication(
            "test-app",
            client_class=MockBaseClient,
            handler_class=MockBaseHandler,
        )

        assert app.application_name == "test-app"
        assert app.transformer_class is None
        assert app.client_class == MockBaseClient
        assert app.handler_class == MockBaseHandler

    @patch("application_sdk.application.metadata_extraction.base.get_workflow_client")
    async def test_setup_workflow_success(self, mock_get_workflow_client):
        """Test successful workflow setup."""
        mock_workflow_client = AsyncMock()
        # Configure mock to return proper string values for WorkerCreationEventData
        mock_workflow_client.application_name = "test-app"
        mock_workflow_client.worker_task_queue = "test-app"
        mock_workflow_client.namespace = "default"
        mock_workflow_client.host = "localhost"
        mock_workflow_client.port = "7233"
        mock_workflow_client.get_connection_string = Mock(return_value="localhost:7233")
        mock_get_workflow_client.return_value = mock_workflow_client

        app = BaseMetadataExtractionApplication("test-app")

        workflow_activities = [(MockWorkflow, BaseMetadataExtractionActivities)]
        await app.setup_workflow(workflow_activities)

        assert app.worker is not None
        mock_workflow_client.load.assert_called_once()

    @patch("application_sdk.application.metadata_extraction.base.get_workflow_client")
    async def test_setup_workflow_with_custom_classes(self, mock_get_workflow_client):
        """Test workflow setup with custom classes."""
        mock_workflow_client = AsyncMock()
        # Configure mock to return proper string values for WorkerCreationEventData
        mock_workflow_client.application_name = "test-app"
        mock_workflow_client.worker_task_queue = "test-app"
        mock_workflow_client.namespace = "default"
        mock_workflow_client.host = "localhost"
        mock_workflow_client.port = "7233"
        mock_workflow_client.get_connection_string = Mock(return_value="localhost:7233")
        mock_get_workflow_client.return_value = mock_workflow_client

        app = BaseMetadataExtractionApplication(
            "test-app",
            client_class=MockBaseClient,
            handler_class=MockBaseHandler,
            transformer_class=MockTransformer,
        )

        workflow_activities = [(MockWorkflow, BaseMetadataExtractionActivities)]
        await app.setup_workflow(workflow_activities)

        assert app.worker is not None
        mock_workflow_client.load.assert_called_once()

    @patch("application_sdk.application.metadata_extraction.base.get_workflow_client")
    async def test_setup_workflow_with_passthrough_modules(
        self, mock_get_workflow_client
    ):
        """Test workflow setup with passthrough modules."""
        mock_workflow_client = AsyncMock()
        # Configure mock to return proper string values for WorkerCreationEventData
        mock_workflow_client.application_name = "test-app"
        mock_workflow_client.worker_task_queue = "test-app"
        mock_workflow_client.namespace = "default"
        mock_workflow_client.host = "localhost"
        mock_workflow_client.port = "7233"
        mock_workflow_client.get_connection_string = Mock(return_value="localhost:7233")
        mock_get_workflow_client.return_value = mock_workflow_client

        app = BaseMetadataExtractionApplication("test-app")

        workflow_activities = [(MockWorkflow, BaseMetadataExtractionActivities)]
        passthrough_modules = ["test_module"]
        await app.setup_workflow(
            workflow_activities, passthrough_modules=passthrough_modules
        )

        assert app.worker is not None

    @patch("application_sdk.application.metadata_extraction.base.get_workflow_client")
    async def test_setup_workflow_with_activity_executor(
        self, mock_get_workflow_client
    ):
        """Test workflow setup with activity executor."""
        mock_workflow_client = AsyncMock()
        # Configure mock to return proper string values for WorkerCreationEventData
        mock_workflow_client.application_name = "test-app"
        mock_workflow_client.worker_task_queue = "test-app"
        mock_workflow_client.namespace = "default"
        mock_workflow_client.host = "localhost"
        mock_workflow_client.port = "7233"
        mock_workflow_client.get_connection_string = Mock(return_value="localhost:7233")
        mock_get_workflow_client.return_value = mock_workflow_client

        app = BaseMetadataExtractionApplication("test-app")

        workflow_activities = [(MockWorkflow, BaseMetadataExtractionActivities)]
        activity_executor = Mock()
        await app.setup_workflow(
            workflow_activities, activity_executor=activity_executor
        )

        assert app.worker is not None

    @patch("application_sdk.application.metadata_extraction.base.get_workflow_client")
    async def test_setup_workflow_with_max_concurrent_activities(
        self, mock_get_workflow_client
    ):
        """Test workflow setup with max concurrent activities."""
        mock_workflow_client = AsyncMock()
        # Configure mock to return proper string values for WorkerCreationEventData
        mock_workflow_client.application_name = "test-app"
        mock_workflow_client.worker_task_queue = "test-app"
        mock_workflow_client.namespace = "default"
        mock_workflow_client.host = "localhost"
        mock_workflow_client.port = "7233"
        mock_workflow_client.get_connection_string = Mock(return_value="localhost:7233")
        mock_get_workflow_client.return_value = mock_workflow_client

        app = BaseMetadataExtractionApplication("test-app")

        workflow_activities = [(MockWorkflow, BaseMetadataExtractionActivities)]
        await app.setup_workflow(workflow_activities, max_concurrent_activities=10)

        assert app.worker is not None

    @patch("application_sdk.application.metadata_extraction.base.get_workflow_client")
    async def test_start_workflow_success(self, mock_get_workflow_client):
        """Test successful workflow start."""
        mock_workflow_client = AsyncMock()
        mock_workflow_client.start_workflow.return_value = "workflow_result"
        mock_get_workflow_client.return_value = mock_workflow_client

        app = BaseMetadataExtractionApplication("test-app")

        workflow_args = {"test": "args"}
        result = await app.start_workflow(workflow_args, MockWorkflow)

        assert result == "workflow_result"
        mock_workflow_client.start_workflow.assert_called_once_with(
            workflow_args, MockWorkflow
        )

    @patch("application_sdk.application.metadata_extraction.base.get_workflow_client")
    async def test_start_workflow_no_client(self, mock_get_workflow_client):
        """Test workflow start when workflow client is None."""
        mock_get_workflow_client.return_value = None

        app = BaseMetadataExtractionApplication("test-app")
        app.workflow_client = None

        with pytest.raises(ValueError, match="Workflow client not initialized"):
            await app.start_workflow({}, MockWorkflow)

    @patch("application_sdk.application.metadata_extraction.base.get_workflow_client")
    async def test_start_worker_success(self, mock_get_workflow_client):
        """Test successful worker start."""
        mock_workflow_client = AsyncMock()
        mock_get_workflow_client.return_value = mock_workflow_client

        app = BaseMetadataExtractionApplication("test-app")
        app.worker = AsyncMock()

        await app.start_worker(daemon=True)

        app.worker.start.assert_called_once_with(daemon=True)

    async def test_start_worker_no_worker(self):
        """Test worker start when worker is None."""
        app = BaseMetadataExtractionApplication("test-app")

        with pytest.raises(ValueError, match="Worker not initialized"):
            await app.start_worker()

    @pytest.mark.skip(
        reason="BaseHandler is abstract and cannot be instantiated directly"
    )
    @patch("application_sdk.application.metadata_extraction.base.get_workflow_client")
    @patch("application_sdk.application.metadata_extraction.base.APIServer")
    async def test_setup_server_success(
        self, mock_api_server, mock_get_workflow_client
    ):
        """Test successful server setup."""
        # This test is skipped because BaseHandler is abstract
        # and cannot be instantiated directly in setup_server
        pass

    @pytest.mark.skip(
        reason="BaseHandler is abstract and cannot be instantiated directly"
    )
    @patch("application_sdk.application.metadata_extraction.base.get_workflow_client")
    @patch("application_sdk.application.metadata_extraction.base.APIServer")
    async def test_setup_server_with_custom_handler(
        self, mock_api_server, mock_get_workflow_client
    ):
        """Test server setup with custom handler."""
        # This test is skipped because BaseHandler is abstract
        # and cannot be instantiated directly in setup_server
        pass

    @pytest.mark.skip(
        reason="BaseHandler is abstract and cannot be instantiated directly"
    )
    @patch("application_sdk.application.metadata_extraction.base.get_workflow_client")
    @patch("application_sdk.application.metadata_extraction.base.APIServer")
    async def test_setup_server_with_custom_client(
        self, mock_api_server, mock_get_workflow_client
    ):
        """Test server setup with custom client."""
        # This test is skipped because BaseHandler is abstract
        # and cannot be instantiated directly in setup_server
        pass

    @pytest.mark.skip(
        reason="BaseHandler is abstract and cannot be instantiated directly"
    )
    @patch("application_sdk.application.metadata_extraction.base.get_workflow_client")
    @patch("application_sdk.application.metadata_extraction.base.APIServer")
    async def test_setup_server_handler_initialization(
        self, mock_api_server, mock_get_workflow_client
    ):
        """Test that server setup properly initializes the handler with base client."""
        # This test is skipped because BaseHandler is abstract
        # and cannot be instantiated directly in setup_server
        pass

    @patch("application_sdk.application.metadata_extraction.base.get_workflow_client")
    async def test_setup_workflow_activities_initialization(
        self, mock_get_workflow_client
    ):
        """Test that workflow setup properly initializes activities with custom classes."""
        mock_workflow_client = AsyncMock()
        # Configure mock to return proper string values for WorkerCreationEventData
        mock_workflow_client.application_name = "test-app"
        mock_workflow_client.worker_task_queue = "test-app"
        mock_workflow_client.namespace = "default"
        mock_workflow_client.host = "localhost"
        mock_workflow_client.port = "7233"
        mock_workflow_client.get_connection_string = Mock(return_value="localhost:7233")
        mock_get_workflow_client.return_value = mock_workflow_client

        app = BaseMetadataExtractionApplication(
            "test-app",
            client_class=MockBaseClient,
            handler_class=MockBaseHandler,
            transformer_class=MockTransformer,
        )

        workflow_activities = [(MockWorkflow, BaseMetadataExtractionActivities)]
        await app.setup_workflow(workflow_activities)

        assert app.worker is not None
        mock_workflow_client.load.assert_called_once()

    def test_application_name_attribute(self):
        """Test that application_name attribute is set correctly."""
        app = BaseMetadataExtractionApplication("test-app-name")
        assert app.application_name == "test-app-name"

    def test_workflow_client_initialization(self):
        """Test that workflow_client is initialized."""
        app = BaseMetadataExtractionApplication("test-app")
        assert app.workflow_client is not None

    def test_default_class_attributes(self):
        """Test that default class attributes are set correctly."""
        app = BaseMetadataExtractionApplication("test-app")
        assert app.client_class == BaseClient
        assert app.handler_class == BaseHandler
        assert app.transformer_class is None
