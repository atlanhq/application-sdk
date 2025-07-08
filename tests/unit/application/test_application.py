"""Unit tests for BaseApplication class."""

from unittest.mock import AsyncMock, Mock, patch

import pytest

from application_sdk.activities import ActivitiesInterface
from application_sdk.application import BaseApplication
from application_sdk.server import ServerInterface
from application_sdk.workflows import WorkflowInterface


class MockWorkflowInterface(WorkflowInterface):
    """Mock workflow interface for testing."""

    @staticmethod
    def get_activities(activities: ActivitiesInterface):
        return []


class MockActivitiesInterface(ActivitiesInterface):
    """Mock activities interface for testing."""

    pass


class MockServerInterface(ServerInterface):
    """Mock server interface for testing."""

    async def start(self):
        pass


class TestBaseApplication:
    """Test cases for BaseApplication class."""

    def test_initialization_basic(self):
        """Test basic application initialization."""
        app = BaseApplication("test-app")

        assert app.application_name == "test-app"
        assert app.server is None
        assert app.worker is None
        assert app.workflow_client is not None
        assert app.application_manifest is None
        assert app.event_subscriptions == {}

    def test_initialization_with_server(self):
        """Test application initialization with server."""
        mock_server = MockServerInterface()
        app = BaseApplication("test-app", server=mock_server)

        assert app.application_name == "test-app"
        assert app.server == mock_server

    def test_initialization_with_manifest(self):
        """Test application initialization with application manifest."""
        manifest = {"eventRegistration": {"consumes": []}}
        app = BaseApplication("test-app", application_manifest=manifest)

        assert app.application_manifest == manifest
        assert app.event_subscriptions == {}

    @patch("application_sdk.application.logger")
    def test_bootstrap_event_registration_no_manifest(self, mock_logger):
        """Test event registration bootstrap with no manifest."""
        app = BaseApplication("test-app")

        # Should not raise any exceptions
        assert app.event_subscriptions == {}
        mock_logger.warning.assert_called_once()

    @patch("application_sdk.application.logger")
    def test_bootstrap_event_registration_empty_consumes(self, mock_logger):
        """Test event registration bootstrap with empty consumes."""
        manifest = {"eventRegistration": {"consumes": []}}
        app = BaseApplication("test-app", application_manifest=manifest)

        assert app.event_subscriptions == {}
        mock_logger.warning.assert_called_once()

    def test_bootstrap_event_registration_with_consumes(self):
        """Test event registration bootstrap with valid consumes."""
        manifest = {
            "eventRegistration": {
                "consumes": [
                    {
                        "eventId": "test_id",
                        "eventType": "test_type",
                        "eventName": "test_name",
                        "version": "1.0",
                        "filters": [],
                    }
                ]
            }
        }
        app = BaseApplication("test-app", application_manifest=manifest)

        assert "test_id" in app.event_subscriptions
        event_trigger = app.event_subscriptions["test_id"]
        assert event_trigger.event_type == "test_type"
        assert event_trigger.event_name == "test_name"
        assert event_trigger.event_id == "test_id"

    def test_bootstrap_event_registration_duplicate_event_id(self):
        """Test event registration bootstrap with duplicate event ID."""
        manifest = {
            "eventRegistration": {
                "consumes": [
                    {
                        "eventId": "test_id",
                        "eventType": "test_type",
                        "eventName": "test_name",
                        "version": "1.0",
                        "filters": [],
                    },
                    {
                        "eventId": "test_id",  # Duplicate ID
                        "eventType": "test_type2",
                        "eventName": "test_name2",
                        "version": "1.0",
                        "filters": [],
                    },
                ]
            }
        }

        with pytest.raises(ValueError, match="Event test_id duplicate"):
            BaseApplication("test-app", application_manifest=manifest)

    def test_register_event_subscription_success(self):
        """Test successful event subscription registration."""
        manifest = {
            "eventRegistration": {
                "consumes": [
                    {
                        "eventId": "test_id",
                        "eventType": "test_type",
                        "eventName": "test_name",
                        "version": "1.0",
                        "filters": [],
                    }
                ]
            }
        }
        app = BaseApplication("test-app", application_manifest=manifest)

        app.register_event_subscription("test_id", MockWorkflowInterface)

        assert (
            app.event_subscriptions["test_id"].workflow_class == MockWorkflowInterface
        )

    def test_register_event_subscription_no_subscriptions(self):
        """Test event subscription registration when subscriptions not initialized."""
        app = BaseApplication("test-app")
        app.event_subscriptions = None

        with pytest.raises(ValueError, match="Event subscriptions not initialized"):
            app.register_event_subscription("test_id", MockWorkflowInterface)

    def test_register_event_subscription_invalid_event_id(self):
        """Test event subscription registration with invalid event ID."""
        manifest = {
            "eventRegistration": {
                "consumes": [
                    {
                        "eventId": "test_id",
                        "eventType": "test_type",
                        "eventName": "test_name",
                        "version": "1.0",
                        "filters": [],
                    }
                ]
            }
        }
        app = BaseApplication("test-app", application_manifest=manifest)

        with pytest.raises(ValueError, match="Event invalid_id not initialized"):
            app.register_event_subscription("invalid_id", MockWorkflowInterface)

    @patch("application_sdk.application.get_workflow_client")
    async def test_setup_workflow_success(self, mock_get_workflow_client):
        """Test successful workflow setup."""
        mock_workflow_client = AsyncMock()
        mock_get_workflow_client.return_value = mock_workflow_client

        app = BaseApplication("test-app")

        workflow_activities = [(MockWorkflowInterface, MockActivitiesInterface)]
        await app.setup_workflow(workflow_activities)

        assert app.worker is not None
        mock_workflow_client.load.assert_called_once()

    @patch("application_sdk.application.get_workflow_client")
    async def test_setup_workflow_with_passthrough_modules(
        self, mock_get_workflow_client
    ):
        """Test workflow setup with passthrough modules."""
        mock_workflow_client = AsyncMock()
        mock_get_workflow_client.return_value = mock_workflow_client

        app = BaseApplication("test-app")

        workflow_activities = [(MockWorkflowInterface, MockActivitiesInterface)]
        passthrough_modules = ["test_module"]
        await app.setup_workflow(
            workflow_activities, passthrough_modules=passthrough_modules
        )

        assert app.worker is not None

    @patch("application_sdk.application.get_workflow_client")
    async def test_start_workflow_success(self, mock_get_workflow_client):
        """Test successful workflow start."""
        mock_workflow_client = AsyncMock()
        mock_workflow_client.start_workflow.return_value = "workflow_result"
        mock_get_workflow_client.return_value = mock_workflow_client

        app = BaseApplication("test-app")

        workflow_args = {"test": "args"}
        result = await app.start_workflow(workflow_args, MockWorkflowInterface)

        assert result == "workflow_result"
        mock_workflow_client.start_workflow.assert_called_once_with(
            workflow_args, MockWorkflowInterface
        )

    @patch("application_sdk.application.get_workflow_client")
    async def test_start_workflow_no_client(self, mock_get_workflow_client):
        """Test workflow start when workflow client is None."""
        mock_get_workflow_client.return_value = None

        app = BaseApplication("test-app")
        app.workflow_client = None

        with pytest.raises(ValueError, match="Workflow client not initialized"):
            await app.start_workflow({}, MockWorkflowInterface)

    @patch("application_sdk.application.get_workflow_client")
    async def test_start_worker_success(self, mock_get_workflow_client):
        """Test successful worker start."""
        mock_workflow_client = AsyncMock()
        mock_get_workflow_client.return_value = mock_workflow_client

        app = BaseApplication("test-app")
        app.worker = AsyncMock()

        await app.start_worker(daemon=True)

        app.worker.start.assert_called_once_with(daemon=True)

    async def test_start_worker_no_worker(self):
        """Test worker start when worker is None."""
        app = BaseApplication("test-app")

        with pytest.raises(ValueError, match="Worker not initialized"):
            await app.start_worker()

    @patch("application_sdk.application.get_workflow_client")
    @patch("application_sdk.application.APIServer")
    async def test_setup_server_success(
        self, mock_api_server, mock_get_workflow_client
    ):
        """Test successful server setup."""
        mock_workflow_client = AsyncMock()
        mock_get_workflow_client.return_value = mock_workflow_client
        mock_server_instance = Mock()
        mock_api_server.return_value = mock_server_instance

        app = BaseApplication("test-app")

        await app.setup_server(MockWorkflowInterface)

        assert app.server == mock_server_instance
        mock_api_server.assert_called_once()
        mock_server_instance.register_workflow.assert_called_once()

    @patch("application_sdk.application.get_workflow_client")
    @patch("application_sdk.application.APIServer")
    async def test_setup_server_with_event_subscriptions(
        self, mock_api_server, mock_get_workflow_client
    ):
        """Test server setup with event subscriptions."""
        mock_workflow_client = AsyncMock()
        mock_get_workflow_client.return_value = mock_workflow_client
        mock_server_instance = Mock()
        mock_api_server.return_value = mock_server_instance

        manifest = {
            "eventRegistration": {
                "consumes": [
                    {
                        "eventId": "test_id",
                        "eventType": "test_type",
                        "eventName": "test_name",
                        "version": "1.0",
                        "filters": [],
                    }
                ]
            }
        }
        app = BaseApplication("test-app", application_manifest=manifest)
        app.register_event_subscription("test_id", MockWorkflowInterface)

        await app.setup_server(MockWorkflowInterface)

        # Should register both event trigger and HTTP trigger
        assert mock_server_instance.register_workflow.call_count == 2

    @patch("application_sdk.application.get_workflow_client")
    @patch("application_sdk.application.APIServer")
    async def test_setup_server_missing_workflow_class(
        self, mock_api_server, mock_get_workflow_client
    ):
        """Test server setup with missing workflow class for event trigger."""
        mock_workflow_client = AsyncMock()
        mock_get_workflow_client.return_value = mock_workflow_client
        mock_server_instance = Mock()
        mock_api_server.return_value = mock_server_instance

        manifest = {
            "eventRegistration": {
                "consumes": [
                    {
                        "eventId": "test_id",
                        "eventType": "test_type",
                        "eventName": "test_name",
                        "version": "1.0",
                        "filters": [],
                    }
                ]
            }
        }
        app = BaseApplication("test-app", application_manifest=manifest)
        # Don't register workflow class for event subscription

        with pytest.raises(
            ValueError, match="Workflow class not set for event trigger"
        ):
            await app.setup_server(MockWorkflowInterface)

    async def test_start_server_success(self):
        """Test successful server start."""
        app = BaseApplication("test-app")
        app.server = AsyncMock()

        await app.start_server()

        app.server.start.assert_called_once()

    async def test_start_server_no_server(self):
        """Test server start when server is None."""
        app = BaseApplication("test-app")

        with pytest.raises(ValueError, match="Application server not initialized"):
            await app.start_server()
