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


# ---------------------------------------------------------------------------
# Direct-HTTPS fallback when the Dapr eventstore binding fails
# ---------------------------------------------------------------------------

from application_sdk.infrastructure.bindings import (  # noqa: E402 — test-section import
    BindingError,
)

_NON_DELIVERY = 'Failed to invoke binding: 500 (dapr errorCode=ERR_INVOKE_OUTPUT_BINDING: Post "https://t/api/eventingress/": EOF)'
_AMBIGUOUS = "Failed to invoke binding: 500 (dapr errorCode=ERR_INVOKE_OUTPUT_BINDING: received status code 502)"


def _component(
    tmp_path,
    *,
    name="eventstore",
    type_="bindings.http",
    url="https://tenant.example/api/eventingress/",
):
    meta = (
        f"  - name: url\n    value: {url}\n"
        if url
        else "  - name: rootPath\n    value: /tmp/x\n"
    )
    (tmp_path / f"{name}.yaml").write_text(
        f"apiVersion: dapr.io/v1alpha1\nkind: Component\nmetadata:\n  name: {name}\n"
        f"spec:\n  type: {type_}\n  version: v1\n  metadata:\n{meta}"
    )
    return tmp_path


def _http_client(post_side_effect=None):
    client = mock.AsyncMock()
    client.__aenter__.return_value = client
    response = mock.MagicMock()
    response.raise_for_status = mock.MagicMock()
    client.post = mock.AsyncMock(return_value=response, side_effect=post_side_effect)
    return client


