"""Unit tests for the event interceptor."""

from __future__ import annotations

from typing import Any
from unittest import mock

import pytest

from application_sdk.contracts.events import (
    ApplicationEventNames,
    Event,
    EventTypes,
    WorkflowStates,
)

# These tests intentionally import private Temporal interceptors because they
# validate internal event publication behavior wired into the worker runtime.
from application_sdk.execution._temporal.interceptors import events as events_module
from application_sdk.execution._temporal.interceptors.events import (
    EventActivityInboundInterceptor,
    EventInterceptor,
    EventWorkflowInboundInterceptor,
    _enrich_event_metadata,
    _get_event_token_service,
    _publish_event_via_binding,
    _send_lifecycle_event_to_segment,
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
            "application_sdk.execution._temporal.interceptors.events._publish_event_via_binding",
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
            "application_sdk.execution._temporal.interceptors.events._publish_event_via_binding",
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
            "application_sdk.execution._temporal.interceptors.events._publish_event_via_binding",
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
            "application_sdk.execution._temporal.interceptors.events._publish_event_via_binding",
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
            "application_sdk.execution._temporal.interceptors.events._publish_event_via_binding",
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
            "application_sdk.execution._temporal.interceptors.events._publish_event_via_binding",
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
            "application_sdk.execution._temporal.interceptors.events._publish_event_via_binding",
            new_callable=mock.AsyncMock,
            side_effect=Exception("store down"),
        ):
            with pytest.raises(Exception, match="Failed to publish event") as exc_info:
                await publish_event(event_data)
            assert "store down" in str(exc_info.value.__cause__)


# ---------------------------------------------------------------------------
# Additional coverage for auth-adjacent and lazy-import code paths.
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=False)
def _reset_event_token_service():
    """Null the module-global cache between tests."""
    events_module._event_token_service = None
    yield
    events_module._event_token_service = None


class TestGetEventTokenService:
    """Tests for the _get_event_token_service singleton."""

    @pytest.mark.asyncio
    async def test_returns_none_when_auth_disabled(
        self, _reset_event_token_service
    ) -> None:
        with mock.patch.object(events_module, "AUTH_ENABLED", False, create=True):
            # Patch via the constants module — the function imports lazily.
            with mock.patch(
                "application_sdk.constants.AUTH_ENABLED", False, create=True
            ):
                result = await _get_event_token_service()
        assert result is None
        # Cache must remain empty when auth is disabled.
        assert events_module._event_token_service is None

    @pytest.mark.asyncio
    async def test_returns_none_when_credentials_missing(
        self, _reset_event_token_service
    ) -> None:
        with (
            mock.patch("application_sdk.constants.AUTH_ENABLED", True, create=True),
            mock.patch("application_sdk.constants.AUTH_URL", "", create=True),
            mock.patch(
                "application_sdk.infrastructure.secrets.get_deployment_secret",
                new=mock.AsyncMock(return_value=""),
            ),
        ):
            result = await _get_event_token_service()
        assert result is None

    @pytest.mark.asyncio
    async def test_constructs_and_caches_when_configured(
        self, _reset_event_token_service
    ) -> None:
        fake_service = mock.MagicMock(name="OAuthTokenService")
        fake_service_cls = mock.MagicMock(return_value=fake_service)
        with (
            mock.patch("application_sdk.constants.AUTH_ENABLED", True, create=True),
            mock.patch(
                "application_sdk.constants.AUTH_URL",
                "https://example.com/token",
                create=True,
            ),
            mock.patch(
                "application_sdk.infrastructure.secrets.get_deployment_secret",
                new=mock.AsyncMock(side_effect=["client-id", "secret"]),
            ),
            mock.patch(
                "application_sdk.credentials.oauth.OAuthTokenService", fake_service_cls
            ),
        ):
            first = await _get_event_token_service()
            second = await _get_event_token_service()
        assert first is fake_service
        assert second is fake_service
        # Constructed exactly once — singleton cache works.
        assert fake_service_cls.call_count == 1


