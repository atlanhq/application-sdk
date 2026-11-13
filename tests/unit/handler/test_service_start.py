"""Handler service tests: starting a workflow: routing, invocability, config validation and response shape."""

from __future__ import annotations

import warnings
from dataclasses import dataclass
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from application_sdk.contracts.base import Input, Output
from application_sdk.handler.contracts import (
    ApiMetadataObject,
    ApiMetadataOutput,
    MetadataInput,
    MetadataOutput,
    SqlMetadataObject,
    SqlMetadataOutput,
)
from application_sdk.handler.service import (
    _normalize_credentials,
    _wrap_response,
    create_app_handler_service,
)
from tests.unit.handler._service_support import (
    _ApiTreeHandler,
    _FailingHandler,
    _make_client,
    _RoutingInput,
    _RoutingOutput,
    _SourceDownHandler,
    _TestHandler,
)


class TestMetadataEndpoint:
    """Tests for POST /workflows/v1/metadata."""

    def test_metadata_source_unavailable_returns_503(self) -> None:
        client = _make_client(handler=_SourceDownHandler())
        response = client.post("/workflows/v1/metadata", json={"credentials": []})
        assert response.status_code == 503
        assert response.json()["detail"] == "the warehouse did not answer"

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
                return SqlMetadataOutput(
                    objects=[
                        SqlMetadataObject(
                            TABLE_CATALOG="DEFAULT", TABLE_SCHEMA="FINANCE"
                        ),
                        SqlMetadataObject(
                            TABLE_CATALOG="DEFAULT", TABLE_SCHEMA="SALES"
                        ),
                    ]
                )

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
        assert (
            "message" not in body
        )  # omitted on success — fixes frontend filter dropdowns

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
        assert (
            "message" not in body
        )  # omitted on success — fixes frontend filter dropdowns

    def test_metadata_api_nested_children(self) -> None:
        """ApiMetadataOutput with nested children serializes the full tree."""

        class _NestedHandler(_TestHandler):
            async def fetch_metadata(self, input: MetadataInput) -> MetadataOutput:
                return ApiMetadataOutput(
                    objects=[
                        ApiMetadataObject(
                            value="proj-1",
                            title="Project A",
                            node_type="project",
                            children=[
                                ApiMetadataObject(
                                    value="ws-1",
                                    title="Workspace 1",
                                    node_type="workspace",
                                ),
                                ApiMetadataObject(
                                    value="ws-2",
                                    title="Workspace 2",
                                    node_type="workspace",
                                ),
                            ],
                        ),
                    ]
                )

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

    def test_metadata_template_key_forwarded_to_handler(self) -> None:
        received: list[str] = []

        class _MetadataTemplateCapture(_TestHandler):
            async def fetch_metadata(self, input: MetadataInput) -> MetadataOutput:
                received.append(input.metadata_template_key)
                return ApiMetadataOutput(objects=[])

        client = _make_client(handler=_MetadataTemplateCapture())
        response = client.post(
            "/workflows/v1/metadata",
            json={
                "credentials": {"host": "db.example.com"},
                "metadata_template_key": "feedbacks",
            },
        )

        assert response.status_code == 200
        assert received == ["feedbacks"]

    def test_metadata_wire_keys_populate_template_key(self) -> None:
        # The orchestrator's wire keys (``metadataTemplateKey`` / ``type``)
        # populate ``metadata_template_key`` via the field's validation alias —
        # the routing key's documented home, not object_filter punning.
        received: list[str] = []

        class _MetadataTemplateCapture(_TestHandler):
            async def fetch_metadata(self, input: MetadataInput) -> MetadataOutput:
                received.append(input.metadata_template_key)
                return ApiMetadataOutput(objects=[])

        client = _make_client(handler=_MetadataTemplateCapture())
        for key in ("metadataTemplateKey", "type"):
            response = client.post(
                "/workflows/v1/metadata",
                json={
                    "credentials": {"host": "db.example.com"},
                    key: "feedbacks",
                },
            )

            assert response.status_code == 200

        assert received == ["feedbacks", "feedbacks"]

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

    @pytest.mark.usefixtures("clean_app_registry", "clean_task_registry")
    def test_start_workflow_with_app_class_but_no_temporal_host_returns_503(
        self,
    ) -> None:
        from application_sdk.app.base import App

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


@pytest.mark.usefixtures("clean_app_registry", "clean_task_registry")
class TestErrorInfoDisclosure:
    """Regression: error responses must not leak internal exception details.

    The security fix removed exception interpolation from 4 HTTP error
    handlers. These tests verify the generic messages are returned.
    """

    def test_start_workflow_type_error_returns_generic_message(self) -> None:
        """TypeError handler on /start must not leak internal type info."""
        from unittest.mock import AsyncMock, MagicMock

        from application_sdk.app.base import App

        class _TypeErrorApp(App):
            async def run(self, input: _RoutingInput) -> _RoutingOutput:
                return _RoutingOutput()

        # A configured app whose Temporal start raises TypeError, so the
        # request reaches the ``except TypeError`` branch (not the 503 guard).
        leaked = "internal-type-detail-must-not-leak"
        temporal = MagicMock()
        temporal.start_workflow = AsyncMock(side_effect=TypeError(leaked))
        svc = create_app_handler_service(
            _TestHandler(),
            app_name="type-error-test",
            app_class=_TypeErrorApp,
            temporal_host="temporal:7233",
        )
        with patch(
            "application_sdk.handler.service._get_temporal_client",
            new=AsyncMock(return_value=temporal),
        ):
            response = TestClient(svc).post("/workflows/v1/start", json={"name": "x"})
        assert temporal.start_workflow.await_count == 1
        assert response.status_code == 400
        body_str = str(response.json())
        assert "Invalid input" in body_str
        assert leaked not in body_str
        assert "Traceback" not in body_str
        assert "TypeError" not in body_str

    def test_handler_error_500_does_not_leak_exception(self) -> None:
        """Handler endpoint errors should return generic messages."""
        client = _make_client(handler=_FailingHandler())
        response = client.post(
            "/workflows/v1/auth",
            json={"credentials": []},
        )
        assert response.status_code == 500
        body_str = str(response.json())
        assert "Traceback" not in body_str
        assert "File " not in body_str


