"""Unit tests for BaseSQLMetadataExtractionApplication class."""

from typing import Any, Dict
from unittest.mock import AsyncMock, Mock, patch

import pytest

from application_sdk.application.metadata_extraction.sql import (
    BaseSQLMetadataExtractionApplication,
)
from application_sdk.clients.sql import BaseSQLClient
from application_sdk.handlers.sql import BaseSQLHandler
from application_sdk.transformers.query import QueryBasedTransformer
from application_sdk.workflows.metadata_extraction.sql import (
    BaseSQLMetadataExtractionActivities,
    BaseSQLMetadataExtractionWorkflow,
)


class MockSQLClient(BaseSQLClient):
    """Mock SQL client for testing."""

    async def load(self, credentials: Dict[str, Any]) -> None:
        pass

    async def close(self) -> None:
        pass


class MockSQLHandler(BaseSQLHandler):
    """Mock SQL handler for testing."""

    def __init__(self, sql_client=None):
        self.sql_client = sql_client

    async def preflight_check(self, workflow_args: Dict[str, Any]) -> bool:
        return True


class MockQueryBasedTransformer(QueryBasedTransformer):
    """Mock query-based transformer for testing."""

    def transform_metadata(self, dataframe, **kwargs):
        return {"transformed": "data"}


class MockSQLMetadataExtractionWorkflow(BaseSQLMetadataExtractionWorkflow):
    """Mock SQL metadata extraction workflow for testing."""

    @staticmethod
    def get_activities(activities: BaseSQLMetadataExtractionActivities):
        return []


