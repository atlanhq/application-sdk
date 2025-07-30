"""Unit tests for the eventstore module."""

from unittest.mock import AsyncMock, Mock, patch

import pytest

from application_sdk.outputs.eventstore import (
    HTTP_BINDING_NAME,
    ApplicationEventNames,
    Event,
    EventMetadata,
    EventStore,
    EventTypes,
    WorkflowStates,
)


class TestEvent:
    """Test cases for the Event class."""

    def test_event_creation(self):
        """Test basic event creation."""
        event = Event(
            event_type="test_event", event_name="test_name", data={"key": "value"}
        )

        assert event.event_type == "test_event"
        assert event.event_name == "test_name"
        assert event.data == {"key": "value"}
        assert event.get_topic_name() == "test_event_topic"

    def test_event_with_metadata(self):
        """Test event creation with metadata."""
        metadata = EventMetadata(
            application_name="test_app",
            workflow_type="test_workflow",
            workflow_id="test_id",
        )

        event = Event(
            event_type="test_event",
            event_name="test_name",
            data={"key": "value"},
            metadata=metadata,
        )

        assert event.metadata.application_name == "test_app"
        assert event.metadata.workflow_type == "test_workflow"
        assert event.metadata.workflow_id == "test_id"

    def test_event_model_dump(self):
        """Test event serialization."""
        event = Event(
            event_type="test_event", event_name="test_name", data={"key": "value"}
        )

        event_dict = event.model_dump(mode="json")
        assert event_dict["event_type"] == "test_event"
        assert event_dict["event_name"] == "test_name"
        assert event_dict["data"] == {"key": "value"}