@pytest.mark.usefixtures("clean_app_registry", "clean_task_registry")
class TestStartWorkflowRouting:
    """Tests for /workflows/v1/start entry-point routing logic."""

    def _make_routed_client(self, app_cls: type, *, patch_temporal: bool = True):  # type: ignore[return]
        """Create a TestClient wired to app_cls with Temporal mocked out."""
        from unittest.mock import AsyncMock, MagicMock

        handler = _TestHandler()
        svc = create_app_handler_service(
            handler,
            app_name="routing-test",
            app_class=app_cls,
            temporal_host="temporal:7233",
        )
        mock_client = MagicMock()
        mock_handle = MagicMock()
        mock_handle.id = "wf-123"
        mock_handle.result_run_id = "run-abc"
        mock_client.start_workflow = AsyncMock(return_value=mock_handle)
        patcher = patch(
            "application_sdk.handler.service._get_temporal_client",
            new=AsyncMock(return_value=mock_client),
        )
        patcher.start()
        client = TestClient(svc, raise_server_exceptions=False)
        return client, patcher

    def test_single_ep_no_workflow_type_auto_selects(self) -> None:
        """Single-entry-point app accepts request with no workflow_type."""
        # Use module-level types — locally-defined dataclasses can't be resolved
        # by get_type_hints() because 'from __future__ import annotations' is active.
        from application_sdk.app.base import App

        class _SingleEpApp(App):
            async def run(self, input: _RoutingInput) -> _RoutingOutput:
                return _RoutingOutput()

        client, patcher = self._make_routed_client(_SingleEpApp)
        try:
            response = client.post("/workflows/v1/start", json={"name": "hello"})
            assert response.status_code == 200
        finally:
            patcher.stop()

    def test_multi_ep_entrypoint_query_param_routes_correctly(self) -> None:
        """Multi-entry-point app with ?entrypoint= query param returns 200."""
        from application_sdk.app.base import App
        from application_sdk.app.entrypoint import entrypoint

        class _MultiEpApp(App):
            @entrypoint
            async def extract(self, input: _RoutingInput) -> _RoutingOutput:
                return _RoutingOutput()

            @entrypoint
            async def load(self, input: _RoutingInput) -> _RoutingOutput:
                return _RoutingOutput()

        client, patcher = self._make_routed_client(_MultiEpApp)
        try:
            response = client.post(
                "/workflows/v1/start?entrypoint=extract",
                json={"name": "x"},
            )
            assert response.status_code == 200
        finally:
            patcher.stop()

    def test_multi_ep_body_workflow_type_fallback_routes_correctly(self) -> None:
        """Multi-entry-point app falls back to body 'workflow_type' when ?entrypoint= absent."""
        from application_sdk.app.base import App
        from application_sdk.app.entrypoint import entrypoint

        class _MultiEpFallbackApp(App):
            @entrypoint
            async def extract(self, input: _RoutingInput) -> _RoutingOutput:
                return _RoutingOutput()

            @entrypoint
            async def load(self, input: _RoutingInput) -> _RoutingOutput:
                return _RoutingOutput()

        client, patcher = self._make_routed_client(_MultiEpFallbackApp)
        try:
            with pytest.warns(DeprecationWarning, match="workflow_type.*deprecated"):
                response = client.post(
                    "/workflows/v1/start",
                    json={"workflow_type": "extract", "name": "x"},
                )
            assert response.status_code == 200
        finally:
            patcher.stop()

    def test_body_workflow_type_alias_selects_but_dispatches_canonical(
        self,
    ) -> None:
        """D2: a legacy alias in the deprecated body field reaches its entry
        point, and the dispatch still emits the canonical type."""
        from unittest.mock import AsyncMock, MagicMock

        from application_sdk.app.base import App
        from application_sdk.app.entrypoint import entrypoint

        class _AliasApp(App):
            name = "routing-test"
            legacy_workflow_types = {"LegacyRoutingWorkflow": "legacy"}

            @entrypoint(name="legacy")
            async def legacy(self, input: _RoutingInput) -> _RoutingOutput:
                return _RoutingOutput()

            @entrypoint(name="target")
            async def target(self, input: _RoutingInput) -> _RoutingOutput:
                return _RoutingOutput()

        svc = create_app_handler_service(
            _TestHandler(),
            app_name="routing-test",
            app_class=_AliasApp,
            temporal_host="temporal:7233",
        )
        mock_handle = MagicMock(id="wf-123", result_run_id="run-abc")
        mock_client = MagicMock()
        mock_client.start_workflow = AsyncMock(return_value=mock_handle)

        with patch(
            "application_sdk.handler.service._get_temporal_client",
            new=AsyncMock(return_value=mock_client),
        ):
            client = TestClient(svc, raise_server_exceptions=False)
            with pytest.warns(DeprecationWarning, match="workflow_type.*deprecated"):
                response = client.post(
                    "/workflows/v1/start",
                    json={"workflow_type": "LegacyRoutingWorkflow", "name": "x"},
                )

        assert response.status_code == 200
        assert mock_client.start_workflow.await_args.args[0] == "routing-test:legacy"

    def test_entrypoint_query_dispatches_canonical_despite_alias(self) -> None:
        """C1: declaring an alias never changes what /start dispatches."""
        from unittest.mock import AsyncMock, MagicMock

        from application_sdk.app.base import App
        from application_sdk.app.entrypoint import entrypoint

        class _AliasedExtractApp(App):
            name = "routing-test"
            legacy_workflow_types = {"LegacyExtractWorkflow": "extract"}

            @entrypoint
            async def extract(self, input: _RoutingInput) -> _RoutingOutput:
                return _RoutingOutput()

        svc = create_app_handler_service(
            _TestHandler(),
            app_name="routing-test",
            app_class=_AliasedExtractApp,
            temporal_host="temporal:7233",
        )
        mock_handle = MagicMock(id="wf-123", result_run_id="run-abc")
        mock_client = MagicMock()
        mock_client.start_workflow = AsyncMock(return_value=mock_handle)

        with patch(
            "application_sdk.handler.service._get_temporal_client",
            new=AsyncMock(return_value=mock_client),
        ):
            client = TestClient(svc, raise_server_exceptions=False)
            response = client.post(
                "/workflows/v1/start?entrypoint=extract", json={"name": "x"}
            )

        assert response.status_code == 200
        assert mock_client.start_workflow.await_args.args[0] == "routing-test:extract"

    def test_query_param_takes_precedence_over_body_workflow_type(self) -> None:
        """?entrypoint= wins over body 'workflow_type' when both are provided."""
        from unittest.mock import AsyncMock, MagicMock

        from application_sdk.app.base import App
        from application_sdk.app.entrypoint import entrypoint

        class _PrecedenceApp(App):
            @entrypoint
            async def extract(self, input: _RoutingInput) -> _RoutingOutput:
                return _RoutingOutput()

            @entrypoint
            async def load(self, input: _RoutingInput) -> _RoutingOutput:
                return _RoutingOutput()

        handler = _TestHandler()
        svc = create_app_handler_service(
            handler,
            app_name="precedence-test",
            app_class=_PrecedenceApp,
            temporal_host="temporal:7233",
        )
        mock_client = MagicMock()
        mock_handle = MagicMock()
        mock_handle.id = "wf-prec"
        mock_handle.result_run_id = "run-prec"
        mock_client.start_workflow = AsyncMock(return_value=mock_handle)
        patcher = patch(
            "application_sdk.handler.service._get_temporal_client",
            new=AsyncMock(return_value=mock_client),
        )
        patcher.start()
        tc = TestClient(svc, raise_server_exceptions=False)
        try:
            # ?entrypoint=extract wins over body workflow_type=load;
            # the canonical query-param path must NOT emit a DeprecationWarning.
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")
                response = tc.post(
                    "/workflows/v1/start?entrypoint=extract",
                    json={"workflow_type": "load", "name": "x"},
                )
            assert response.status_code == 200
            assert mock_client.start_workflow.call_count == 1
            # The started workflow name must end in ':extract', not ':load'
            started_name = mock_client.start_workflow.call_args[0][0]
            assert ":load" not in started_name, f"load was dispatched: {started_name!r}"
            assert started_name.endswith(":extract"), (
                f"Expected :extract, got {started_name!r}"
            )
            assert not any(
                issubclass(w.category, DeprecationWarning) for w in caught
            ), "Canonical ?entrypoint= path must not emit DeprecationWarning"
        finally:
            patcher.stop()

    def test_entrypoint_query_param_rejects_an_alias(self) -> None:
        """The canonical selector is names-only: an alias through ?entrypoint=
        is a 400, keeping the alias namespace reachable only from raw Temporal
        dispatch and the deprecated body field."""
        from unittest.mock import AsyncMock, MagicMock

        from application_sdk.app.base import App
        from application_sdk.app.entrypoint import entrypoint

        class _QueryStrictApp(App):
            name = "routing-test"
            legacy_workflow_types = {"LegacyStrictWorkflow": "extract"}

            @entrypoint
            async def extract(self, input: _RoutingInput) -> _RoutingOutput:
                return _RoutingOutput()

        svc = create_app_handler_service(
            _TestHandler(),
            app_name="routing-test",
            app_class=_QueryStrictApp,
            temporal_host="temporal:7233",
        )
        mock_client = MagicMock()
        mock_client.start_workflow = AsyncMock()

        with patch(
            "application_sdk.handler.service._get_temporal_client",
            new=AsyncMock(return_value=mock_client),
        ):
            client = TestClient(svc, raise_server_exceptions=False)
            response = client.post(
                "/workflows/v1/start?entrypoint=LegacyStrictWorkflow",
                json={"name": "x"},
            )

        assert response.status_code == 400
        mock_client.start_workflow.assert_not_awaited()

    def test_alias_equal_to_a_sibling_name_is_impossible(self) -> None:
        """A11: the shape that made selector precedence ambiguous is rejected.

        One string serving as both an entry-point name and a sibling's legacy
        alias would make ``?entrypoint=`` and the deprecated body field resolve
        differently. Registration refuses the shape, so no precedence rule is
        needed — this pins that refusal.
        """
        from application_sdk.app.base import App
        from application_sdk.app.entrypoint import (
            EntryPointDeclarationError,
            entrypoint,
        )

        with pytest.raises(EntryPointDeclarationError, match="entry point name"):

            class _CollisionApp(App):
                name = "selector-collision"
                legacy_workflow_types = {"keifu": "extract"}

                @entrypoint(name="keifu")
                async def keifu(self, input: _RoutingInput) -> _RoutingOutput:
                    return _RoutingOutput()

                @entrypoint(name="extract")
                async def extract(self, input: _RoutingInput) -> _RoutingOutput:
                    return _RoutingOutput()

    def test_unknown_entrypoint_query_param_returns_400(self) -> None:
        """Providing an unrecognised ?entrypoint= returns 400 without leaking names."""
        from application_sdk.app.base import App
        from application_sdk.app.entrypoint import entrypoint

        class _TwoEpApp(App):
            @entrypoint
            async def extract(self, input: _RoutingInput) -> _RoutingOutput:
                return _RoutingOutput()

            @entrypoint
            async def load(self, input: _RoutingInput) -> _RoutingOutput:
                return _RoutingOutput()

        client, patcher = self._make_routed_client(_TwoEpApp)
        try:
            response = client.post(
                "/workflows/v1/start?entrypoint=does-not-exist",
                json={"name": "x"},
            )
            assert response.status_code == 400
            detail = response.json().get("detail", "")
            assert "extract" not in detail
            assert "load" not in detail
        finally:
            patcher.stop()

    def test_multi_ep_no_entrypoint_dispatches_auto_default(self) -> None:
        """Multi-entry-point app with no ?entrypoint= dispatches to the auto-default.

        When multiple @entrypoints exist with no explicit default, the first
        alphabetically (via dir()) is auto-marked default at registration.
        Omitting ?entrypoint= therefore returns 200 and dispatches that ep.
        """
        from application_sdk.app.base import App
        from application_sdk.app.entrypoint import entrypoint

        class _AnotherMultiEpApp(App):
            @entrypoint
            async def step_a(self, input: _RoutingInput) -> _RoutingOutput:
                return _RoutingOutput()

            @entrypoint
            async def step_b(self, input: _RoutingInput) -> _RoutingOutput:
                return _RoutingOutput()

        client, patcher = self._make_routed_client(_AnotherMultiEpApp)
        try:
            response = client.post("/workflows/v1/start", json={"name": "x"})
            assert response.status_code == 200
        finally:
            patcher.stop()


