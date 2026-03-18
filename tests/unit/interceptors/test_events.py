"""Unit tests for the event interceptor."""

from __future__ import annotations

from typing import Any
from unittest import mock

import pytest

from application_sdk.interceptors.events import (
    EventActivityInboundInterceptor,
    EventInterceptor,
    EventWorkflowInboundInterceptor,
    publish_event,
)


class TestEventInterceptor:
    """Tests for EventInterceptor (the Temporal Interceptor class)."""

    def test_intercept_activity_wraps_next(self) -> None:
        interceptor = EventInterceptor()
        mock_next = mock.MagicMock()
        result = interceptor.intercept_activity(mock_next)
        assert isinstance(result, EventActivityInboundInterceptor)

    def test_workflow_interceptor_class_returns_event_workflow_interceptor(
        self,
    ) -> None:
        interceptor = EventInterceptor()
        mock_input = mock.MagicMock()
        result = interceptor.workflow_interceptor_class(mock_input)
        assert result is EventWorkflowInboundInterceptor

    def test_workflow_interceptor_class_never_returns_none(self) -> None:
        interceptor = EventInterceptor()
        mock_input = mock.MagicMock()
        result = interceptor.workflow_interceptor_class(mock_input)
        assert result is not None


class TestEventActivityInboundInterceptor:
    """Tests for EventActivityInboundInterceptor."""

    @pytest.mark.asyncio
    async def test_execute_activity_calls_next(self) -> None:
        mock_next = mock.AsyncMock()
        mock_next.execute_activity = mock.AsyncMock(return_value="activity_result")
        interceptor = EventActivityInboundInterceptor(mock_next)
        mock_input = mock.MagicMock()

        with mock.patch(
            "application_sdk.interceptors.events._publish_event_via_binding",
            new_callable=mock.AsyncMock,
        ):
            result = await interceptor.execute_activity(mock_input)

        assert result == "activity_result"
        mock_next.execute_activity.assert_called_once_with(mock_input)

    @pytest.mark.asyncio
    async def test_execute_activity_publishes_start_event(self) -> None:
        mock_next = mock.AsyncMock()
        mock_next.execute_activity = mock.AsyncMock(return_value="ok")
        interceptor = EventActivityInboundInterceptor(mock_next)
        mock_input = mock.MagicMock()

        with mock.patch(
            "application_sdk.interceptors.events._publish_event_via_binding",
            new_callable=mock.AsyncMock,
        ) as mock_publish:
            await interceptor.execute_activity(mock_input)

        # Should be called at least twice (start + end)
        assert mock_publish.call_count >= 2

    @pytest.mark.asyncio
    async def test_execute_activity_publishes_end_event_on_success(self) -> None:
        mock_next = mock.AsyncMock()
        mock_next.execute_activity = mock.AsyncMock(return_value="result")
        interceptor = EventActivityInboundInterceptor(mock_next)
        mock_input = mock.MagicMock()

        published_events = []

        async def capture_event(event: Any) -> None:
            published_events.append(event)

        with mock.patch(
            "application_sdk.interceptors.events._publish_event_via_binding",
            side_effect=capture_event,
        ):
            await interceptor.execute_activity(mock_input)

        # Should have start and end events
        assert len(published_events) == 2
        # End event has duration_ms
        end_event = published_events[1]
        assert "duration_ms" in end_event.data

    @pytest.mark.asyncio
    async def test_execute_activity_reraises_exception(self) -> None:
        mock_next = mock.AsyncMock()
        mock_next.execute_activity = mock.AsyncMock(
            side_effect=ValueError("activity failed")
        )
        interceptor = EventActivityInboundInterceptor(mock_next)
        mock_input = mock.MagicMock()

        with mock.patch(
            "application_sdk.interceptors.events._publish_event_via_binding",
            new_callable=mock.AsyncMock,
        ):
            with pytest.raises(ValueError, match="activity failed"):
                await interceptor.execute_activity(mock_input)

    @pytest.mark.asyncio
    async def test_execute_activity_publishes_end_event_on_failure(self) -> None:
        mock_next = mock.AsyncMock()
        mock_next.execute_activity = mock.AsyncMock(side_effect=RuntimeError("failure"))
        interceptor = EventActivityInboundInterceptor(mock_next)
        mock_input = mock.MagicMock()

        with mock.patch(
            "application_sdk.interceptors.events._publish_event_via_binding",
            new_callable=mock.AsyncMock,
        ) as mock_publish:
            with pytest.raises(RuntimeError):
                await interceptor.execute_activity(mock_input)

        # End event should still be published (finally block)
        assert mock_publish.call_count == 2


class TestPublishEventActivity:
    """Tests for the publish_event activity function."""

    @pytest.mark.asyncio
    async def test_publishes_event_to_event_store(self) -> None:
        event_data = {
            "event_type": "APPLICATION_EVENT",
            "event_name": "test_event",
            "data": {},
        }

        with mock.patch(
            "application_sdk.interceptors.events._publish_event_via_binding",
            new_callable=mock.AsyncMock,
        ) as mock_publish:
            await publish_event(event_data)

        mock_publish.assert_called_once()

    @pytest.mark.asyncio
    async def test_reraises_on_eventstore_failure(self) -> None:
        event_data = {
            "event_type": "APPLICATION_EVENT",
            "event_name": "test_event",
            "data": {},
        }

        with mock.patch(
            "application_sdk.interceptors.events._publish_event_via_binding",
            new_callable=mock.AsyncMock,
            side_effect=Exception("store down"),
        ):
            with pytest.raises(Exception, match="store down"):
                await publish_event(event_data)
