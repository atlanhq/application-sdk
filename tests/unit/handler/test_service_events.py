"""Handler service tests: Dapr subscriptions and the event-trigger endpoint."""

from __future__ import annotations

from unittest.mock import patch

from fastapi.testclient import TestClient

from application_sdk.handler.contracts import (
    EventFilterRule,
    EventTriggerConfig,
    SubscriptionConfig,
)
from application_sdk.handler.service import (
    create_app_handler_service,
)
from tests.unit.handler._service_support import (
    _make_client,
    _RoutingInput,
    _RoutingOutput,
    _TestHandler,
)


class TestDaprSubscribeEndpoint:
    """Tests for GET /dapr/subscribe."""

    def test_subscribe_empty_when_no_triggers_or_subs(self) -> None:
        client = _make_client()
        response = client.get("/dapr/subscribe")
        assert response.status_code == 200
        assert response.json() == []

    def test_subscribe_includes_event_trigger(self) -> None:
        handler = _TestHandler()
        trigger = EventTriggerConfig(
            event_id="my-trigger",
            event_type="metadata_extraction",
            event_name="extraction_requested",
            event_filters=[
                EventFilterRule(path="event.data.type", operator="==", value="meta")
            ],
        )
        app = create_app_handler_service(
            handler, app_name="test-app", event_triggers=[trigger]
        )
        client = TestClient(app)
        response = client.get("/dapr/subscribe")
        assert response.status_code == 200
        subs = response.json()
        assert len(subs) == 1
        sub = subs[0]
        assert sub["topic"] == "metadata_extraction"
        routes = sub["routes"]
        assert routes["rules"][0]["path"] == "/events/v1/event/my-trigger"
        assert routes["default"] == "/events/v1/drop"

    def test_subscribe_includes_subscription_config(self) -> None:
        handler = _TestHandler()

        async def my_handler(request: object) -> dict:
            return {"status": "SUCCESS"}

        sub = SubscriptionConfig(
            component_name="my-pubsub",
            topic="my-topic",
            route="my-route",
            handler=my_handler,
        )
        app = create_app_handler_service(
            handler, app_name="test-app", subscriptions=[sub]
        )
        client = TestClient(app)
        response = client.get("/dapr/subscribe")
        assert response.status_code == 200
        subs = response.json()
        assert len(subs) == 1
        assert subs[0]["pubsubname"] == "my-pubsub"
        assert subs[0]["topic"] == "my-topic"
        assert subs[0]["route"] == "/subscriptions/v1/my-route"


class TestDropEventEndpoint:
    """Tests for POST /events/v1/drop."""

    def test_drop_returns_drop_status(self) -> None:
        client = _make_client()
        response = client.post("/events/v1/drop")
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "DROP"
        assert body["success"] is False


class TestDynamicSubscriptionRoutes:
    """Tests for dynamic /subscriptions/v1/{route} endpoints."""

    def test_subscription_route_is_registered(self) -> None:
        handler = _TestHandler()

        async def my_handler() -> dict:
            return {"status": "SUCCESS"}

        sub = SubscriptionConfig(
            component_name="pubsub",
            topic="topic",
            route="my-handler",
            handler=my_handler,
        )
        app = create_app_handler_service(
            handler, app_name="test-app", subscriptions=[sub]
        )
        client = TestClient(app)
        response = client.post("/subscriptions/v1/my-handler")
        assert response.status_code == 200
        assert response.json() == {"status": "SUCCESS"}