@pytest.mark.usefixtures("clean_app_registry", "clean_task_registry")
class TestStartWorkflowInvocability:
    """Tests that every entry-point combination is reachable via /workflows/v1/start.

    Covers:
      - run()-only: no ?entrypoint= AND ?entrypoint=run
      - @entrypoint-only (single): no ?entrypoint= AND ?entrypoint={name}
      - @entrypoints (multiple): ?entrypoint= for each AND no ?entrypoint= uses auto-default
      - mixed run() + @entrypoint: default (run, no ?entrypoint=), explicit by name
      - mixed run() + @entrypoints (multiple): same rules
    """

    def _make_routed_client(self, app_cls: type):  # type: ignore[return]
        from unittest.mock import AsyncMock, MagicMock

        handler = _TestHandler()
        svc = create_app_handler_service(
            handler,
            app_name="inv-test",
            app_class=app_cls,
            temporal_host="temporal:7233",
        )
        mock_client = MagicMock()
        mock_handle = MagicMock()
        mock_handle.id = "wf-1"
        mock_handle.result_run_id = "run-1"
        mock_client.start_workflow = AsyncMock(return_value=mock_handle)
        patcher = patch(
            "application_sdk.handler.service._get_temporal_client",
            new=AsyncMock(return_value=mock_client),
        )
        patcher.start()
        client = TestClient(svc, raise_server_exceptions=False)
        return client, patcher, mock_client

    def _started_workflow_name(self, mock_client: object) -> str:
        from unittest.mock import MagicMock

        assert isinstance(mock_client, MagicMock)
        return mock_client.start_workflow.call_args[0][0]

    # ── run()-only ────────────────────────────────────────────────────────

    def test_run_only_no_param_is_200(self) -> None:
        """run()-only app: omitting ?entrypoint= dispatches successfully."""
        from application_sdk.app.base import App

        class _RunOnlyInvApp(App):
            async def run(self, input: _RoutingInput) -> _RoutingOutput:
                return _RoutingOutput()

        client, patcher, mock_client = self._make_routed_client(_RunOnlyInvApp)
        try:
            resp = client.post("/workflows/v1/start", json={"name": "x"})
            assert resp.status_code == 200
            # Implicit run() uses bare app-name workflow type (no colon suffix)
            assert ":" not in self._started_workflow_name(mock_client)
        finally:
            patcher.stop()

    def test_run_only_explicit_run_param_is_200(self) -> None:
        """run()-only app: ?entrypoint=run dispatches to the same implicit entry point."""
        from application_sdk.app.base import App

        class _RunOnlyExplicitApp(App):
            async def run(self, input: _RoutingInput) -> _RoutingOutput:
                return _RoutingOutput()

        client, patcher, mock_client = self._make_routed_client(_RunOnlyExplicitApp)
        try:
            resp = client.post("/workflows/v1/start?entrypoint=run", json={"name": "x"})
            assert resp.status_code == 200
            assert ":" not in self._started_workflow_name(mock_client)
        finally:
            patcher.stop()

    # ── @entrypoint-only (single) ─────────────────────────────────────────

    def test_single_ep_no_param_is_200(self) -> None:
        """Single @entrypoint app: omitting ?entrypoint= dispatches via len==1 default."""
        from application_sdk.app.base import App
        from application_sdk.app.entrypoint import entrypoint

        class _SingleEpInvApp(App):
            @entrypoint
            async def ingest(self, input: _RoutingInput) -> _RoutingOutput:
                return _RoutingOutput()

        client, patcher, mock_client = self._make_routed_client(_SingleEpInvApp)
        try:
            resp = client.post("/workflows/v1/start", json={"name": "x"})
            assert resp.status_code == 200
            assert self._started_workflow_name(mock_client).endswith(":ingest")
        finally:
            patcher.stop()

    def test_single_ep_explicit_param_is_200(self) -> None:
        """Single @entrypoint app: ?entrypoint={name} dispatches correctly."""
        from application_sdk.app.base import App
        from application_sdk.app.entrypoint import entrypoint

        class _SingleEpExplicitApp(App):
            @entrypoint
            async def ingest(self, input: _RoutingInput) -> _RoutingOutput:
                return _RoutingOutput()

        client, patcher, mock_client = self._make_routed_client(_SingleEpExplicitApp)
        try:
            resp = client.post(
                "/workflows/v1/start?entrypoint=ingest", json={"name": "x"}
            )
            assert resp.status_code == 200
            assert self._started_workflow_name(mock_client).endswith(":ingest")
        finally:
            patcher.stop()

    # ── @entrypoints (multiple) ───────────────────────────────────────────

    def test_multi_ep_no_param_dispatches_auto_default(self) -> None:
        """Multiple @entrypoints, none explicit default: ?entrypoint= omitted uses auto-default.

        Auto-default is the first alphabetically: alpha-ep precedes beta-ep.
        """
        from application_sdk.app.base import App
        from application_sdk.app.entrypoint import entrypoint

        class _MultiEpInvApp(App):
            @entrypoint
            async def alpha_ep(self, input: _RoutingInput) -> _RoutingOutput:
                return _RoutingOutput()

            @entrypoint
            async def beta_ep(self, input: _RoutingInput) -> _RoutingOutput:
                return _RoutingOutput()

        client, patcher, mock_client = self._make_routed_client(_MultiEpInvApp)
        try:
            resp = client.post("/workflows/v1/start", json={"name": "x"})
            assert resp.status_code == 200
            assert self._started_workflow_name(mock_client).endswith(":alpha-ep")
        finally:
            patcher.stop()

    def test_multi_ep_explicit_alpha_param_is_200(self) -> None:
        """Multiple @entrypoints: ?entrypoint=alpha-ep dispatches to alpha-ep."""
        from application_sdk.app.base import App
        from application_sdk.app.entrypoint import entrypoint

        class _MultiEpAlphaApp(App):
            @entrypoint
            async def alpha_ep(self, input: _RoutingInput) -> _RoutingOutput:
                return _RoutingOutput()

            @entrypoint
            async def beta_ep(self, input: _RoutingInput) -> _RoutingOutput:
                return _RoutingOutput()

        client, patcher, mock_client = self._make_routed_client(_MultiEpAlphaApp)
        try:
            resp = client.post(
                "/workflows/v1/start?entrypoint=alpha-ep", json={"name": "x"}
            )
            assert resp.status_code == 200
            assert self._started_workflow_name(mock_client).endswith(":alpha-ep")
        finally:
            patcher.stop()

    def test_multi_ep_explicit_beta_param_is_200(self) -> None:
        """Multiple @entrypoints: ?entrypoint=beta-ep dispatches to beta-ep."""
        from application_sdk.app.base import App
        from application_sdk.app.entrypoint import entrypoint

        class _MultiEpBetaApp(App):
            @entrypoint
            async def alpha_ep(self, input: _RoutingInput) -> _RoutingOutput:
                return _RoutingOutput()

            @entrypoint
            async def beta_ep(self, input: _RoutingInput) -> _RoutingOutput:
                return _RoutingOutput()

        client, patcher, mock_client = self._make_routed_client(_MultiEpBetaApp)
        try:
            resp = client.post(
                "/workflows/v1/start?entrypoint=beta-ep", json={"name": "x"}
            )
            assert resp.status_code == 200
            assert self._started_workflow_name(mock_client).endswith(":beta-ep")
        finally:
            patcher.stop()

    def test_multi_ep_explicit_default_no_param_dispatches_marked(self) -> None:
        """Multiple @entrypoints with one explicit default: ?entrypoint= omitted uses marked."""
        from application_sdk.app.base import App
        from application_sdk.app.entrypoint import entrypoint

        class _MultiEpMarkedApp(App):
            @entrypoint
            async def alpha_ep(self, input: _RoutingInput) -> _RoutingOutput:
                return _RoutingOutput()

            @entrypoint(default=True)
            async def beta_ep(self, input: _RoutingInput) -> _RoutingOutput:
                return _RoutingOutput()

        client, patcher, mock_client = self._make_routed_client(_MultiEpMarkedApp)
        try:
            resp = client.post("/workflows/v1/start", json={"name": "x"})
            assert resp.status_code == 200
            assert self._started_workflow_name(mock_client).endswith(":beta-ep")
        finally:
            patcher.stop()

    # ── mixed run() + single @entrypoint ─────────────────────────────────

    def test_mixed_single_no_param_dispatches_run(self) -> None:
        """Mixed app: ?entrypoint= omitted dispatches to run() (permanent default)."""
        from application_sdk.app.base import App
        from application_sdk.app.entrypoint import entrypoint

        class _MixedSingleInvApp(App):
            @entrypoint
            async def extract(self, input: _RoutingInput) -> _RoutingOutput:
                return _RoutingOutput()

            async def run(self, input: _RoutingInput) -> _RoutingOutput:
                return _RoutingOutput()

        client, patcher, mock_client = self._make_routed_client(_MixedSingleInvApp)
        try:
            resp = client.post("/workflows/v1/start", json={"name": "x"})
            assert resp.status_code == 200
            # run() is implicit → bare app-name, no colon
            assert ":" not in self._started_workflow_name(mock_client)
        finally:
            patcher.stop()

    def test_mixed_single_run_param_dispatches_run(self) -> None:
        """Mixed app: ?entrypoint=run dispatches to run()."""
        from application_sdk.app.base import App
        from application_sdk.app.entrypoint import entrypoint

        class _MixedRunParamApp(App):
            @entrypoint
            async def extract(self, input: _RoutingInput) -> _RoutingOutput:
                return _RoutingOutput()

            async def run(self, input: _RoutingInput) -> _RoutingOutput:
                return _RoutingOutput()

        client, patcher, mock_client = self._make_routed_client(_MixedRunParamApp)
        try:
            resp = client.post("/workflows/v1/start?entrypoint=run", json={"name": "x"})
            assert resp.status_code == 200
            assert ":" not in self._started_workflow_name(mock_client)
        finally:
            patcher.stop()

    def test_mixed_single_explicit_ep_param_dispatches_ep(self) -> None:
        """Mixed app: ?entrypoint={name} dispatches to the named @entrypoint."""
        from application_sdk.app.base import App
        from application_sdk.app.entrypoint import entrypoint

        class _MixedEpParamApp(App):
            @entrypoint
            async def extract(self, input: _RoutingInput) -> _RoutingOutput:
                return _RoutingOutput()

            async def run(self, input: _RoutingInput) -> _RoutingOutput:
                return _RoutingOutput()

        client, patcher, mock_client = self._make_routed_client(_MixedEpParamApp)
        try:
            resp = client.post(
                "/workflows/v1/start?entrypoint=extract", json={"name": "x"}
            )
            assert resp.status_code == 200
            assert self._started_workflow_name(mock_client).endswith(":extract")
        finally:
            patcher.stop()

    # ── mixed run() + multiple @entrypoints ──────────────────────────────

    def test_mixed_multi_no_param_dispatches_run(self) -> None:
        """Mixed app with multiple @entrypoints: run() remains default when omitted."""
        from application_sdk.app.base import App
        from application_sdk.app.entrypoint import entrypoint

        class _MixedMultiInvApp(App):
            @entrypoint
            async def extract(self, input: _RoutingInput) -> _RoutingOutput:
                return _RoutingOutput()

            @entrypoint
            async def publish(self, input: _RoutingInput) -> _RoutingOutput:
                return _RoutingOutput()

            async def run(self, input: _RoutingInput) -> _RoutingOutput:
                return _RoutingOutput()

        client, patcher, mock_client = self._make_routed_client(_MixedMultiInvApp)
        try:
            resp = client.post("/workflows/v1/start", json={"name": "x"})
            assert resp.status_code == 200
            assert ":" not in self._started_workflow_name(mock_client)
        finally:
            patcher.stop()

    def test_mixed_multi_explicit_ep_params_each_dispatch_correctly(self) -> None:
        """Mixed app with multiple @entrypoints: each is reachable by name."""
        from application_sdk.app.base import App
        from application_sdk.app.entrypoint import entrypoint

        class _MixedMultiEachApp(App):
            @entrypoint
            async def extract(self, input: _RoutingInput) -> _RoutingOutput:
                return _RoutingOutput()

            @entrypoint
            async def publish(self, input: _RoutingInput) -> _RoutingOutput:
                return _RoutingOutput()

            async def run(self, input: _RoutingInput) -> _RoutingOutput:
                return _RoutingOutput()

        for ep_name, ep_suffix in [
            ("run", None),
            ("extract", ":extract"),
            ("publish", ":publish"),
        ]:
            client, patcher, mock_client = self._make_routed_client(_MixedMultiEachApp)
            try:
                resp = client.post(
                    f"/workflows/v1/start?entrypoint={ep_name}", json={"name": "x"}
                )
                assert resp.status_code == 200, (
                    f"?entrypoint={ep_name} returned {resp.status_code}"
                )
                wf_name = self._started_workflow_name(mock_client)
                if ep_suffix is None:
                    assert ":" not in wf_name, (
                        f"?entrypoint={ep_name}: expected bare name (no colon), got {wf_name!r}"
                    )
                else:
                    assert wf_name.endswith(ep_suffix), (
                        f"?entrypoint={ep_name}: expected suffix {ep_suffix!r}, got {wf_name!r}"
                    )
            finally:
                patcher.stop()