class TestEnrichEventMetadata:
    """Direct tests for _enrich_event_metadata."""

    def _make_event(self) -> Event:
        return Event(
            event_type=EventTypes.APPLICATION_EVENT.value,
            event_name=ApplicationEventNames.WORKFLOW_START.value,
            data={},
        )

    def test_sets_application_name_and_topic(self) -> None:
        with (
            mock.patch("application_sdk.constants.APPLICATION_NAME", "unit-test-app"),
            mock.patch.object(
                events_module.workflow, "info", side_effect=RuntimeError("no ctx")
            ),
            mock.patch.object(
                events_module.activity, "info", side_effect=RuntimeError("no ctx")
            ),
        ):
            event = self._make_event()
            enriched = _enrich_event_metadata(event)
        assert enriched.metadata.application_name == "unit-test-app"
        assert enriched.metadata.topic_name == event.get_topic_name()
        assert enriched.metadata.created_timestamp > 0

    def test_swallows_workflow_and_activity_info_errors(self) -> None:
        with (
            mock.patch.object(
                events_module.workflow, "info", side_effect=RuntimeError("boom")
            ),
            mock.patch.object(
                events_module.activity, "info", side_effect=RuntimeError("boom")
            ),
        ):
            enriched = _enrich_event_metadata(self._make_event())
        # No workflow/activity fields populated when context unavailable.
        assert enriched.metadata.workflow_id is None
        assert enriched.metadata.activity_id is None

    def test_populates_from_workflow_info(self) -> None:
        wf_info = mock.MagicMock(
            workflow_type="my-wf",
            workflow_id="wf-1",
            run_id="run-1",
        )
        with (
            mock.patch.object(events_module.workflow, "info", return_value=wf_info),
            mock.patch.object(
                events_module.activity, "info", side_effect=RuntimeError("no ctx")
            ),
        ):
            enriched = _enrich_event_metadata(self._make_event())
        assert enriched.metadata.workflow_type == "my-wf"
        assert enriched.metadata.workflow_id == "wf-1"
        assert enriched.metadata.workflow_run_id == "run-1"

    def test_populates_from_activity_info(self) -> None:
        act_info = mock.MagicMock(
            activity_type="my-act",
            activity_id="act-1",
            attempt=2,
            workflow_type="wf-type",
            workflow_id="wf-1",
            workflow_run_id="run-1",
        )
        with (
            mock.patch.object(
                events_module.workflow, "info", side_effect=RuntimeError("no ctx")
            ),
            mock.patch.object(events_module.activity, "info", return_value=act_info),
        ):
            enriched = _enrich_event_metadata(self._make_event())
        assert enriched.metadata.activity_type == "my-act"
        assert enriched.metadata.activity_id == "act-1"
        assert enriched.metadata.attempt == 2
        assert enriched.metadata.workflow_state == WorkflowStates.RUNNING.value

    def test_sdk_version_defaults_to_installed_version(self) -> None:
        """EventMetadata.sdk_version must always be populated with the running
        SDK version — independent of ATLAN_SDK_VERSION env injection."""
        from application_sdk.contracts.events import EventMetadata
        from application_sdk.version import __version__ as sdk_version

        # Default constructor — no enrichment needed for this field.
        assert EventMetadata().sdk_version == sdk_version
        # Constructed via the base Event default_factory path.
        event = self._make_event()
        assert event.metadata.sdk_version == sdk_version
        # Enrichment must not clobber it.
        with (
            mock.patch.object(
                events_module.workflow, "info", side_effect=RuntimeError("no ctx")
            ),
            mock.patch.object(
                events_module.activity, "info", side_effect=RuntimeError("no ctx")
            ),
        ):
            enriched = _enrich_event_metadata(event)
        assert enriched.metadata.sdk_version == sdk_version


class TestSendLifecycleEventToSegment:
    """Tests for _send_lifecycle_event_to_segment."""

    def test_skips_non_lifecycle_events(self) -> None:
        event = Event(
            event_type=EventTypes.APPLICATION_EVENT.value,
            event_name="some_other_event",
            data={},
        )
        with mock.patch(
            "application_sdk.observability.metrics_adaptor.get_metrics"
        ) as get_metrics:
            _send_lifecycle_event_to_segment(event)
        get_metrics.assert_not_called()

    def test_emits_metric_for_lifecycle_event(self) -> None:
        event = Event(
            event_type=EventTypes.APPLICATION_EVENT.value,
            event_name=ApplicationEventNames.WORKFLOW_START.value,
            data={"foo": "bar", "drop": [1, 2]},
        )
        event.metadata.workflow_id = "wf-1"
        event.metadata.workflow_run_id = "run-1"
        event.metadata.workflow_type = "my-wf"
        event.metadata.workflow_state = WorkflowStates.RUNNING.value
        event.metadata.activity_id = "a-1"
        event.metadata.activity_type = "my-act"
        event.metadata.attempt = 3
        event.metadata.created_timestamp = 1700000000

        metrics = mock.MagicMock()
        with (
            mock.patch(
                "application_sdk.observability.metrics_adaptor.get_metrics",
                return_value=metrics,
            ),
            mock.patch(
                "application_sdk.constants.APP_TENANT_ID", "tenant-x", create=True
            ),
            mock.patch(
                "application_sdk.constants.ATLAN_BASE_URL",
                "https://atlan.example",
                create=True,
            ),
        ):
            _send_lifecycle_event_to_segment(event)

        metrics.segment_client.send_metric.assert_called_once()
        record = metrics.segment_client.send_metric.call_args[0][0]
        assert record.name == "workflow_started"
        assert record.value == 1.0
        # Scalar data values become string labels; lists are filtered out.
        assert record.labels["foo"] == "bar"
        assert "drop" not in record.labels
        assert record.labels["workflow_id"] == "wf-1"
        assert record.labels["attempt"] == "3"

    def test_swallows_metric_emit_failure(self) -> None:
        event = Event(
            event_type=EventTypes.APPLICATION_EVENT.value,
            event_name=ApplicationEventNames.WORKFLOW_END.value,
            data={},
        )
        with mock.patch(
            "application_sdk.observability.metrics_adaptor.get_metrics",
            side_effect=RuntimeError("metrics unavailable"),
        ):
            # Must not raise — best-effort side-channel.
            _send_lifecycle_event_to_segment(event)