class TestEventTriggerEndpoint:
    """Tests for POST /events/v1/event/{event_id}."""

    def _make_event_app(self):
        from application_sdk.app.base import App
        from application_sdk.app.registry import AppRegistry, TaskRegistry

        AppRegistry.reset()
        TaskRegistry.reset()

        class _EvApp(App):
            async def run(self, input: _RoutingInput) -> _RoutingOutput:
                return _RoutingOutput()

        # Tag the app class so the event handler can find _input_type
        _EvApp._input_type = _RoutingInput
        return _EvApp

    def _teardown(self) -> None:
        from application_sdk.app.registry import AppRegistry, TaskRegistry

        AppRegistry.reset()
        TaskRegistry.reset()

    def test_event_503_when_temporal_unconfigured(self) -> None:
        from application_sdk.handler.contracts import EventTriggerConfig

        trigger = EventTriggerConfig(event_id="t1", event_type="topic", event_name="ev")
        app = create_app_handler_service(
            _TestHandler(), app_name="ev-test", event_triggers=[trigger]
        )
        client = TestClient(app)
        response = client.post("/events/v1/event/t1", json={})
        assert response.status_code == 503

    def test_event_unknown_id_returns_404(self) -> None:
        from application_sdk.handler.contracts import EventTriggerConfig

        try:
            app_cls = self._make_event_app()
            trigger = EventTriggerConfig(
                event_id="known", event_type="topic", event_name="ev"
            )
            app = create_app_handler_service(
                _TestHandler(),
                app_name="ev-test",
                app_class=app_cls,
                temporal_host="t:7233",
                event_triggers=[trigger],
            )
            client = TestClient(app)
            response = client.post("/events/v1/event/unknown", json={})
            assert response.status_code == 404
            assert "unknown" in response.json()["detail"]
        finally:
            self._teardown()

    def test_event_invalid_json_returns_400(self) -> None:
        from application_sdk.handler.contracts import EventTriggerConfig

        try:
            app_cls = self._make_event_app()
            trigger = EventTriggerConfig(
                event_id="t1", event_type="topic", event_name="ev"
            )
            app = create_app_handler_service(
                _TestHandler(),
                app_name="ev-test",
                app_class=app_cls,
                temporal_host="t:7233",
                event_triggers=[trigger],
            )
            client = TestClient(app)
            response = client.post(
                "/events/v1/event/t1",
                content=b"not-valid-json",
                headers={"content-type": "application/json"},
            )
            assert response.status_code == 400
        finally:
            self._teardown()

    def test_event_happy_path_starts_workflow(self) -> None:
        from unittest.mock import AsyncMock, MagicMock

        from application_sdk.handler.contracts import EventTriggerConfig

        try:
            app_cls = self._make_event_app()
            trigger = EventTriggerConfig(
                event_id="t1", event_type="topic", event_name="ev"
            )
            app = create_app_handler_service(
                _TestHandler(),
                app_name="ev-test",
                app_class=app_cls,
                temporal_host="t:7233",
                event_triggers=[trigger],
            )
            mock_handle = MagicMock()
            mock_handle.id = "wf-1"
            mock_handle.result_run_id = "run-1"
            mock_client = MagicMock()
            mock_client.start_workflow = AsyncMock(return_value=mock_handle)
            with patch(
                "application_sdk.handler.service._get_temporal_client",
                new=AsyncMock(return_value=mock_client),
            ):
                client = TestClient(app)
                response = client.post(
                    "/events/v1/event/t1",
                    json={"data": {"name": "from-event"}},
                )
            assert response.status_code == 200
            body = response.json()
            assert body["status"] == "SUCCESS"
            assert body["workflow_id"] == "wf-1"
            # CNCT-104: event-triggered starts must carry a correlation memo
            # (previously none was set → the LogInterceptor minted an identity
            # the run page never queries → the run's logs were unfindable).
            kwargs = mock_client.start_workflow.await_args.kwargs
            memo = kwargs["memo"]
            # A real UUID was minted (not merely any truthy string), matching
            # the rigor of the event-supplied sibling test below.
            import uuid as _uuid

            assert len(memo["correlation_id"]) == 36
            assert _uuid.UUID(memo["correlation_id"])  # raises if not a valid UUID
            # The minted id is returned to the caller so the run's logs are
            # addressable, and it matches what was stamped on the input.
            assert body["correlation_id"] == memo["correlation_id"]
            input_arg = kwargs["args"][0]
            assert input_arg.correlation_id == memo["correlation_id"]
        finally:
            self._teardown()

    def test_event_dispatches_the_entry_points_registered_type(self) -> None:
        """Event starts must use the entry point's registered workflow type.

        The bare app name is registered only when the app derives its entry
        point from run(); an @entrypoint app registers "{app}:{ep}", so the
        start succeeded, no worker claimed it, and the run sat open until the
        execution timeout.
        """
        from unittest.mock import AsyncMock, MagicMock

        from application_sdk.app.base import App
        from application_sdk.app.entrypoint import entrypoint
        from application_sdk.app.registry import AppRegistry, TaskRegistry
        from application_sdk.handler.contracts import EventTriggerConfig

        try:
            AppRegistry.reset()
            TaskRegistry.reset()

            class _EvEpApp(App):
                name = "ev-ep"
                legacy_workflow_types = {"LegacyEventWorkflow": "ingest"}

                @entrypoint(default=True)
                async def ingest(self, input: _RoutingInput) -> _RoutingOutput:
                    return _RoutingOutput()

            trigger = EventTriggerConfig(
                event_id="t1", event_type="topic", event_name="ev"
            )
            app = create_app_handler_service(
                _TestHandler(),
                app_name="ev-ep",
                app_class=_EvEpApp,
                temporal_host="t:7233",
                event_triggers=[trigger],
            )
            mock_handle = MagicMock()
            mock_handle.id = "wf-1"
            mock_handle.result_run_id = "run-1"
            mock_client = MagicMock()
            mock_client.start_workflow = AsyncMock(return_value=mock_handle)
            with patch(
                "application_sdk.handler.service._get_temporal_client",
                new=AsyncMock(return_value=mock_client),
            ):
                client = TestClient(app)
                response = client.post(
                    "/events/v1/event/t1", json={"data": {"name": "x"}}
                )
            assert response.status_code == 200
            dispatched = mock_client.start_workflow.await_args.args[0]
            assert dispatched == "ev-ep:ingest"
            registered = set(AppRegistry.get_instance().get("ev-ep").workflow_types)
            assert dispatched in registered
        finally:
            self._teardown()

    def test_event_respects_event_supplied_correlation_id(self) -> None:
        # An event payload that carries correlation_id keeps it (caller wins),
        # mirroring the /workflows/v1/start contract.
        from unittest.mock import AsyncMock, MagicMock

        from application_sdk.handler.contracts import EventTriggerConfig

        try:
            app_cls = self._make_event_app()
            trigger = EventTriggerConfig(
                event_id="t1", event_type="topic", event_name="ev"
            )
            app = create_app_handler_service(
                _TestHandler(),
                app_name="ev-test",
                app_class=app_cls,
                temporal_host="t:7233",
                event_triggers=[trigger],
            )
            mock_handle = MagicMock()
            mock_handle.id = "wf-1"
            mock_handle.result_run_id = "run-1"
            mock_client = MagicMock()
            mock_client.start_workflow = AsyncMock(return_value=mock_handle)
            with patch(
                "application_sdk.handler.service._get_temporal_client",
                new=AsyncMock(return_value=mock_client),
            ):
                client = TestClient(app)
                response = client.post(
                    "/events/v1/event/t1",
                    json={"data": {"name": "x", "correlation_id": "event-corr"}},
                )
            assert response.status_code == 200
            assert response.json()["correlation_id"] == "event-corr"
            kwargs = mock_client.start_workflow.await_args.kwargs
            assert kwargs["memo"] == {"correlation_id": "event-corr"}
        finally:
            self._teardown()

    def test_event_temporal_failure_returns_retry_status(self) -> None:
        """When Temporal start fails, the endpoint returns 500 + RETRY so Dapr
        will retry the message — per the bus contract."""
        from unittest.mock import AsyncMock, MagicMock

        from application_sdk.handler.contracts import EventTriggerConfig

        try:
            app_cls = self._make_event_app()
            trigger = EventTriggerConfig(
                event_id="t1", event_type="topic", event_name="ev"
            )
            app = create_app_handler_service(
                _TestHandler(),
                app_name="ev-test",
                app_class=app_cls,
                temporal_host="t:7233",
                event_triggers=[trigger],
            )
            mock_client = MagicMock()
            mock_client.start_workflow = AsyncMock(
                side_effect=RuntimeError("temporal down")
            )
            with patch(
                "application_sdk.handler.service._get_temporal_client",
                new=AsyncMock(return_value=mock_client),
            ):
                client = TestClient(app)
                response = client.post("/events/v1/event/t1", json={"data": {}})
            assert response.status_code == 500
            assert response.json()["status"] == "RETRY"
        finally:
            self._teardown()

    def test_event_execution_timeout_set_when_configured(self) -> None:
        """execution_timeout is passed to start_workflow on the event route when configured."""
        from datetime import timedelta
        from unittest.mock import AsyncMock, MagicMock

        from application_sdk.handler.contracts import EventTriggerConfig

        try:
            app_cls = self._make_event_app()
            trigger = EventTriggerConfig(
                event_id="t1", event_type="topic", event_name="ev"
            )
            app = create_app_handler_service(
                _TestHandler(),
                app_name="ev-test",
                app_class=app_cls,
                temporal_host="t:7233",
                event_triggers=[trigger],
                workflow_max_timeout_hours=48,
            )
            mock_client = MagicMock()
            mock_handle = MagicMock()
            mock_handle.id = "wf-id"
            mock_handle.result_run_id = "run-id"
            mock_client.start_workflow = AsyncMock(return_value=mock_handle)
            with patch(
                "application_sdk.handler.service._get_temporal_client",
                new=AsyncMock(return_value=mock_client),
            ):
                client = TestClient(app)
                response = client.post("/events/v1/event/t1", json={})
            assert response.status_code == 200
            kwargs = mock_client.start_workflow.call_args.kwargs
            assert kwargs["execution_timeout"] == timedelta(hours=48)
        finally:
            self._teardown()

    def test_event_execution_timeout_none_when_not_configured(self) -> None:
        """execution_timeout is None on the event route when workflow_max_timeout_hours is unset."""
        from unittest.mock import AsyncMock, MagicMock

        from application_sdk.handler.contracts import EventTriggerConfig

        try:
            app_cls = self._make_event_app()
            trigger = EventTriggerConfig(
                event_id="t1", event_type="topic", event_name="ev"
            )
            app = create_app_handler_service(
                _TestHandler(),
                app_name="ev-test",
                app_class=app_cls,
                temporal_host="t:7233",
                event_triggers=[trigger],
            )
            mock_client = MagicMock()
            mock_handle = MagicMock()
            mock_handle.id = "wf-id"
            mock_handle.result_run_id = "run-id"
            mock_client.start_workflow = AsyncMock(return_value=mock_handle)
            with patch(
                "application_sdk.handler.service._get_temporal_client",
                new=AsyncMock(return_value=mock_client),
            ):
                client = TestClient(app)
                response = client.post("/events/v1/event/t1", json={})
            assert response.status_code == 200
            kwargs = mock_client.start_workflow.call_args.kwargs
            assert kwargs["execution_timeout"] is None
        finally:
            self._teardown()