class TestWrapResponse:
    """Tests for _wrap_response helper."""

    def test_basic_structure(self) -> None:
        result = _wrap_response({"key": "value"})
        assert result["success"] is True
        assert "message" not in result  # omitted when empty (frontend dropdowns)
        assert result["data"] == {"key": "value"}

    def test_custom_message(self) -> None:
        result = _wrap_response({}, message="All good")
        assert result["message"] == "All good"

    def test_failure_response(self) -> None:
        result = _wrap_response({"error": "oops"}, success=False, message="failed")
        assert result["success"] is False
        assert result["message"] == "failed"

    def test_list_data(self) -> None:
        result = _wrap_response([{"a": 1}, {"b": 2}], message="ok")
        assert result["data"] == [{"a": 1}, {"b": 2}]
        assert result["message"] == "ok"


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


class TestWorkflowConfigValidation:
    """Tests for _CONFIG_KEY_RE validation on /workflows/v1/config/{config_id}."""

    @pytest.mark.parametrize(
        "config_id",
        [
            "../foo",
            "a/b",
            "a.b",
            "",
            "a" * 129,
            "%2e%2e",
            "a b",
        ],
    )
    def test_get_config_rejects_invalid_config_id(self, config_id: str) -> None:
        client = _make_client()
        response = client.get(f"/workflows/v1/config/{config_id}")
        assert response.status_code in {
            400,
            404,
            422,
        }, f"Expected rejection for {config_id!r}, got {response.status_code}"

    @pytest.mark.parametrize(
        "config_id",
        [
            "../foo",
            "a/b",
            "a.b",
            "",
            "a" * 129,
            "%2e%2e",
            "a b",
        ],
    )
    def test_post_config_rejects_invalid_config_id(self, config_id: str) -> None:
        client = _make_client()
        response = client.post(
            f"/workflows/v1/config/{config_id}",
            json={"key": "value"},
        )
        assert response.status_code in {
            400,
            404,
            422,
        }, f"Expected rejection for {config_id!r}, got {response.status_code}"

    @pytest.mark.parametrize(
        "type_param",
        ["../etc", "a/b", "a.b", "a" * 129],
    )
    def test_get_config_rejects_invalid_type_param(self, type_param: str) -> None:
        client = _make_client()
        response = client.get(
            "/workflows/v1/config/valid-id",
            params={"type": type_param},
        )
        assert response.status_code == 422, (
            f"Expected 422 from FastAPI pattern validator for type={type_param!r}, got {response.status_code}"
        )

    @pytest.mark.parametrize(
        "type_param",
        ["../etc", "a/b", "a.b", "a" * 129],
    )
    def test_post_config_rejects_invalid_type_param(self, type_param: str) -> None:
        client = _make_client()
        response = client.post(
            "/workflows/v1/config/valid-id",
            params={"type": type_param},
            json={"key": "value"},
        )
        assert response.status_code == 422, (
            f"Expected 422 from FastAPI pattern validator for type={type_param!r}, got {response.status_code}"
        )

    @pytest.mark.parametrize(
        "config_id",
        ["abc123", "ABC_123", "a-b", "a" * 128],
    )
    def test_get_config_accepts_valid_config_id(self, config_id: str) -> None:
        """Valid config_ids pass the regex check (result is 503/404 without a store, not 400)."""
        client = _make_client()
        response = client.get(f"/workflows/v1/config/{config_id}")
        assert response.status_code != 400, (
            f"Valid config_id {config_id!r} was wrongly rejected"
        )


