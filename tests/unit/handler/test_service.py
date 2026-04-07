"""Unit tests for the handler FastAPI service."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from fastapi.testclient import TestClient

from application_sdk.handler.base import Handler, HandlerError
from application_sdk.handler.contracts import (
    ApiMetadataObject,
    ApiMetadataOutput,
    AuthInput,
    AuthOutput,
    AuthStatus,
    EventFilterRule,
    EventTriggerConfig,
    MetadataInput,
    MetadataOutput,
    PreflightInput,
    PreflightOutput,
    PreflightStatus,
    SqlMetadataObject,
    SqlMetadataOutput,
    SubscriptionConfig,
)
from application_sdk.handler.service import (
    _normalize_credentials,
    _wrap_response,
    create_app_handler_service,
)

# ---------------------------------------------------------------------------
# Test Handler implementation
# ---------------------------------------------------------------------------


class _TestHandler(Handler):
    """Minimal handler for testing — returns SqlMetadataOutput."""

    async def test_auth(self, input: AuthInput) -> AuthOutput:
        return AuthOutput(status=AuthStatus.SUCCESS, message="auth ok")

    async def preflight_check(self, input: PreflightInput) -> PreflightOutput:
        return PreflightOutput(status=PreflightStatus.READY, message="ready")

    async def fetch_metadata(self, input: MetadataInput) -> MetadataOutput:
        return SqlMetadataOutput(objects=[])


class _ApiTreeHandler(Handler):
    """Handler that returns ApiMetadataOutput (BI connector path)."""

    async def test_auth(self, input: AuthInput) -> AuthOutput:
        return AuthOutput(status=AuthStatus.SUCCESS, message="auth ok")

    async def preflight_check(self, input: PreflightInput) -> PreflightOutput:
        return PreflightOutput(status=PreflightStatus.READY, message="ready")

    async def fetch_metadata(self, input: MetadataInput) -> MetadataOutput:
        return ApiMetadataOutput(objects=[
            ApiMetadataObject(value="tag-1", title="Tag One", node_type="tag"),
            ApiMetadataObject(value="tag-2", title="Tag Two", node_type="tag"),
        ])


class _FailingHandler(Handler):
    """Handler that raises HandlerError."""

    async def test_auth(self, input: AuthInput) -> AuthOutput:
        raise HandlerError("auth failed")

    async def preflight_check(self, input: PreflightInput) -> PreflightOutput:
        raise HandlerError("preflight failed")

    async def fetch_metadata(self, input: MetadataInput) -> MetadataOutput:
        raise HandlerError("metadata failed")


# ---------------------------------------------------------------------------
# Helper: create test client
# ---------------------------------------------------------------------------


def _make_client(handler: Handler | None = None) -> TestClient:
    handler = handler or _TestHandler()
    app = create_app_handler_service(handler, app_name="test-app")
    return TestClient(app)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestHealthEndpoints:
    """Tests for /health and /server/ready."""

    def test_health_endpoint_returns_200(self) -> None:
        client = _make_client()
        response = client.get("/health")
        assert response.status_code == 200
        assert response.json() == {"status": "healthy"}

    def test_server_health_alias_returns_200(self) -> None:
        client = _make_client()
        response = client.get("/server/health")
        assert response.status_code == 200
        assert response.json() == {"status": "healthy"}

    def test_ready_endpoint_returns_200(self) -> None:
        client = _make_client()
        response = client.get("/server/ready")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}

    def test_ready_alias_returns_200(self) -> None:
        client = _make_client()
        response = client.get("/ready")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}


class TestAuthEndpoint:
    """Tests for POST /workflows/v1/auth."""

    def test_auth_success(self) -> None:
        client = _make_client()
        response = client.post(
            "/workflows/v1/auth",
            json={"credentials": [], "connection_id": "test-conn"},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["success"] is True
        assert body["data"]["status"] == "success"

    def test_auth_handler_error_returns_500(self) -> None:
        client = _make_client(handler=_FailingHandler())
        response = client.post(
            "/workflows/v1/auth",
            json={"credentials": []},
        )
        assert response.status_code == 500

    def test_auth_response_has_envelope_format(self) -> None:
        client = _make_client()
        response = client.post(
            "/workflows/v1/auth",
            json={"credentials": []},
        )
        body = response.json()
        assert "success" in body
        assert "message" in body
        assert "data" in body

    def test_auth_with_credentials_succeeds(self) -> None:
        client = _make_client()
        response = client.post(
            "/workflows/v1/auth",
            json={
                "credentials": [{"key": "api_key", "value": "secret123"}],
            },
        )
        assert response.status_code == 200


class TestPreflightEndpoint:
    """Tests for POST /workflows/v1/check."""

    def test_preflight_success(self) -> None:
        client = _make_client()
        response = client.post(
            "/workflows/v1/check",
            json={"credentials": []},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["success"] is True
        # v2 format: data is a dict of check results keyed by camelCase name.
        # _TestHandler returns no checks, so data is empty.
        assert body["data"] == {}
        assert body["message"] == "ready"

    def test_preflight_handler_error_returns_500(self) -> None:
        client = _make_client(handler=_FailingHandler())
        response = client.post(
            "/workflows/v1/check",
            json={"credentials": []},
        )
        assert response.status_code == 500


class TestMetadataEndpoint:
    """Tests for POST /workflows/v1/metadata."""

    def test_metadata_sql_empty_returns_empty_list(self) -> None:
        """SqlMetadataOutput with no objects → empty list in data."""
        client = _make_client()
        response = client.post(
            "/workflows/v1/metadata",
            json={"credentials": []},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["success"] is True
        assert body["data"] == []

    def test_metadata_sql_returns_flat_rows(self) -> None:
        """SqlMetadataOutput → [{TABLE_CATALOG, TABLE_SCHEMA}] for sqltree widget."""

        class _SQLHandler(_TestHandler):
            async def fetch_metadata(self, input: MetadataInput) -> MetadataOutput:
                return SqlMetadataOutput(objects=[
                    SqlMetadataObject(TABLE_CATALOG="DEFAULT", TABLE_SCHEMA="FINANCE"),
                    SqlMetadataObject(TABLE_CATALOG="DEFAULT", TABLE_SCHEMA="SALES"),
                ])

        client = _make_client(handler=_SQLHandler())
        response = client.post(
            "/workflows/v1/metadata",
            json={"credentials": []},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["success"] is True
        assert body["data"] == [
            {"TABLE_CATALOG": "DEFAULT", "TABLE_SCHEMA": "FINANCE"},
            {"TABLE_CATALOG": "DEFAULT", "TABLE_SCHEMA": "SALES"},
        ]
        assert body["message"] == "Fetched 2 objects"

    def test_metadata_api_returns_tree_nodes(self) -> None:
        """ApiMetadataOutput → [{value, title, node_type, children}] for apitree widget."""
        client = _make_client(handler=_ApiTreeHandler())
        response = client.post(
            "/workflows/v1/metadata",
            json={"credentials": []},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["success"] is True
        assert body["data"] == [
            {"value": "tag-1", "title": "Tag One", "node_type": "tag", "children": []},
            {"value": "tag-2", "title": "Tag Two", "node_type": "tag", "children": []},
        ]
        assert body["message"] == "Fetched 2 objects"

    def test_metadata_api_nested_children(self) -> None:
        """ApiMetadataOutput with nested children serializes the full tree."""

        class _NestedHandler(_TestHandler):
            async def fetch_metadata(self, input: MetadataInput) -> MetadataOutput:
                return ApiMetadataOutput(objects=[
                    ApiMetadataObject(
                        value="proj-1", title="Project A", node_type="project",
                        children=[
                            ApiMetadataObject(value="ws-1", title="Workspace 1", node_type="workspace"),
                            ApiMetadataObject(value="ws-2", title="Workspace 2", node_type="workspace"),
                        ],
                    ),
                ])

        client = _make_client(handler=_NestedHandler())
        response = client.post(
            "/workflows/v1/metadata",
            json={"credentials": []},
        )
        assert response.status_code == 200
        data = response.json()["data"]
        assert len(data) == 1
        assert data[0]["value"] == "proj-1"
        assert len(data[0]["children"]) == 2
        assert data[0]["children"][0]["value"] == "ws-1"
        assert data[0]["children"][1]["node_type"] == "workspace"

    def test_metadata_handler_error_returns_500(self) -> None:
        client = _make_client(handler=_FailingHandler())
        response = client.post(
            "/workflows/v1/metadata",
            json={"credentials": []},
        )
        assert response.status_code == 500


class TestStartWorkflowEndpoint:
    """Tests for POST /workflows/v1/start."""

    def test_start_workflow_without_temporal_config_returns_503(self) -> None:
        # Service created without app_class → not configured
        client = _make_client()
        response = client.post(
            "/workflows/v1/start",
            json={"name": "test"},
        )
        assert response.status_code == 503

    def test_start_workflow_with_app_class_but_no_temporal_host_returns_503(
        self,
    ) -> None:
        from application_sdk.app.base import App
        from application_sdk.app.registry import AppRegistry, TaskRegistry
        from application_sdk.contracts.base import Input, Output

        AppRegistry.reset()
        TaskRegistry.reset()

        try:

            @dataclass
            class _StartInput(Input, allow_unbounded_fields=True):
                name: str = ""

            @dataclass
            class _StartOutput(Output, allow_unbounded_fields=True):
                result: str = ""

            class _StartApp(App):
                async def run(self, input: _StartInput) -> _StartOutput:
                    return _StartOutput()

            # No temporal_host → is_configured() returns False
            handler = _TestHandler()
            app = create_app_handler_service(
                handler,
                app_name="start-test",
                app_class=_StartApp,
                temporal_host="",  # empty → not configured
            )
            client = TestClient(app)
            response = client.post("/workflows/v1/start", json={"name": "world"})
            assert response.status_code == 503
        finally:
            AppRegistry.reset()
            TaskRegistry.reset()


class TestWrapResponse:
    """Tests for _wrap_response helper."""

    def test_basic_structure(self) -> None:
        result = _wrap_response({"key": "value"})
        assert result["success"] is True
        assert result["message"] == ""
        assert result["data"] == {"key": "value"}

    def test_custom_message(self) -> None:
        result = _wrap_response({}, message="All good")
        assert result["message"] == "All good"

    def test_failure_response(self) -> None:
        result = _wrap_response({"error": "oops"}, success=False, message="failed")
        assert result["success"] is False
        assert result["message"] == "failed"


class TestRunIdPathParam:
    """Tests for stop and status endpoints using slash-containing run_ids."""

    def test_status_with_slash_run_id(self) -> None:
        # Without a configured Temporal client the service returns 503,
        # but the route itself must resolve (not 404/405).
        client = _make_client()
        response = client.get("/workflows/v1/status/my-workflow/some/slashed/run-id")
        # 503 = route resolved but Temporal not configured; proves :path matched
        assert response.status_code == 503

    def test_stop_with_slash_run_id(self) -> None:
        client = _make_client()
        response = client.post("/workflows/v1/stop/my-workflow/some/slashed/run-id")
        assert response.status_code == 503


class TestConfigMapEndpoints:
    """Tests for GET /workflows/v1/configmap/{id} and /configmaps."""

    def test_configmap_not_found_returns_empty(self, tmp_path: Path) -> None:
        from application_sdk.handler import service as svc_module

        original = svc_module.CONTRACT_GENERATED_DIR
        svc_module.CONTRACT_GENERATED_DIR = tmp_path
        try:
            client = _make_client()
            response = client.get("/workflows/v1/configmap/nonexistent")
            assert response.status_code == 200
            body = response.json()
            assert body["data"] == {}
        finally:
            svc_module.CONTRACT_GENERATED_DIR = original

    def test_configmap_returns_wrapped_k8s_shape(self, tmp_path: Path) -> None:
        from application_sdk.handler import service as svc_module

        raw = {"config": {"key": "value"}}
        (tmp_path / "my-config.json").write_text(json.dumps(raw))

        original = svc_module.CONTRACT_GENERATED_DIR
        svc_module.CONTRACT_GENERATED_DIR = tmp_path
        try:
            client = _make_client()
            response = client.get("/workflows/v1/configmap/my-config")
            assert response.status_code == 200
            data = response.json()["data"]
            assert data["kind"] == "ConfigMap"
            assert data["apiVersion"] == "v1"
            assert data["metadata"]["name"] == "my-config"
            parsed_config = json.loads(data["data"]["config"])
            assert parsed_config == {"key": "value"}
        finally:
            svc_module.CONTRACT_GENERATED_DIR = original

    def test_configmaps_lists_stems_excluding_manifest(self, tmp_path: Path) -> None:
        from application_sdk.handler import service as svc_module

        (tmp_path / "config-a.json").write_text("{}")
        (tmp_path / "config-b.json").write_text("{}")
        (tmp_path / "manifest.json").write_text("{}")

        original = svc_module.CONTRACT_GENERATED_DIR
        svc_module.CONTRACT_GENERATED_DIR = tmp_path
        try:
            client = _make_client()
            response = client.get("/workflows/v1/configmaps")
            assert response.status_code == 200
            configmaps = response.json()["data"]["configmaps"]
            assert "config-a" in configmaps
            assert "config-b" in configmaps
            assert "manifest" not in configmaps
        finally:
            svc_module.CONTRACT_GENERATED_DIR = original

    def test_configmaps_empty_when_dir_missing(self, tmp_path: Path) -> None:
        from application_sdk.handler import service as svc_module

        missing = tmp_path / "nonexistent"
        original = svc_module.CONTRACT_GENERATED_DIR
        svc_module.CONTRACT_GENERATED_DIR = missing
        try:
            client = _make_client()
            response = client.get("/workflows/v1/configmaps")
            assert response.status_code == 200
            assert response.json()["data"]["configmaps"] == []
        finally:
            svc_module.CONTRACT_GENERATED_DIR = original


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


class TestManifestEndpoint:
    """Tests for GET /workflows/v1/manifest."""

    def _make_manifest(self):
        from application_sdk.handler.manifest import (
            AppManifest,
            DagNode,
            DagNodeDependency,
            ExecuteWorkflowInputs,
        )

        return AppManifest(
            execution_mode="dag",
            dag={
                "extract": DagNode(
                    activity_name="execute_workflow",
                    activity_display_name="Extract",
                    app_name="my-extractor",
                    inputs=ExecuteWorkflowInputs(
                        workflow_type="extraction",
                        task_queue="my-extractor-queue",
                        args={"connection": "{{connection}}"},
                    ),
                ),
                "load": DagNode(
                    activity_name="execute_workflow",
                    activity_display_name="Load",
                    app_name="my-loader",
                    inputs=ExecuteWorkflowInputs(
                        workflow_type="loading",
                        task_queue="my-loader-queue",
                    ),
                    depends_on=DagNodeDependency(node_id="extract"),
                ),
            },
            init_endpoint="/workflows/v1/init",
        )

    def test_manifest_returns_programmatic_manifest(self) -> None:
        manifest = self._make_manifest()
        app = create_app_handler_service(
            _TestHandler(), app_name="test-app", manifest=manifest
        )
        client = TestClient(app)
        response = client.get("/workflows/v1/manifest")
        assert response.status_code == 200
        body = response.json()
        assert body["execution_mode"] == "dag"
        assert "extract" in body["dag"]
        assert body["dag"]["extract"]["app_name"] == "my-extractor"
        assert body["dag"]["load"]["depends_on"]["node_id"] == "extract"
        assert body["init_endpoint"] == "/workflows/v1/init"

    def test_manifest_falls_back_to_disk(self, tmp_path: Path) -> None:
        from application_sdk.handler import service as svc_module

        manifest_data = {
            "execution_mode": "dag",
            "dag": {
                "extract": {
                    "activity_name": "execute_workflow",
                    "activity_display_name": "Extract",
                    "app_name": "disk-app",
                    "inputs": {
                        "workflow_type": "extraction",
                        "task_queue": "disk-queue",
                    },
                }
            },
        }
        (tmp_path / "manifest.json").write_text(__import__("json").dumps(manifest_data))

        original = svc_module.CONTRACT_GENERATED_DIR
        svc_module.CONTRACT_GENERATED_DIR = tmp_path
        try:
            client = _make_client()
            response = client.get("/workflows/v1/manifest")
            assert response.status_code == 200
            body = response.json()
            assert body["execution_mode"] == "dag"
            assert body["dag"]["extract"]["app_name"] == "disk-app"
        finally:
            svc_module.CONTRACT_GENERATED_DIR = original

    def test_manifest_disk_substitutes_deployment_name(self, tmp_path: Path) -> None:
        from application_sdk.handler import service as svc_module

        manifest_data = {
            "execution_mode": "dag",
            "dag": {
                "extract": {
                    "activity_name": "execute_workflow",
                    "activity_display_name": "Extract",
                    "app_name": "my-app",
                    "inputs": {
                        "workflow_type": "extraction",
                        "task_queue": "{deployment_name}-queue",
                    },
                }
            },
        }
        (tmp_path / "manifest.json").write_text(__import__("json").dumps(manifest_data))

        original_dir = svc_module.CONTRACT_GENERATED_DIR
        original_dep = svc_module.DEPLOYMENT_NAME
        svc_module.CONTRACT_GENERATED_DIR = tmp_path
        svc_module.DEPLOYMENT_NAME = "prod-deploy"
        try:
            client = _make_client()
            response = client.get("/workflows/v1/manifest")
            assert response.status_code == 200
            body = response.json()
            assert body["dag"]["extract"]["inputs"]["task_queue"] == "prod-deploy-queue"
        finally:
            svc_module.CONTRACT_GENERATED_DIR = original_dir
            svc_module.DEPLOYMENT_NAME = original_dep

    def test_manifest_programmatic_takes_priority(self, tmp_path: Path) -> None:
        """When both programmatic and disk manifest exist, programmatic wins."""
        from application_sdk.handler import service as svc_module

        disk_data = {
            "execution_mode": "disk-mode",
            "dag": {
                "node": {
                    "activity_name": "execute_workflow",
                    "activity_display_name": "Node",
                    "app_name": "disk-app",
                    "inputs": {"workflow_type": "t", "task_queue": "q"},
                }
            },
        }
        (tmp_path / "manifest.json").write_text(__import__("json").dumps(disk_data))

        manifest = self._make_manifest()
        app = create_app_handler_service(
            _TestHandler(), app_name="test-app", manifest=manifest
        )
        client = TestClient(app)

        original = svc_module.CONTRACT_GENERATED_DIR
        svc_module.CONTRACT_GENERATED_DIR = tmp_path
        try:
            response = client.get("/workflows/v1/manifest")
            assert response.status_code == 200
            body = response.json()
            # Programmatic manifest wins — has "dag" mode, not "disk-mode"
            assert body["execution_mode"] == "dag"
            assert "extract" in body["dag"]
        finally:
            svc_module.CONTRACT_GENERATED_DIR = original

    def test_manifest_404_when_none(self, tmp_path: Path) -> None:
        """Returns 404 when no manifest param and no manifest.json on disk."""
        from application_sdk.handler import service as svc_module

        missing = tmp_path / "nonexistent"
        original = svc_module.CONTRACT_GENERATED_DIR
        svc_module.CONTRACT_GENERATED_DIR = missing
        try:
            client = _make_client()
            response = client.get("/workflows/v1/manifest")
            assert response.status_code == 404
        finally:
            svc_module.CONTRACT_GENERATED_DIR = original


class TestNormalizeCredentials:
    """Tests for _normalize_credentials v2→v3 compat shim."""

    def test_v3_list_passthrough(self) -> None:
        body = {"credentials": [{"key": "host", "value": "localhost"}]}
        result = _normalize_credentials(body)
        assert result["credentials"] == [{"key": "host", "value": "localhost"}]

    def test_v3_empty_list_passthrough(self) -> None:
        body = {"credentials": []}
        result = _normalize_credentials(body)
        assert result["credentials"] == []

    def test_missing_credentials_passthrough(self) -> None:
        body = {"other_key": "value"}
        result = _normalize_credentials(body)
        assert result == {"other_key": "value"}

    def test_null_credentials_passthrough(self) -> None:
        body = {"credentials": None}
        result = _normalize_credentials(body)
        assert result["credentials"] is None

    def test_v2_dict_conversion(self) -> None:
        body = {
            "credentials": {
                "host": "app.mode.com",
                "username": "user1",
                "password": "secret",
            }
        }
        result = _normalize_credentials(body)
        creds = result["credentials"]
        assert isinstance(creds, list)
        keys = {c["key"]: c["value"] for c in creds}
        assert keys["host"] == "app.mode.com"
        assert keys["username"] == "user1"
        assert keys["password"] == "secret"

    def test_v2_extra_flattening(self) -> None:
        body = {
            "credentials": {
                "host": "app.mode.com",
                "extra": {"workspace": "atlan", "region": "us"},
            }
        }
        result = _normalize_credentials(body)
        keys = {c["key"]: c["value"] for c in result["credentials"]}
        assert keys["host"] == "app.mode.com"
        assert keys["extra.workspace"] == "atlan"
        assert keys["extra.region"] == "us"
        assert "extra" not in keys

    def test_none_values_excluded(self) -> None:
        body = {"credentials": {"host": "localhost", "port": None}}
        result = _normalize_credentials(body)
        keys = [c["key"] for c in result["credentials"]]
        assert "host" in keys
        assert "port" not in keys

    def test_non_string_values_serialized_as_json(self) -> None:
        body = {
            "credentials": {
                "host": "localhost",
                "port": 5432,
                "ssl": True,
                "options": {"timeout": 30},
            }
        }
        result = _normalize_credentials(body)
        keys = {c["key"]: c["value"] for c in result["credentials"]}
        assert keys["host"] == "localhost"
        assert keys["port"] == "5432"
        assert keys["ssl"] == "true"
        assert keys["options"] == '{"timeout": 30}'

    def test_non_dict_extra_ignored(self) -> None:
        body = {"credentials": {"host": "localhost", "extra": "not-a-dict"}}
        result = _normalize_credentials(body)
        keys = {c["key"]: c["value"] for c in result["credentials"]}
        assert keys["host"] == "localhost"
        assert "extra" not in keys

    def test_does_not_mutate_original_body(self) -> None:
        body = {
            "credentials": {"host": "localhost"},
            "metadata": {"key": "value"},
        }
        original_meta = body["metadata"]
        result = _normalize_credentials(body)
        assert result is not body
        assert result["metadata"] is original_meta
        assert isinstance(body["credentials"], dict)

    def test_preserves_other_body_fields(self) -> None:
        body = {
            "credentials": {"host": "localhost"},
            "connection_id": "conn-123",
            "timeout_seconds": 60,
        }
        result = _normalize_credentials(body)
        assert result["connection_id"] == "conn-123"
        assert result["timeout_seconds"] == 60

    def test_v2_auth_integration(self) -> None:
        client = _make_client()
        response = client.post(
            "/workflows/v1/auth",
            json={
                "credentials": {
                    "host": "app.mode.com",
                    "username": "user",
                    "password": "pass",
                    "extra": {"workspace": "ws"},
                }
            },
        )
        assert response.status_code == 200
        assert response.json()["data"]["status"] == "success"

    # ── Flat top-level credential format (Heracles credential test) ──────

    def test_flat_toplevel_conversion(self) -> None:
        """Heracles sends flat top-level keys: {"host": ..., "authType": ...}."""
        body = {
            "host": "myns.servicebus.windows.net:9093",
            "port": 9093,
            "authType": "basic",
            "username": "$ConnectionString",
            "password": "Endpoint=sb://myns/;SharedAccessKeyName=key;SharedAccessKey=secret",
            "extra": {"security_protocol": "SASL_SSL"},
            "connectorConfigName": "atlan-connectors-azure-event-hub",
        }
        result = _normalize_credentials(body)
        creds = result["credentials"]
        assert isinstance(creds, list)
        keys = {c["key"]: c["value"] for c in creds}
        assert keys["host"] == "myns.servicebus.windows.net:9093"
        assert keys["authType"] == "basic"
        assert keys["username"] == "$ConnectionString"
        assert "Endpoint=sb://" in keys["password"]
        assert keys["extra.security_protocol"] == "SASL_SSL"
        assert keys["port"] == "9093"

    def test_flat_toplevel_no_extra(self) -> None:
        body = {"host": "localhost", "authType": "basic", "password": "secret"}
        result = _normalize_credentials(body)
        creds = result["credentials"]
        assert isinstance(creds, list)
        keys = {c["key"]: c["value"] for c in creds}
        assert keys["host"] == "localhost"
        assert keys["authType"] == "basic"

    def test_flat_toplevel_preserves_other_fields(self) -> None:
        body = {
            "host": "localhost",
            "authType": "basic",
            "connection_id": "conn-123",
            "timeout_seconds": 30,
        }
        result = _normalize_credentials(body)
        assert result["connection_id"] == "conn-123"
        assert result["timeout_seconds"] == 30
        assert isinstance(result["credentials"], list)

    def test_no_credential_keys_passthrough(self) -> None:
        """Body with no known credential keys should pass through unchanged."""
        body = {"connection_id": "conn-123", "metadata": {"key": "value"}}
        result = _normalize_credentials(body)
        assert "credentials" not in result
        assert result == body

    def test_flat_toplevel_auth_integration(self) -> None:
        client = _make_client()
        response = client.post(
            "/workflows/v1/auth",
            json={
                "host": "app.mode.com",
                "authType": "basic",
                "username": "user",
                "password": "pass",
                "extra": {"workspace": "ws"},
                "connectorConfigName": "test-connector",
            },
        )
        assert response.status_code == 200
        assert response.json()["data"]["status"] == "success"