class TestPublishEventViaBinding:
    """Tests for _publish_event_via_binding."""

    @pytest.mark.asyncio
    async def test_skips_when_no_infrastructure(self) -> None:
        with mock.patch(
            "application_sdk.infrastructure.context.get_infrastructure",
            return_value=None,
        ):
            event = Event(
                event_type=EventTypes.APPLICATION_EVENT.value,
                event_name=ApplicationEventNames.WORKFLOW_START.value,
                data={},
            )
            await _publish_event_via_binding(event)  # no exception, no-op

    @pytest.mark.asyncio
    async def test_skips_when_no_event_binding(self) -> None:
        infra = mock.MagicMock(event_binding=None)
        with mock.patch(
            "application_sdk.infrastructure.context.get_infrastructure",
            return_value=infra,
        ):
            event = Event(
                event_type=EventTypes.APPLICATION_EVENT.value,
                event_name=ApplicationEventNames.WORKFLOW_END.value,
                data={},
            )
            await _publish_event_via_binding(event)

    @pytest.mark.asyncio
    async def test_publishes_with_auth_headers(
        self, _reset_event_token_service
    ) -> None:
        infra = mock.MagicMock()
        infra.event_binding = mock.MagicMock()
        infra.event_binding.invoke = mock.AsyncMock()

        token_service = mock.MagicMock()
        token_service.get_headers = mock.AsyncMock(
            return_value={"authorization": "Bearer token-xyz"}
        )

        event = Event(
            event_type=EventTypes.APPLICATION_EVENT.value,
            event_name=ApplicationEventNames.WORKFLOW_START.value,
            data={},
        )

        with (
            mock.patch(
                "application_sdk.infrastructure.context.get_infrastructure",
                return_value=infra,
            ),
            mock.patch.object(
                events_module,
                "_get_event_token_service",
                new=mock.AsyncMock(return_value=token_service),
            ),
            mock.patch.object(events_module, "_enrich_event_metadata", lambda e: e),
            mock.patch.object(
                events_module, "_send_lifecycle_event_to_segment", lambda e: None
            ),
        ):
            await _publish_event_via_binding(event)

        infra.event_binding.invoke.assert_awaited_once()
        kwargs = infra.event_binding.invoke.await_args.kwargs
        assert kwargs["operation"] == "create"
        assert kwargs["metadata"]["content-type"] == "application/json"
        assert kwargs["metadata"]["authorization"] == "Bearer token-xyz"

    @pytest.mark.asyncio
    async def test_publishes_when_token_service_returns_none(
        self, _reset_event_token_service
    ) -> None:
        infra = mock.MagicMock()
        infra.event_binding = mock.MagicMock()
        infra.event_binding.invoke = mock.AsyncMock()

        event = Event(
            event_type=EventTypes.APPLICATION_EVENT.value,
            event_name=ApplicationEventNames.WORKFLOW_START.value,
            data={},
        )

        with (
            mock.patch(
                "application_sdk.infrastructure.context.get_infrastructure",
                return_value=infra,
            ),
            mock.patch.object(
                events_module,
                "_get_event_token_service",
                new=mock.AsyncMock(return_value=None),
            ),
            mock.patch.object(events_module, "_enrich_event_metadata", lambda e: e),
            mock.patch.object(
                events_module, "_send_lifecycle_event_to_segment", lambda e: None
            ),
        ):
            await _publish_event_via_binding(event)

        kwargs = infra.event_binding.invoke.await_args.kwargs
        assert "authorization" not in kwargs["metadata"]

    @pytest.mark.asyncio
    async def test_swallows_token_service_failure(
        self, _reset_event_token_service
    ) -> None:
        """Failure to fetch auth headers must not block event publication."""
        infra = mock.MagicMock()
        infra.event_binding = mock.MagicMock()
        infra.event_binding.invoke = mock.AsyncMock()

        event = Event(
            event_type=EventTypes.APPLICATION_EVENT.value,
            event_name=ApplicationEventNames.WORKFLOW_START.value,
            data={},
        )

        with (
            mock.patch(
                "application_sdk.infrastructure.context.get_infrastructure",
                return_value=infra,
            ),
            mock.patch.object(
                events_module,
                "_get_event_token_service",
                new=mock.AsyncMock(side_effect=RuntimeError("auth down")),
            ),
            mock.patch.object(events_module, "_enrich_event_metadata", lambda e: e),
            mock.patch.object(
                events_module, "_send_lifecycle_event_to_segment", lambda e: None
            ),
        ):
            await _publish_event_via_binding(event)

        infra.event_binding.invoke.assert_awaited_once()