class TestStartCredentialStripping:
    """Tests for credential stripping in /start handler.

    The /start endpoint strips credentials from the body before dispatching
    to Temporal. No state store save or credential_guid injection occurs.
    """

    def test_normalize_v2_dict_credentials(self) -> None:
        """V2 dict credentials are normalized to v3 list format."""
        body = {
            "credentials": {
                "host": "db.example.com",
                "port": 5432,
                "username": "admin",
                "password": "secret",
            },
            "other_field": "kept",
        }

        body = _normalize_credentials(body)

        assert isinstance(body["credentials"], list)
        keys = {item["key"] for item in body["credentials"]}
        assert "host" in keys
        assert "port" in keys
        # Other fields preserved
        assert body["other_field"] == "kept"

    def test_normalize_v3_list_credentials(self) -> None:
        """V3 list credentials pass through normalization unchanged."""
        body = {
            "credentials": [
                {"key": "host", "value": "db.example.com"},
                {"key": "username", "value": "admin"},
            ],
        }

        body = _normalize_credentials(body)

        assert isinstance(body["credentials"], list)
        assert len(body["credentials"]) == 2

    def test_no_credentials_passthrough(self) -> None:
        """Body without credentials passes through unchanged."""
        body = {"name": "test-workflow"}
        body = _normalize_credentials(body)
        assert "credentials" not in body or not body.get("credentials")