class TestBaseSQLMetadataExtractionApplication:
    """Test cases for BaseSQLMetadataExtractionApplication class."""

    def test_initialization_basic(self):
        """Test basic SQL metadata extraction application initialization."""
        app = BaseSQLMetadataExtractionApplication("test-app")

        assert app.application_name == "test-app"
        assert app.server is None
        assert app.worker is None
        assert app.workflow_client is not None
        assert app.transformer_class == QueryBasedTransformer
        assert app.client_class == BaseSQLClient
        assert app.handler_class == BaseSQLHandler

    def test_initialization_with_custom_classes(self):
        """Test SQL metadata extraction application initialization with custom classes."""
        app = BaseSQLMetadataExtractionApplication(
            "test-app",
            client_class=MockSQLClient,
            handler_class=MockSQLHandler,
            transformer_class=MockQueryBasedTransformer,
        )

        assert app.application_name == "test-app"
        assert app.transformer_class == MockQueryBasedTransformer
        assert app.client_class == MockSQLClient
        assert app.handler_class == MockSQLHandler

    def test_initialization_with_server(self):
        """Test SQL metadata extraction application initialization with server."""
        mock_server = Mock()
        app = BaseSQLMetadataExtractionApplication("test-app", server=mock_server)

        assert app.application_name == "test-app"
        assert app.server == mock_server

    @patch("application_sdk.application.metadata_extraction.sql.get_workflow_client")
    async def test_setup_workflow_success(self, mock_get_workflow_client):
        """Test successful SQL metadata extraction workflow setup."""
        mock_workflow_client = AsyncMock()
        mock_get_workflow_client.return_value = mock_workflow_client

        app = BaseSQLMetadataExtractionApplication("test-app")

        workflow_activities = [
            (MockSQLMetadataExtractionWorkflow, BaseSQLMetadataExtractionActivities)
        ]
        await app.setup_workflow(workflow_activities)

        assert app.worker is not None
        mock_workflow_client.load.assert_called_once()

    @patch("application_sdk.application.metadata_extraction.sql.get_workflow_client")
    async def test_setup_workflow_with_custom_classes(self, mock_get_workflow_client):
        """Test SQL metadata extraction workflow setup with custom classes."""
        mock_workflow_client = AsyncMock()
        mock_get_workflow_client.return_value = mock_workflow_client

        app = BaseSQLMetadataExtractionApplication(
            "test-app",
            client_class=MockSQLClient,
            handler_class=MockSQLHandler,
            transformer_class=MockQueryBasedTransformer,
        )

        workflow_activities = [
            (MockSQLMetadataExtractionWorkflow, BaseSQLMetadataExtractionActivities)
        ]
        await app.setup_workflow(workflow_activities)

        assert app.worker is not None
        mock_workflow_client.load.assert_called_once()

    @patch("application_sdk.application.metadata_extraction.sql.get_workflow_client")
    async def test_setup_workflow_with_passthrough_modules(
        self, mock_get_workflow_client
    ):
        """Test SQL metadata extraction workflow setup with passthrough modules."""
        mock_workflow_client = AsyncMock()
        mock_get_workflow_client.return_value = mock_workflow_client

        app = BaseSQLMetadataExtractionApplication("test-app")

        workflow_activities = [
            (MockSQLMetadataExtractionWorkflow, BaseSQLMetadataExtractionActivities)
        ]
        passthrough_modules = ["test_module"]
        await app.setup_workflow(
            workflow_activities, passthrough_modules=passthrough_modules
        )

        assert app.worker is not None

    @patch("application_sdk.application.metadata_extraction.sql.get_workflow_client")
    async def test_setup_workflow_with_activity_executor(
        self, mock_get_workflow_client
    ):
        """Test SQL metadata extraction workflow setup with activity executor."""
        mock_workflow_client = AsyncMock()
        mock_get_workflow_client.return_value = mock_workflow_client

        app = BaseSQLMetadataExtractionApplication("test-app")

        workflow_activities = [
            (MockSQLMetadataExtractionWorkflow, BaseSQLMetadataExtractionActivities)
        ]
        activity_executor = Mock()
        await app.setup_workflow(
            workflow_activities, activity_executor=activity_executor
        )

        assert app.worker is not None

    @patch("application_sdk.application.metadata_extraction.sql.get_workflow_client")
    async def test_setup_workflow_with_max_concurrent_activities(
        self, mock_get_workflow_client
    ):
        """Test SQL metadata extraction workflow setup with max concurrent activities."""
        mock_workflow_client = AsyncMock()
        mock_get_workflow_client.return_value = mock_workflow_client

        app = BaseSQLMetadataExtractionApplication("test-app")

        workflow_activities = [
            (MockSQLMetadataExtractionWorkflow, BaseSQLMetadataExtractionActivities)
        ]
        await app.setup_workflow(workflow_activities, max_concurrent_activities=10)

        assert app.worker is not None

    @patch("application_sdk.application.metadata_extraction.sql.get_workflow_client")
    async def test_start_workflow_success(self, mock_get_workflow_client):
        """Test successful SQL metadata extraction workflow start."""
        mock_workflow_client = AsyncMock()
        mock_workflow_client.start_workflow.return_value = "workflow_result"
        mock_get_workflow_client.return_value = mock_workflow_client

        app = BaseSQLMetadataExtractionApplication("test-app")

        workflow_args = {"test": "args"}
        result = await app.start_workflow(
            workflow_args, MockSQLMetadataExtractionWorkflow
        )

        assert result == "workflow_result"
        mock_workflow_client.start_workflow.assert_called_once_with(
            workflow_args, MockSQLMetadataExtractionWorkflow
        )

    @patch("application_sdk.application.metadata_extraction.sql.get_workflow_client")
    async def test_start_workflow_default_class(self, mock_get_workflow_client):
        """Test SQL metadata extraction workflow start with default workflow class."""
        mock_workflow_client = AsyncMock()
        mock_workflow_client.start_workflow.return_value = "workflow_result"
        mock_get_workflow_client.return_value = mock_workflow_client

        app = BaseSQLMetadataExtractionApplication("test-app")

        workflow_args = {"test": "args"}
        result = await app.start_workflow(workflow_args)

        assert result == "workflow_result"
        mock_workflow_client.start_workflow.assert_called_once_with(
            workflow_args, BaseSQLMetadataExtractionWorkflow
        )

    @patch("application_sdk.application.metadata_extraction.sql.get_workflow_client")
    async def test_start_workflow_no_client(self, mock_get_workflow_client):
        """Test SQL metadata extraction workflow start when workflow client is None."""
        mock_get_workflow_client.return_value = None

        app = BaseSQLMetadataExtractionApplication("test-app")
        app.workflow_client = None

        with pytest.raises(ValueError, match="Workflow client not initialized"):
            await app.start_workflow({}, MockSQLMetadataExtractionWorkflow)

    @patch("application_sdk.application.metadata_extraction.sql.get_workflow_client")
    async def test_start_worker_success(self, mock_get_workflow_client):
        """Test successful SQL metadata extraction worker start."""
        mock_workflow_client = AsyncMock()
        mock_get_workflow_client.return_value = mock_workflow_client

        app = BaseSQLMetadataExtractionApplication("test-app")
        app.worker = AsyncMock()

        await app.start_worker(daemon=True)

        app.worker.start.assert_called_once_with(daemon=True)

    async def test_start_worker_no_worker(self):
        """Test SQL metadata extraction worker start when worker is None."""
        app = BaseSQLMetadataExtractionApplication("test-app")

        with pytest.raises(ValueError, match="Worker not initialized"):
            await app.start_worker()

    @patch("application_sdk.application.metadata_extraction.sql.get_workflow_client")
    @patch("application_sdk.application.metadata_extraction.sql.APIServer")
    async def test_setup_server_success(
        self, mock_api_server, mock_get_workflow_client
    ):
        """Test successful SQL metadata extraction server setup."""
        mock_workflow_client = AsyncMock()
        mock_get_workflow_client.return_value = mock_workflow_client
        mock_server_instance = Mock()
        mock_api_server.return_value = mock_server_instance

        app = BaseSQLMetadataExtractionApplication("test-app")

        await app.setup_server(MockSQLMetadataExtractionWorkflow)

        assert app.server == mock_server_instance
        mock_api_server.assert_called_once()
        mock_server_instance.register_workflow.assert_called_once()

    @patch("application_sdk.application.metadata_extraction.sql.get_workflow_client")
    @patch("application_sdk.application.metadata_extraction.sql.APIServer")
    async def test_setup_server_default_class(
        self, mock_api_server, mock_get_workflow_client
    ):
        """Test SQL metadata extraction server setup with default workflow class."""
        mock_workflow_client = AsyncMock()
        mock_get_workflow_client.return_value = mock_workflow_client
        mock_server_instance = Mock()
        mock_api_server.return_value = mock_server_instance

        app = BaseSQLMetadataExtractionApplication("test-app")

        await app.setup_server()

        assert app.server == mock_server_instance
        mock_api_server.assert_called_once()
        mock_server_instance.register_workflow.assert_called_once()

    @patch("application_sdk.application.metadata_extraction.sql.get_workflow_client")
    @patch("application_sdk.application.metadata_extraction.sql.APIServer")
    async def test_setup_server_with_custom_handler(
        self, mock_api_server, mock_get_workflow_client
    ):
        """Test SQL metadata extraction server setup with custom handler."""
        mock_workflow_client = AsyncMock()
        mock_get_workflow_client.return_value = mock_workflow_client
        mock_server_instance = Mock()
        mock_api_server.return_value = mock_server_instance

        app = BaseSQLMetadataExtractionApplication(
            "test-app", handler_class=MockSQLHandler
        )

        await app.setup_server(MockSQLMetadataExtractionWorkflow)

        assert app.server == mock_server_instance
        mock_api_server.assert_called_once()
        mock_server_instance.register_workflow.assert_called_once()

    @patch("application_sdk.application.metadata_extraction.sql.get_workflow_client")
    @patch("application_sdk.application.metadata_extraction.sql.APIServer")
    async def test_setup_server_with_custom_client(
        self, mock_api_server, mock_get_workflow_client
    ):
        """Test SQL metadata extraction server setup with custom client."""
        mock_workflow_client = AsyncMock()
        mock_get_workflow_client.return_value = mock_workflow_client
        mock_server_instance = Mock()
        mock_api_server.return_value = mock_server_instance

        app = BaseSQLMetadataExtractionApplication(
            "test-app", client_class=MockSQLClient
        )

        await app.setup_server(MockSQLMetadataExtractionWorkflow)

        assert app.server == mock_server_instance
        mock_api_server.assert_called_once()
        mock_server_instance.register_workflow.assert_called_once()

    @patch("application_sdk.application.metadata_extraction.sql.get_workflow_client")
    @patch("application_sdk.application.metadata_extraction.sql.APIServer")
    async def test_setup_server_handler_initialization(
        self, mock_api_server, mock_get_workflow_client
    ):
        """Test that server setup properly initializes the handler with SQL client."""
        mock_workflow_client = AsyncMock()
        mock_get_workflow_client.return_value = mock_workflow_client
        mock_server_instance = Mock()
        mock_api_server.return_value = mock_server_instance

        app = BaseSQLMetadataExtractionApplication(
            "test-app", client_class=MockSQLClient, handler_class=MockSQLHandler
        )

        await app.setup_server(MockSQLMetadataExtractionWorkflow)

        # Verify APIServer was called with handler that has SQL client
        mock_api_server.assert_called_once()
        call_args = mock_api_server.call_args
        handler = call_args[1]["handler"]  # Get handler from kwargs
        assert isinstance(handler, MockSQLHandler)
        assert isinstance(handler.sql_client, MockSQLClient)