class TestEventWorkflowInboundInterceptor:
    """Tests for EventWorkflowInboundInterceptor.execute_workflow."""

    @pytest.mark.asyncio
    async def test_success_emits_start_and_end_events(self) -> None:
        next_iface = mock.AsyncMock()
        next_iface.execute_workflow = mock.AsyncMock(return_value="ok")
        interceptor = EventWorkflowInboundInterceptor(next_iface)

        # workflow.time must be deterministic so duration_ms is computable.
        times = iter([100.0, 110.0])
        execute_activity = mock.AsyncMock()

        with (
            mock.patch.object(
                events_module.workflow, "time", side_effect=lambda: next(times)
            ),
            mock.patch.object(
                events_module.workflow,
                "execute_activity",
                new=execute_activity,
            ),
        ):
            result = await interceptor.execute_workflow(mock.MagicMock())

        assert result == "ok"
        # Two execute_activity calls: workflow_start + workflow_end
        assert execute_activity.await_count == 2
        end_payload = execute_activity.await_args_list[1].args[1]
        assert end_payload["data"]["duration_ms"] == 10000.0
        assert (
            end_payload["metadata"]["workflow_state"] == WorkflowStates.COMPLETED.value
        )

    @pytest.mark.asyncio
    async def test_failure_emits_failed_end_event_and_reraises(self) -> None:
        next_iface = mock.AsyncMock()
        next_iface.execute_workflow = mock.AsyncMock(side_effect=ValueError("boom"))
        interceptor = EventWorkflowInboundInterceptor(next_iface)

        execute_activity = mock.AsyncMock()
        with (
            mock.patch.object(events_module.workflow, "time", return_value=1.0),
            mock.patch.object(
                events_module.workflow, "execute_activity", new=execute_activity
            ),
            pytest.raises(ValueError, match="boom"),
        ):
            await interceptor.execute_workflow(mock.MagicMock())

        # start + end emitted even on failure
        assert execute_activity.await_count == 2
        end_payload = execute_activity.await_args_list[1].args[1]
        assert end_payload["metadata"]["workflow_state"] == WorkflowStates.FAILED.value

    @pytest.mark.asyncio
    async def test_swallows_publish_failure_on_start(self) -> None:
        """A failure in workflow start-event publication must not abort the workflow."""
        next_iface = mock.AsyncMock()
        next_iface.execute_workflow = mock.AsyncMock(return_value="result")
        interceptor = EventWorkflowInboundInterceptor(next_iface)

        # First call (start emit) raises; second (end emit) succeeds.
        execute_activity = mock.AsyncMock(
            side_effect=[RuntimeError("start failed"), None]
        )
        with (
            mock.patch.object(events_module.workflow, "time", return_value=1.0),
            mock.patch.object(
                events_module.workflow, "execute_activity", new=execute_activity
            ),
        ):
            result = await interceptor.execute_workflow(mock.MagicMock())

        assert result == "result"
        assert execute_activity.await_count == 2

    @pytest.mark.asyncio
    async def test_swallows_publish_failure_on_end(self) -> None:
        next_iface = mock.AsyncMock()
        next_iface.execute_workflow = mock.AsyncMock(return_value="result")
        interceptor = EventWorkflowInboundInterceptor(next_iface)

        execute_activity = mock.AsyncMock(
            side_effect=[None, RuntimeError("end failed")]
        )
        with (
            mock.patch.object(events_module.workflow, "time", return_value=1.0),
            mock.patch.object(
                events_module.workflow, "execute_activity", new=execute_activity
            ),
        ):
            # End-event publish failure must be swallowed; result still propagates.
            result = await interceptor.execute_workflow(mock.MagicMock())
        assert result == "result"