@pytest.mark.usefixtures("clean_app_registry", "clean_task_registry")
class TestStartWorkflowExtras:
    """Tests for /start beyond simple routing — explicit workflow_id, caller
    correlation_id, credential stripping, and error branches."""

    def _wire(self, app_cls):
        """Build service + patch _get_temporal_client. Caller must reset
        registries beforehand and define app_cls AFTER reset."""
        from unittest.mock import AsyncMock, MagicMock

        svc = create_app_handler_service(
            _TestHandler(),
            app_name="start-extras",
            app_class=app_cls,
            temporal_host="temporal:7233",
        )
        mock_client = MagicMock()
        mock_handle = MagicMock()
        mock_handle.id = "wf-id-actual"
        mock_handle.result_run_id = "run-id-actual"
        mock_client.start_workflow = AsyncMock(return_value=mock_handle)
        patcher = patch(
            "application_sdk.handler.service._get_temporal_client",
            new=AsyncMock(return_value=mock_client),
        )
        patcher.start()
        return TestClient(svc, raise_server_exceptions=False), mock_client, patcher

    def test_explicit_workflow_id_is_used(self) -> None:
        from application_sdk.app.base import App

        class _IdApp(App):
            async def run(self, input: _RoutingInput) -> _RoutingOutput:
                return _RoutingOutput()

        client, mock_client, patcher = self._wire(_IdApp)
        try:
            response = client.post(
                "/workflows/v1/start",
                json={"name": "x", "workflow_id": "caller-given-id"},
            )
            assert response.status_code == 200
            kwargs = mock_client.start_workflow.call_args.kwargs
            assert kwargs["id"] == "caller-given-id"
        finally:
            patcher.stop()

    def test_caller_correlation_id_round_trips(self) -> None:
        """If caller provides correlation_id, response echoes the same value."""
        from application_sdk.app.base import App

        class _CorrApp(App):
            async def run(self, input: _RoutingInput) -> _RoutingOutput:
                return _RoutingOutput()

        client, mock_client, patcher = self._wire(_CorrApp)
        try:
            response = client.post(
                "/workflows/v1/start",
                json={"name": "x", "correlation_id": "trace-9999"},
            )
            assert response.status_code == 200
            assert response.json()["correlation_id"] == "trace-9999"
        finally:
            patcher.stop()

    def test_inline_credentials_are_stripped_before_dispatch(self) -> None:
        """Credentials must NEVER reach Temporal history — start_workflow's
        first arg (input_data) must not contain raw credential pairs."""
        from application_sdk.app.base import App

        class _StripApp(App):
            async def run(self, input: _RoutingInput) -> _RoutingOutput:
                return _RoutingOutput()

        client, mock_client, patcher = self._wire(_StripApp)
        try:
            response = client.post(
                "/workflows/v1/start",
                json={
                    "name": "x",
                    "credentials": [{"key": "password", "value": "supersecret"}],
                },
            )
            assert response.status_code == 200
            call_args = mock_client.start_workflow.call_args
            input_data = call_args.kwargs["args"][0]
            dumped = (
                input_data.model_dump()
                if hasattr(input_data, "model_dump")
                else vars(input_data)
            )
            assert "supersecret" not in str(dumped)
        finally:
            patcher.stop()

    def test_start_response_envelope_shape(self) -> None:
        from application_sdk.app.base import App

        class _EnvApp(App):
            async def run(self, input: _RoutingInput) -> _RoutingOutput:
                return _RoutingOutput()

        client, _, patcher = self._wire(_EnvApp)
        try:
            response = client.post("/workflows/v1/start", json={"name": "x"})
            body = response.json()
            assert set(body.keys()) >= {"success", "message", "data", "correlation_id"}
            assert body["success"] is True
            assert "workflow_id" in body["data"]
            assert "run_id" in body["data"]
        finally:
            patcher.stop()


