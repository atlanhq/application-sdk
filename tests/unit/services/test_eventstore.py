"""Unit tests for the eventstore module."""

from unittest.mock import AsyncMock, Mock, patch

import pytest

from application_sdk.constants import EVENT_STORE_NAME
from application_sdk.events.models import (
    ApplicationEventNames,
    Event,
    EventMetadata,
    EventTypes,
    WorkflowStates,
)
from application_sdk.services.eventstore import EventStore


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
        assert event.get_topic_name() == "test_event"

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
            event_name=ApplicationEventNames.WORKER_START.value,
            data={
                "application_name": "test_app",
                "task_queue": "test_queue",
                "workflow_count": 2,
                "activity_count": 3,
            },
        )

    @patch("application_sdk.services.eventstore.workflow")
    @patch("application_sdk.services.eventstore.activity")
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

    @patch("application_sdk.services.eventstore.workflow")
    @patch("application_sdk.services.eventstore.activity")
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

    @patch("application_sdk.services.eventstore.workflow")
    @patch("application_sdk.services.eventstore.activity")
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

    @patch("application_sdk.services.eventstore.clients.DaprClient")
    @patch("application_sdk.clients.atlan_auth.AtlanAuthClient")
    @patch("application_sdk.services.eventstore.is_component_registered")
    async def test_publish_event_success(
        self,
        mock_is_component_registered,
        mock_auth_client,
        mock_dapr_client,
        sample_event,
    ):
        """Test publishing event successfully via binding."""
        # Mock component registration check
        mock_is_component_registered.return_value = True

        # Mock auth client
        mock_auth_instance = Mock()
        mock_auth_client.return_value = mock_auth_instance
        mock_auth_instance.get_authenticated_headers = AsyncMock(
            return_value={"Authorization": "Bearer test_token"}
        )

        # Mock DAPR client
        mock_dapr_instance = Mock()
        mock_dapr_client.return_value.__enter__.return_value = mock_dapr_instance

        await EventStore.publish_event(sample_event)

        # Verify DAPR invoke_binding was called with correct parameters
        mock_dapr_instance.invoke_binding.assert_called_once()
        call_args = mock_dapr_instance.invoke_binding.call_args

        assert call_args[1]["binding_name"] == EVENT_STORE_NAME
        assert call_args[1]["operation"] == "create"
        assert call_args[1]["binding_metadata"]["content-type"] == "application/json"
        assert call_args[1]["binding_metadata"]["Authorization"] == "Bearer test_token"

    async def test_publish_event_always_enriches_metadata(self, sample_event):
        """Test publishing event always enriches metadata."""
        with patch("application_sdk.services.eventstore.clients.DaprClient"):
            with patch(
                "application_sdk.services.eventstore.is_component_registered",
                return_value=True,
            ):
                # Should always call enrich_event_metadata
                await EventStore.publish_event(sample_event)


class TestApplicationEventNames:
    """Test cases for ApplicationEventNames enum."""

    def test_worker_created_event_name(self):
        """Test worker created event name."""
        assert ApplicationEventNames.WORKER_START.value == "worker_start"

    def test_all_event_names(self):
        """Test all event names are properly defined."""
        expected_names = [
            "workflow_end",
            "workflow_start",
            "activity_start",
            "activity_end",
            "worker_start",
            "worker_end",
            "application_start",
            "application_end",
        ]
        actual_names = [name.value for name in ApplicationEventNames]
        assert actual_names == expected_names


class TestEventMetadata:
    """Test cases for EventMetadata class."""

    def test_event_metadata_creation(self):
        """Test basic event metadata creation."""
        metadata = EventMetadata()
        assert metadata.application_name is not None
        assert metadata.created_timestamp == 0
        assert metadata.workflow_type is None
        assert metadata.workflow_id is None
        assert metadata.workflow_run_id is None
        assert metadata.workflow_state == WorkflowStates.UNKNOWN.value
        assert metadata.activity_type is None
        assert metadata.activity_id is None
        assert metadata.attempt is None
        assert metadata.topic_name is None

    def test_event_metadata_with_values(self):
        """Test event metadata creation with specific values."""
        metadata = EventMetadata(
            application_name="test_app",
            workflow_type="test_workflow",
            workflow_id="test_id",
            workflow_run_id="test_run_id",
            workflow_state=WorkflowStates.RUNNING.value,
            activity_type="test_activity",
            activity_id="test_activity_id",
            attempt=1,
        )
        assert metadata.application_name == "test_app"
        assert metadata.workflow_type == "test_workflow"
        assert metadata.workflow_id == "test_id"
        assert metadata.workflow_run_id == "test_run_id"
        assert metadata.workflow_state == WorkflowStates.RUNNING.value
        assert metadata.activity_type == "test_activity"
        assert metadata.activity_id == "test_activity_id"
        assert metadata.attempt == 1