class TestEventStore:
    """Test cases for the EventStore class."""

    @pytest.fixture
    def sample_event(self):
        """Create a sample event for testing."""
        return Event(
            event_type=EventTypes.APPLICATION_EVENT.value,
            event_name=ApplicationEventNames.WORKER_CREATED.value,
            data={
                "application_name": "test_app",
                "task_queue": "test_queue",
                "workflow_count": 2,
                "activity_count": 3,
            },
        )

    @patch("application_sdk.outputs.eventstore.workflow")
    @patch("application_sdk.outputs.eventstore.activity")
    def test_enrich_event_metadata_with_workflow_context(
        self, mock_activity, mock_workflow, sample_event
    ):
        """Test enriching event metadata with workflow context."""
        # Mock workflow info
        mock_workflow_info = Mock()
        mock_workflow_info.workflow_type = "TestWorkflow"
        mock_workflow_info.workflow_id = "test_workflow_id"
        mock_workflow_info.run_id = "test_run_id"
        mock_workflow.info.return_value = mock_workflow_info

        # Mock activity info to return None (not in activity context)
        mock_activity.info.return_value = None

        enriched_event = EventStore.enrich_event_metadata(sample_event)

        assert enriched_event.metadata.workflow_type == "TestWorkflow"
        assert enriched_event.metadata.workflow_id == "test_workflow_id"
        assert enriched_event.metadata.workflow_run_id == "test_run_id"
        assert enriched_event.metadata.workflow_state == WorkflowStates.UNKNOWN.value

    @patch("application_sdk.outputs.eventstore.workflow")
    @patch("application_sdk.outputs.eventstore.activity")
    def test_enrich_event_metadata_with_activity_context(
        self, mock_activity, mock_workflow, sample_event
    ):
        """Test enriching event metadata with activity context."""
        # Mock workflow info to return None (not in workflow context)
        mock_workflow.info.return_value = None

        # Mock activity info
        mock_activity_info = Mock()
        mock_activity_info.activity_type = "TestActivity"
        mock_activity_info.activity_id = "test_activity_id"
        mock_activity_info.attempt = 1
        mock_activity_info.workflow_type = "TestWorkflow"
        mock_activity_info.workflow_id = "test_workflow_id"
        mock_activity_info.workflow_run_id = "test_run_id"
        mock_activity.info.return_value = mock_activity_info

        enriched_event = EventStore.enrich_event_metadata(sample_event)

        assert enriched_event.metadata.activity_type == "TestActivity"
        assert enriched_event.metadata.activity_id == "test_activity_id"
        assert enriched_event.metadata.attempt == 1
        assert enriched_event.metadata.workflow_type == "TestWorkflow"
        assert enriched_event.metadata.workflow_id == "test_workflow_id"
        assert enriched_event.metadata.workflow_run_id == "test_run_id"
        assert enriched_event.metadata.workflow_state == WorkflowStates.RUNNING.value

    @patch("application_sdk.outputs.eventstore.workflow")
    @patch("application_sdk.outputs.eventstore.activity")
    def test_enrich_event_metadata_no_context(
        self, mock_activity, mock_workflow, sample_event
    ):
        """Test enriching event metadata when not in workflow or activity context."""
        # Mock both workflow and activity info to return None
        mock_workflow.info.return_value = None
        mock_activity.info.return_value = None

        enriched_event = EventStore.enrich_event_metadata(sample_event)

        assert enriched_event.metadata.workflow_type is None
        assert enriched_event.metadata.activity_type is None
        assert enriched_event.metadata.workflow_state == WorkflowStates.UNKNOWN.value

    @patch("application_sdk.outputs.eventstore.IS_EXTERNAL_DEPLOYMENT", True)
    @patch("application_sdk.outputs.eventstore.clients.DaprClient")
    @patch("application_sdk.outputs.eventstore.AtlanAuthClient")
    def test_publish_event_external_deployment_success(
        self, mock_auth_client, mock_dapr_client, sample_event
    ):
        """Test publishing event to external deployment successfully."""
        # Mock auth client
        mock_auth_instance = Mock()
        mock_auth_client.return_value = mock_auth_instance
        mock_auth_instance.get_access_token = AsyncMock(return_value="test_token_123")

        # Mock DAPR client
        mock_dapr_instance = Mock()
        mock_dapr_client.return_value.__enter__.return_value = mock_dapr_instance

        # Mock asyncio.run to return the token
        with patch(
            "application_sdk.outputs.eventstore.asyncio.run",
            return_value="test_token_123",
        ):
            EventStore.publish_event(sample_event)

        # Verify DAPR binding was called with correct parameters
        mock_dapr_instance.invoke_binding.assert_called_once()
        call_args = mock_dapr_instance.invoke_binding.call_args

        assert call_args[1]["binding_name"] == HTTP_BINDING_NAME
        assert call_args[1]["operation"] == "post"
        assert call_args[1]["binding_metadata"]["content-type"] == "application/json"
        assert (
            call_args[1]["binding_metadata"]["Authorization"] == "Bearer test_token_123"
        )

    @patch("application_sdk.outputs.eventstore.IS_EXTERNAL_DEPLOYMENT", True)
    @patch("application_sdk.outputs.eventstore.clients.DaprClient")
    @patch("application_sdk.outputs.eventstore.AtlanAuthClient")
    def test_publish_event_external_deployment_no_token(
        self, mock_auth_client, mock_dapr_client, sample_event
    ):
        """Test publishing event to external deployment when auth token is not available."""
        # Mock auth client to raise exception
        mock_auth_instance = Mock()
        mock_auth_client.return_value = mock_auth_instance
        mock_auth_instance.get_access_token = AsyncMock(
            side_effect=Exception("Auth failed")
        )

        # Mock DAPR client
        mock_dapr_instance = Mock()
        mock_dapr_client.return_value.__enter__.return_value = mock_dapr_instance

        # Mock asyncio.run to raise exception
        with patch(
            "application_sdk.outputs.eventstore.asyncio.run",
            side_effect=Exception("Auth failed"),
        ):
            EventStore.publish_event(sample_event)

        # Verify DAPR binding was called without Authorization header
        mock_dapr_instance.invoke_binding.assert_called_once()
        call_args = mock_dapr_instance.invoke_binding.call_args

        assert call_args[1]["binding_name"] == HTTP_BINDING_NAME
        assert call_args[1]["operation"] == "post"
        assert call_args[1]["binding_metadata"]["content-type"] == "application/json"
        assert "Authorization" not in call_args[1]["binding_metadata"]

    @patch("application_sdk.outputs.eventstore.IS_EXTERNAL_DEPLOYMENT", False)
    @patch("application_sdk.outputs.eventstore.clients.DaprClient")
    def test_publish_event_internal_deployment(self, mock_dapr_client, sample_event):
        """Test publishing event to internal pub/sub system."""
        # Mock DAPR client
        mock_dapr_instance = Mock()
        mock_dapr_client.return_value.__enter__.return_value = mock_dapr_instance

        EventStore.publish_event(sample_event)

        # Verify DAPR publish_event was called with correct parameters
        mock_dapr_instance.publish_event.assert_called_once()
        call_args = mock_dapr_instance.publish_event.call_args

        assert call_args[1]["pubsub_name"] == "eventstore"
        assert call_args[1]["topic_name"] == "application_event_topic"
        assert call_args[1]["data_content_type"] == "application/json"

    @patch("application_sdk.outputs.eventstore.IS_EXTERNAL_DEPLOYMENT", True)
    @patch("application_sdk.outputs.eventstore.clients.DaprClient")
    def test_publish_event_external_deployment_dapr_error(
        self, mock_dapr_client, sample_event
    ):
        """Test publishing event when DAPR client raises an error."""
        # Mock DAPR client to raise exception
        mock_dapr_instance = Mock()
        mock_dapr_client.return_value.__enter__.return_value = mock_dapr_instance
        mock_dapr_instance.invoke_binding.side_effect = Exception("DAPR error")

        # Should not raise exception, should log error
        with patch("application_sdk.outputs.eventstore.logger") as mock_logger:
            EventStore.publish_event(sample_event)
            mock_logger.error.assert_called_once()

    @patch("application_sdk.outputs.eventstore.IS_EXTERNAL_DEPLOYMENT", False)
    @patch("application_sdk.outputs.eventstore.clients.DaprClient")
    def test_publish_event_internal_deployment_dapr_error(
        self, mock_dapr_client, sample_event
    ):
        """Test publishing event to internal deployment when DAPR raises an error."""
        # Mock DAPR client to raise exception
        mock_dapr_instance = Mock()
        mock_dapr_client.return_value.__enter__.return_value = mock_dapr_instance
        mock_dapr_instance.publish_event.side_effect = Exception("DAPR error")

        # Should not raise exception, should log error
        with patch("application_sdk.outputs.eventstore.logger") as mock_logger:
            EventStore.publish_event(sample_event)
            mock_logger.error.assert_called_once()

    def test_publish_event_without_enrichment(self, sample_event):
        """Test publishing event without metadata enrichment."""
        with patch("application_sdk.outputs.eventstore.IS_EXTERNAL_DEPLOYMENT", False):
            with patch("application_sdk.outputs.eventstore.clients.DaprClient"):
                # Should not call enrich_event_metadata
                with patch.object(EventStore, "enrich_event_metadata") as mock_enrich:
                    EventStore.publish_event(sample_event, enrich_metadata=False)
                    mock_enrich.assert_not_called()

    @patch("application_sdk.outputs.eventstore.IS_EXTERNAL_DEPLOYMENT", True)
    @patch("application_sdk.outputs.eventstore.clients.DaprClient")
    @patch("application_sdk.outputs.eventstore.AtlanAuthClient")
    def test_publish_event_external_deployment_with_thread_executor(
        self, mock_auth_client, mock_dapr_client, sample_event
    ):
        """Test publishing event to external deployment using thread executor for async token."""
        # Mock auth client
        mock_auth_instance = Mock()
        mock_auth_client.return_value = mock_auth_instance
        mock_auth_instance.get_access_token = AsyncMock(return_value="test_token_123")

        # Mock DAPR client
        mock_dapr_instance = Mock()
        mock_dapr_client.return_value.__enter__.return_value = mock_dapr_instance

        # Mock asyncio.run to raise RuntimeError (event loop already running)
        with patch(
            "application_sdk.outputs.eventstore.asyncio.run",
            side_effect=RuntimeError("Event loop already running"),
        ):
            # Mock ThreadPoolExecutor
            with patch(
                "application_sdk.outputs.eventstore.ThreadPoolExecutor"
            ) as mock_executor:
                mock_future = Mock()
                mock_future.result.return_value = "test_token_123"
                mock_executor.return_value.__enter__.return_value.submit.return_value = mock_future

                EventStore.publish_event(sample_event)

        # Verify DAPR binding was called with correct parameters
        mock_dapr_instance.invoke_binding.assert_called_once()
        call_args = mock_dapr_instance.invoke_binding.call_args

        assert call_args[1]["binding_name"] == HTTP_BINDING_NAME
        assert call_args[1]["operation"] == "post"
        assert call_args[1]["binding_metadata"]["content-type"] == "application/json"
        assert (
            call_args[1]["binding_metadata"]["Authorization"] == "Bearer test_token_123"
        )

    @patch("application_sdk.outputs.eventstore.IS_EXTERNAL_DEPLOYMENT", True)
    @patch("application_sdk.outputs.eventstore.clients.DaprClient")
    @patch("application_sdk.outputs.eventstore.AtlanAuthClient")
    def test_publish_event_external_deployment_token_timeout(
        self, mock_auth_client, mock_dapr_client, sample_event
    ):
        """Test publishing event to external deployment when token retrieval times out."""
        # Mock auth client
        mock_auth_instance = Mock()
        mock_auth_client.return_value = mock_auth_instance
        mock_auth_instance.get_access_token = AsyncMock(return_value="test_token_123")

        # Mock DAPR client
        mock_dapr_instance = Mock()
        mock_dapr_client.return_value.__enter__.return_value = mock_dapr_instance

        # Mock asyncio.run to raise RuntimeError
        with patch(
            "application_sdk.outputs.eventstore.asyncio.run",
            side_effect=RuntimeError("Event loop already running"),
        ):
            # Mock ThreadPoolExecutor to raise timeout
            with patch(
                "application_sdk.outputs.eventstore.ThreadPoolExecutor"
            ) as mock_executor:
                from concurrent.futures import TimeoutError

                mock_future = Mock()
                mock_future.result.side_effect = TimeoutError(
                    "Token retrieval timed out"
                )
                mock_executor.return_value.__enter__.return_value.submit.return_value = mock_future

                EventStore.publish_event(sample_event)

        # Verify DAPR binding was called without Authorization header
        mock_dapr_instance.invoke_binding.assert_called_once()
        call_args = mock_dapr_instance.invoke_binding.call_args

        assert call_args[1]["binding_name"] == HTTP_BINDING_NAME
        assert call_args[1]["operation"] == "post"
        assert call_args[1]["binding_metadata"]["content-type"] == "application/json"
        assert "Authorization" not in call_args[1]["binding_metadata"]

    @patch("application_sdk.outputs.eventstore.IS_EXTERNAL_DEPLOYMENT", True)
    @patch("application_sdk.outputs.eventstore.clients.DaprClient")
    @patch("application_sdk.outputs.eventstore.AtlanAuthClient")
    def test_publish_event_external_deployment_empty_token(
        self, mock_auth_client, mock_dapr_client, sample_event
    ):
        """Test publishing event to external deployment when auth token is empty."""
        # Mock auth client
        mock_auth_instance = Mock()
        mock_auth_client.return_value = mock_auth_instance
        mock_auth_instance.get_access_token = AsyncMock(return_value="")

        # Mock DAPR client
        mock_dapr_instance = Mock()
        mock_dapr_client.return_value.__enter__.return_value = mock_dapr_instance

        # Mock asyncio.run to return empty token
        with patch("application_sdk.outputs.eventstore.asyncio.run", return_value=""):
            EventStore.publish_event(sample_event)

        # Verify DAPR binding was called without Authorization header
        mock_dapr_instance.invoke_binding.assert_called_once()
        call_args = mock_dapr_instance.invoke_binding.call_args

        assert call_args[1]["binding_name"] == HTTP_BINDING_NAME
        assert call_args[1]["operation"] == "post"
        assert call_args[1]["binding_metadata"]["content-type"] == "application/json"
        assert "Authorization" not in call_args[1]["binding_metadata"]

    @patch("application_sdk.outputs.eventstore.IS_EXTERNAL_DEPLOYMENT", True)
    @patch("application_sdk.outputs.eventstore.clients.DaprClient")
    @patch("application_sdk.outputs.eventstore.AtlanAuthClient")
    def test_publish_event_external_deployment_none_token(
        self, mock_auth_client, mock_dapr_client, sample_event
    ):
        """Test publishing event to external deployment when auth token is None."""
        # Mock auth client
        mock_auth_instance = Mock()
        mock_auth_client.return_value = mock_auth_instance
        mock_auth_instance.get_access_token = AsyncMock(return_value=None)

        # Mock DAPR client
        mock_dapr_instance = Mock()
        mock_dapr_client.return_value.__enter__.return_value = mock_dapr_instance

        # Mock asyncio.run to return None
        with patch("application_sdk.outputs.eventstore.asyncio.run", return_value=None):
            EventStore.publish_event(sample_event)

        # Verify DAPR binding was called without Authorization header
        mock_dapr_instance.invoke_binding.assert_called_once()
        call_args = mock_dapr_instance.invoke_binding.call_args

        assert call_args[1]["binding_name"] == HTTP_BINDING_NAME
        assert call_args[1]["operation"] == "post"
        assert call_args[1]["binding_metadata"]["content-type"] == "application/json"
        assert "Authorization" not in call_args[1]["binding_metadata"]