@pytest.mark.usefixtures("clean_app_registry", "clean_task_registry")
class TestStartWorkflowExecutionTimeout:
    """Tests for execution_timeout wiring in /workflows/v1/start."""

    def _wire(self, app_cls, workflow_max_timeout_hours=None):
        from unittest.mock import AsyncMock, MagicMock

        svc = create_app_handler_service(
            _TestHandler(),
            app_name="timeout-test",
            app_class=app_cls,
            temporal_host="temporal:7233",
            workflow_max_timeout_hours=workflow_max_timeout_hours,
        )
        mock_client = MagicMock()
        mock_handle = MagicMock()
        mock_handle.id = "wf-id"
        mock_handle.result_run_id = "run-id"
        mock_client.start_workflow = AsyncMock(return_value=mock_handle)
        patcher = patch(
            "application_sdk.handler.service._get_temporal_client",
            new=AsyncMock(return_value=mock_client),
        )
        patcher.start()
        return TestClient(svc, raise_server_exceptions=False), mock_client, patcher

    def test_execution_timeout_set_when_configured(self) -> None:
        """execution_timeout is passed to start_workflow when workflow_max_timeout_hours is set."""
        from datetime import timedelta

        from application_sdk.app.base import App

        class _TApp(App):
            async def run(self, input: _RoutingInput) -> _RoutingOutput:
                return _RoutingOutput()

        client, mock_client, patcher = self._wire(_TApp, workflow_max_timeout_hours=48)
        try:
            response = client.post("/workflows/v1/start", json={"name": "x"})
            assert response.status_code == 200
            kwargs = mock_client.start_workflow.call_args.kwargs
            assert kwargs["execution_timeout"] == timedelta(hours=48)
        finally:
            patcher.stop()

    def test_execution_timeout_none_when_not_configured(self) -> None:
        """execution_timeout is None when workflow_max_timeout_hours is not set."""
        from application_sdk.app.base import App

        class _TApp2(App):
            async def run(self, input: _RoutingInput) -> _RoutingOutput:
                return _RoutingOutput()

        client, mock_client, patcher = self._wire(_TApp2)
        try:
            response = client.post("/workflows/v1/start", json={"name": "x"})
            assert response.status_code == 200
            kwargs = mock_client.start_workflow.call_args.kwargs
            assert kwargs["execution_timeout"] is None
        finally:
            patcher.stop()