class TestDirectHttpFallback:
    """When daprd cannot deliver an event and the error proves nothing reached
    Event Ingress, the SDK posts it there itself — same payload, headers and
    URL the binding used — and says so loudly. Motivated by a Go-1.27 daprd
    whose TLS handshake a customer firewall closed while every other client in
    the pod passed."""

    @pytest.fixture(autouse=True)
    def _fresh_state(self, monkeypatch, tmp_path):
        events_module._reset_direct_publish_state()
        monkeypatch.setenv("DAPR_COMPONENTS_PATH", str(tmp_path))
        monkeypatch.chdir(tmp_path)  # so ./components does not exist either
        yield
        events_module._reset_direct_publish_state()

    def _event(self):
        return Event(
            event_type=EventTypes.APPLICATION_EVENT,
            event_name=ApplicationEventNames.WORKER_START,
            data={"k": "v"},
        )

    def _infra(self, invoke_side_effect):
        binding = mock.MagicMock()
        binding.invoke = mock.AsyncMock(side_effect=invoke_side_effect)
        infra = mock.MagicMock()
        infra.event_binding = binding
        return infra

    def _patches(self, infra, client, token_service=None, enabled=True):
        return (
            mock.patch(
                "application_sdk.infrastructure.context.get_infrastructure",
                return_value=infra,
            ),
            mock.patch.object(
                events_module,
                "_get_event_token_service",
                mock.AsyncMock(return_value=token_service),
            ),
            mock.patch(
                "application_sdk.constants.EVENT_INGRESS_DIRECT_FALLBACK", enabled
            ),
            mock.patch("httpx.AsyncClient", return_value=client),
            mock.patch.object(events_module, "logger"),
        )

    @pytest.mark.asyncio
    async def test_binding_success_never_touches_http(self, tmp_path):
        _component(tmp_path)
        infra, client = self._infra(None), _http_client()
        p = self._patches(infra, client)
        with (
            p[0],
            p[1],
            p[2],
            mock.patch("httpx.AsyncClient", return_value=client) as http_client,
            p[4] as log,
        ):
            await _publish_event_via_binding(self._event())
        http_client.assert_not_called()
        log.info.assert_called_once()
        log.warning.assert_not_called()

    @pytest.mark.asyncio
    async def test_non_delivery_error_posts_same_payload_headers_url_and_timeout(
        self, tmp_path
    ):
        _component(tmp_path)
        infra = self._infra(
            BindingError(_NON_DELIVERY, binding_name="eventstore", operation="create")
        )
        client = _http_client()
        token_service = mock.MagicMock()
        token_service.get_headers = mock.AsyncMock(
            return_value={"Authorization": "Bearer t0k"}
        )
        p = self._patches(infra, client, token_service)
        with (
            p[0],
            p[1],
            p[2],
            mock.patch("httpx.AsyncClient", return_value=client) as http_client,
            p[4] as log,
        ):
            await _publish_event_via_binding(self._event())

        http_client.assert_called_once()
        assert (
            http_client.call_args.kwargs["timeout"]
            == events_module._DIRECT_PUBLISH_TIMEOUT_SECONDS
        )
        (url,), kwargs = client.post.call_args
        assert url == "https://tenant.example/api/eventingress/"
        assert kwargs["content"] == infra.event_binding.invoke.call_args.kwargs["data"]
        assert (
            kwargs["headers"] == infra.event_binding.invoke.call_args.kwargs["metadata"]
        )
        assert kwargs["headers"]["Authorization"] == "Bearer t0k"
        warnings = [str(c.args[0]) for c in log.warning.call_args_list]
        assert any(w.startswith("FALLBACK ACTIVE") for w in warnings)
        assert any(
            w.startswith("Published event via direct HTTPS fallback") for w in warnings
        )
        # the success line must not carry a traceback
        success = next(
            c
            for c in log.warning.call_args_list
            if str(c.args[0]).startswith("Published event via direct")
        )
        assert "exc_info" not in success.kwargs
        log.info.assert_not_called()

    @pytest.mark.asyncio
    async def test_worker_start_budget_is_passed_through(self, tmp_path):
        _component(tmp_path)
        infra = self._infra(
            BindingError(_NON_DELIVERY, binding_name="eventstore", operation="create")
        )
        client = _http_client()
        p = self._patches(infra, client)
        with (
            p[0],
            p[1],
            p[2],
            mock.patch("httpx.AsyncClient", return_value=client) as http_client,
            p[4],
        ):
            await _publish_event_via_binding(
                self._event(),
                direct_publish_timeout=events_module.WORKER_START_DIRECT_PUBLISH_TIMEOUT_SECONDS,
            )
        assert http_client.call_args.kwargs["timeout"] == 20.0
        # the default must fit inside the 30 s publish_event activity budget
        assert events_module._DIRECT_PUBLISH_TIMEOUT_SECONDS < 30

    @pytest.mark.asyncio
    async def test_ambiguous_error_is_not_resent(self, tmp_path):
        """An upstream HTTP status means the far side answered; re-sending could
        double-deliver, so the fallback must not fire."""
        _component(tmp_path)
        infra = self._infra(
            BindingError(_AMBIGUOUS, binding_name="eventstore", operation="create")
        )
        client = _http_client()
        p = self._patches(infra, client)
        with (
            p[0],
            p[1],
            p[2],
            mock.patch("httpx.AsyncClient", return_value=client) as http_client,
            p[4] as log,
        ):
            with pytest.raises(BindingError):
                await _publish_event_via_binding(self._event())
        http_client.assert_not_called()
        assert any(
            str(c.args[0]).startswith("FALLBACK SKIPPED")
            for c in log.warning.call_args_list
        )

    @pytest.mark.asyncio
    async def test_fallback_disabled_reraises(self, tmp_path):
        _component(tmp_path)
        infra = self._infra(
            BindingError(_NON_DELIVERY, binding_name="eventstore", operation="create")
        )
        client = _http_client()
        p = self._patches(infra, client, enabled=False)
        with (
            p[0],
            p[1],
            p[2],
            mock.patch("httpx.AsyncClient", return_value=client) as http_client,
            p[4],
        ):
            with pytest.raises(BindingError):
                await _publish_event_via_binding(self._event())
        http_client.assert_not_called()

    @pytest.mark.asyncio
    async def test_fallback_failure_keeps_dapr_cause_and_suspends(self, tmp_path):
        _component(tmp_path)
        dapr_cause = RuntimeError("EOF from daprd")
        err = BindingError(
            _NON_DELIVERY,
            binding_name="eventstore",
            operation="create",
            cause=dapr_cause,
        )
        err.__cause__ = dapr_cause
        infra = self._infra(err)
        client = _http_client(post_side_effect=ConnectionError("tunnel closed"))
        p = self._patches(infra, client)
        with (
            p[0],
            p[1],
            p[2],
            mock.patch("httpx.AsyncClient", return_value=client) as http_client,
            p[4] as log,
        ):
            with pytest.raises(BindingError) as exc:
                await _publish_event_via_binding(self._event())
            # second event during the backoff: no HTTP attempt, logged as suspended
            with pytest.raises(BindingError):
                await _publish_event_via_binding(self._event())
        assert exc.value.__cause__ is dapr_cause  # original Dapr cause preserved
        assert any("tunnel closed" in n for n in getattr(exc.value, "__notes__", []))
        assert http_client.call_count == 1  # breaker stopped the second attempt
        log.exception.assert_called_once()
        assert str(log.exception.call_args.args[0]).startswith("FALLBACK FAILED")
        assert any(
            str(c.args[0]).startswith("FALLBACK SUSPENDED")
            for c in log.info.call_args_list
        )

    @pytest.mark.asyncio
    async def test_non_http_component_means_no_fallback(self, tmp_path):
        """The SDK's local-dev eventstore is bindings.localstorage with no url:
        never guess a URL for it."""
        _component(tmp_path, type_="bindings.localstorage", url=None)
        infra = self._infra(
            BindingError(_NON_DELIVERY, binding_name="eventstore", operation="create")
        )
        client = _http_client()
        p = self._patches(infra, client)
        with (
            p[0],
            p[1],
            p[2],
            mock.patch("httpx.AsyncClient", return_value=client) as http_client,
            p[4] as log,
            mock.patch(
                "application_sdk.constants.ATLAN_BASE_URL", "https://should-not-be-used"
            ),
        ):
            with pytest.raises(BindingError):
                await _publish_event_via_binding(self._event())
        http_client.assert_not_called()
        assert str(log.error.call_args.args[0]).startswith("FALLBACK UNAVAILABLE")

    def test_url_resolution_honours_event_store_name_and_ignores_bad_siblings(
        self, tmp_path
    ):
        (tmp_path / "aaa-broken.yaml").write_text("this: [is: not: yaml")  # sorts first
        _component(tmp_path, name="events", url="https://named/api/eventingress/")
        with mock.patch("application_sdk.constants.EVENT_STORE_NAME", "events"):
            assert (
                events_module._resolve_event_ingress_url()
                == "https://named/api/eventingress/"
            )

    def test_url_resolution_has_no_base_url_guess(self, tmp_path):
        with mock.patch(
            "application_sdk.constants.ATLAN_BASE_URL", "https://tenant.example"
        ):
            assert events_module._resolve_event_ingress_url() is None

    def test_url_is_resolved_once_then_cached(self, tmp_path):
        _component(tmp_path)
        assert (
            events_module._resolve_event_ingress_url()
            == "https://tenant.example/api/eventingress/"
        )
        (tmp_path / "eventstore.yaml").unlink()
        assert (
            events_module._resolve_event_ingress_url()
            == "https://tenant.example/api/eventingress/"
        )

    @pytest.mark.parametrize(
        "text,expected",
        [
            (_NON_DELIVERY, True),
            (
                'Post "https://t/": dial tcp 1.2.3.4:443: connect: connection refused',
                True,
            ),
            ("tls: handshake failure", True),
            ("couldn't find output binding eventstore", True),
            (_AMBIGUOUS, False),
            ("received status code 401", False),
            (
                "context deadline exceeded (Client.Timeout exceeded while awaiting headers)",
                False,
            ),
        ],
    )
    def test_non_delivery_classifier(self, text, expected):
        assert events_module._proves_non_delivery(BindingError(text)) is expected