class TestApplicationEventNames:
    """Test cases for ApplicationEventNames enum."""

    def test_worker_created_event_name(self):
        """Test that WORKER_CREATED event name is correctly defined."""
        assert ApplicationEventNames.WORKER_CREATED.value == "worker_created"

    def test_all_event_names(self):
        """Test all event names are correctly defined."""
        expected_names = {
            "WORKFLOW_END": "workflow_end",
            "WORKFLOW_START": "workflow_start",
            "ACTIVITY_START": "activity_start",
            "ACTIVITY_END": "activity_end",
            "WORKER_CREATED": "worker_created",
        }

        for name, value in expected_names.items():
            assert getattr(ApplicationEventNames, name).value == value


class TestEventMetadata:
    """Test cases for EventMetadata class."""

    def test_event_metadata_creation(self):
        """Test basic EventMetadata creation."""
        metadata = EventMetadata()

        assert metadata.application_name == "default"
        assert metadata.event_published_client_timestamp == 0
        assert metadata.workflow_type is None
        assert metadata.workflow_id is None
        assert metadata.workflow_run_id is None
        assert metadata.workflow_state == WorkflowStates.UNKNOWN.value
        assert metadata.activity_type is None
        assert metadata.activity_id is None
        assert metadata.attempt is None
        assert metadata.topic_name is None

    def test_event_metadata_with_values(self):
        """Test EventMetadata creation with specific values."""
        metadata = EventMetadata(
            application_name="test_app",
            workflow_type="TestWorkflow",
            workflow_id="test_id",
            workflow_state=WorkflowStates.RUNNING.value,
            activity_type="TestActivity",
            activity_id="test_activity_id",
            attempt=1,
        )

        assert metadata.application_name == "test_app"
        assert metadata.workflow_type == "TestWorkflow"
        assert metadata.workflow_id == "test_id"
        assert metadata.workflow_state == WorkflowStates.RUNNING.value
        assert metadata.activity_type == "TestActivity"
        assert metadata.activity_id == "test_activity_id"
        assert metadata.attempt == 1
