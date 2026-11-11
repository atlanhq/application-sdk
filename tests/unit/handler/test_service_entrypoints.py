"""Handler service tests: input contracts, default and bundle entrypoints, agent-json ingress and manifest POST transport."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from application_sdk.handler.contracts import (
    PreflightInput,
    PreflightOutput,
)
from application_sdk.handler.service import (
    _normalize_preflight_request,
    _RequestContractError,
    _validate_request,
    create_app_handler_service,
)
from application_sdk.templates.contracts.sql_metadata import (  # entry-point annotations resolve at module level
    ExtractionInput,
    ExtractionOutput,
)
from tests.unit.handler._service_support import (
    _AlphaInput,
    _AlphaOutput,
    _BetaInput,
    _BetaOutput,
    _install_fake_app_module,
    _make_client,
    _RoutingInput,
    _RoutingOutput,
    _TestHandler,
)

# Toolkit output, rendered from fixtures/bigquery_contract/app.pkl.
_GENERATED_CRAWLER_INPUT = (
    Path(__file__).parent
    / "fixtures"
    / "generated_bigquery_credentials"
    / "crawler"
    / "_input.py"
)


@pytest.mark.usefixtures("clean_app_registry", "clean_task_registry")
class TestInputContractEndpoint:
    """Tests for GET /workflows/v1/input-contract."""

    def _client(self, app_cls: type) -> TestClient:
        svc = create_app_handler_service(
            _TestHandler(), app_name="ic-test", app_class=app_cls
        )
        return TestClient(svc, raise_server_exceptions=False)

    def test_single_entrypoint_returns_model_json_schema(self) -> None:
        """Single-entry-point app: no ?entrypoint= resolves the only one and
        returns exactly AppInputContract.model_json_schema()."""
        from application_sdk.app.base import App

        class _SingleEpApp(App):
            async def run(self, input: _RoutingInput) -> _RoutingOutput:
                return _RoutingOutput()

        resp = self._client(_SingleEpApp).get("/workflows/v1/input-contract")
        assert resp.status_code == 200
        assert resp.json() == _RoutingInput.model_json_schema()

    def test_multi_entrypoint_selected_by_query_param(self) -> None:
        """Multi-entry-point app: ?entrypoint=<name> returns that entry point's schema."""
        from application_sdk.app.base import App
        from application_sdk.app.entrypoint import entrypoint

        class _MultiEpApp(App):
            @entrypoint
            async def extract(self, input: _RoutingInput) -> _RoutingOutput:
                return _RoutingOutput()

            @entrypoint
            async def load(self, input: _RoutingInput) -> _RoutingOutput:
                return _RoutingOutput()

        resp = self._client(_MultiEpApp).get(
            "/workflows/v1/input-contract?entrypoint=extract"
        )
        assert resp.status_code == 200
        assert resp.json() == _RoutingInput.model_json_schema()

    def test_multi_entrypoint_no_param_uses_auto_default(self) -> None:
        """Multi-entry-point app without ?entrypoint= → 200 using auto-default (first alphabetically)."""
        from application_sdk.app.base import App
        from application_sdk.app.entrypoint import entrypoint

        class _MultiEpApp2(App):
            @entrypoint
            async def extract(self, input: _RoutingInput) -> _RoutingOutput:
                return _RoutingOutput()

            @entrypoint
            async def load(self, input: _RoutingInput) -> _RoutingOutput:
                return _RoutingOutput()

        # 'extract' precedes 'load' alphabetically → auto-marked default
        resp = self._client(_MultiEpApp2).get("/workflows/v1/input-contract")
        assert resp.status_code == 200
        assert resp.json() == _RoutingInput.model_json_schema()

    def test_unknown_entrypoint_returns_404(self) -> None:
        from application_sdk.app.base import App

        class _SingleEpApp2(App):
            async def run(self, input: _RoutingInput) -> _RoutingOutput:
                return _RoutingOutput()

        resp = self._client(_SingleEpApp2).get(
            "/workflows/v1/input-contract?entrypoint=nope"
        )
        assert resp.status_code == 404

    def test_invalid_entrypoint_name_returns_400(self) -> None:
        from application_sdk.app.base import App

        class _SingleEpApp3(App):
            async def run(self, input: _RoutingInput) -> _RoutingOutput:
                return _RoutingOutput()

        resp = self._client(_SingleEpApp3).get(
            "/workflows/v1/input-contract?entrypoint=../etc"
        )
        assert resp.status_code == 400

    @staticmethod
    def _inject_generated_contract(
        monkeypatch: pytest.MonkeyPatch, ep_module: str, source: Path
    ) -> type:
        """Import *source* as ``app.generated.{ep_module}._input``.

        Parents are added only when absent and the leaf through
        ``monkeypatch``, so any module a previous import left in
        ``sys.modules`` is restored afterwards, never dropped.
        """
        import importlib.util
        import sys
        import types

        for parent in ("app", "app.generated", f"app.generated.{ep_module}"):
            if parent not in sys.modules:
                monkeypatch.setitem(sys.modules, parent, types.ModuleType(parent))
        leaf = f"app.generated.{ep_module}._input"
        spec = importlib.util.spec_from_file_location(leaf, source)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        monkeypatch.setitem(sys.modules, leaf, module)
        return module.AppInputContract

    def test_prefers_generated_app_input_contract(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When app/generated/{ep}/_input.py:AppInputContract is importable, the
        endpoint returns its (rich) schema, not the thin runtime input_type.

        The module is real toolkit output: ``crawler/_input.py`` rendered from
        ``fixtures/bigquery_contract/app.pkl`` (kept current by
        ``tests/regen_fixtures.py``).
        """
        from application_sdk.app.base import App
        from application_sdk.app.entrypoint import entrypoint

        class _MultiEpGen(App):
            @entrypoint
            async def crawler(self, input: _RoutingInput) -> _RoutingOutput:
                return _RoutingOutput()

            @entrypoint
            async def load(self, input: _RoutingInput) -> _RoutingOutput:
                return _RoutingOutput()

        contract = self._inject_generated_contract(
            monkeypatch, "crawler", _GENERATED_CRAWLER_INPUT
        )
        resp = self._client(_MultiEpGen).get(
            "/workflows/v1/input-contract?entrypoint=crawler"
        )
        assert resp.status_code == 200
        # rich generated contract, not the thin _RoutingInput
        assert resp.json() == contract.model_json_schema()
        assert "crawler_credential" in resp.json()["properties"]
        # the entrypoint with no generated module still falls back
        resp_load = self._client(_MultiEpGen).get(
            "/workflows/v1/input-contract?entrypoint=load"
        )
        assert resp_load.json() == _RoutingInput.model_json_schema()

    def test_published_contract_falls_back_to_input_type(self) -> None:
        """No generated module → _published_input_contract returns input_type."""
        from application_sdk.app.entrypoint import EntryPointMetadata
        from application_sdk.handler.service import _published_input_contract

        ep = EntryPointMetadata(
            name="no-generated-here",
            input_type=_RoutingInput,
            output_type=_RoutingOutput,
            method_name="no_generated_here",
        )
        assert _published_input_contract(ep) is _RoutingInput


@pytest.mark.usefixtures("clean_app_registry", "clean_task_registry")
class TestDefaultEntrypoint:
    """Tests for default-entrypoint resolution (@entrypoint(default=True))."""

    def test_resolver_rules(self) -> None:
        from application_sdk.app.entrypoint import (
            EntryPointMetadata,
            _resolve_default_entrypoint,
        )

        def _ep(name: str, *, default: bool = False) -> EntryPointMetadata:
            return EntryPointMetadata(
                name=name,
                input_type=_RoutingInput,
                output_type=_RoutingOutput,
                method_name=name,
                default=default,
            )

        a, b = _ep("a"), _ep("b")
        # single → that one
        assert _resolve_default_entrypoint({"a": a}) is a
        # multi, one default → the default
        bd = _ep("b", default=True)
        assert _resolve_default_entrypoint({"a": a, "b": bd}) is bd
        # multi, no default → None
        assert _resolve_default_entrypoint({"a": a, "b": b}) is None
        # multi, ambiguous (>1 default) → None
        ad = _ep("a", default=True)
        assert _resolve_default_entrypoint({"a": ad, "b": bd}) is None
        # empty → None
        assert _resolve_default_entrypoint({}) is None

    def test_multiple_defaults_raise_at_registration(self) -> None:
        from application_sdk.app.base import App
        from application_sdk.app.entrypoint import EntryPointContractError, entrypoint

        with pytest.raises(EntryPointContractError, match="default entry point"):

            class _BadApp(App):
                @entrypoint(default=True)
                async def extract(self, input: _AlphaInput) -> _AlphaOutput:
                    return _AlphaOutput()

                @entrypoint(default=True)
                async def load(self, input: _BetaInput) -> _BetaOutput:
                    return _BetaOutput()

    def test_input_contract_resolves_marked_default(self) -> None:
        """Multi-entry-point app: no ?entrypoint= resolves the default-marked one."""
        from application_sdk.app.base import App
        from application_sdk.app.entrypoint import entrypoint

        class _DefaultEpApp(App):
            @entrypoint
            async def extract(self, input: _AlphaInput) -> _AlphaOutput:
                return _AlphaOutput()

            @entrypoint(default=True)
            async def load(self, input: _RoutingInput) -> _RoutingOutput:
                return _RoutingOutput()

        svc = create_app_handler_service(
            _TestHandler(), app_name="default-ic", app_class=_DefaultEpApp
        )
        resp = TestClient(svc, raise_server_exceptions=False).get(
            "/workflows/v1/input-contract"
        )
        assert resp.status_code == 200
        # The default ('load') uses _RoutingInput — confirm that schema came back.
        assert resp.json() == _RoutingInput.model_json_schema()

    def test_start_resolves_marked_default_without_param(self) -> None:
        """Multi-entry-point app: /start with no ?entrypoint= resolves the default."""
        from unittest.mock import AsyncMock, MagicMock, patch

        from application_sdk.app.base import App
        from application_sdk.app.entrypoint import entrypoint

        class _DefaultStartApp(App):
            @entrypoint
            async def extract(self, input: _AlphaInput) -> _AlphaOutput:
                return _AlphaOutput()

            @entrypoint(default=True)
            async def load(self, input: _RoutingInput) -> _RoutingOutput:
                return _RoutingOutput()

        svc = create_app_handler_service(
            _TestHandler(),
            app_name="default-start",
            app_class=_DefaultStartApp,
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
        try:
            resp = TestClient(svc, raise_server_exceptions=False).post(
                "/workflows/v1/start", json={"name": "x"}
            )
            assert resp.status_code == 200
        finally:
            patcher.stop()


class TestAgentJsonIngressOnTheHandlerPath:
    """The handler path's end of the ``agent_json`` ingress normaliser.

    The normaliser itself is pinned in
    ``tests/unit/credentials/test_ingress.py``; this covers what the request
    path adds — the normalized body must be accepted by the typed input, and a
    body that is genuinely malformed must come back as a 422 naming the field
    rather than a plain-text 500.
    """

    _PLACEHOLDER = {
        "agent-name": "agent-name",
        "aws-auth-method": "aws-auth-method",
        "host": "host",
        "port": "port",
        "secret-manager": "secret-manager",
    }

    def test_placeholder_widget_body_still_validates(self) -> None:
        """The regression: the normalized body must be accepted by the typed input.

        A rendered-but-unfilled agent widget submits every field name as its own
        value. ``port`` is ``AgentCredentialSpec``'s only non-str field, so the
        payload coerces to a dict but fails typed validation — promoting it made
        every direct-mode request whose form renders the widget 500.
        """
        body = _normalize_preflight_request(
            {
                "connector": "oracle",
                "entrypoint": "miner",
                "credentials": {"extra": {}},
                "metadata": {
                    "agent_json": json.dumps(self._PLACEHOLDER),
                    "agent-json": json.dumps(self._PLACEHOLDER),
                    "extraction_method": "query_history",
                },
            }
        )
        validated = PreflightInput.model_validate(body)  # must not raise
        assert validated.agent_json is None

    @pytest.mark.parametrize(
        "endpoint",
        [
            "/workflows/v1/auth",
            "/workflows/v1/check",
            "/workflows/v1/metadata",
        ],
    )
    def test_malformed_body_is_a_422_naming_the_field(self, endpoint: str) -> None:
        """A body that does not fit the contract must say which field.

        The endpoints validate their own body (they normalise v2 wire shapes
        first), so an unhandled ValidationError used to become Starlette's
        plain-text 500 — reaching the caller as an opaque JSON-decode error with
        no hint of the offending field. That is why the placeholder bug above
        took a full investigation to pin.

        Parameterized over the three ingress endpoints (auth / check /
        metadata): all route through the same ``_validate_request`` call and
        their contracts share ``timeout_seconds: int``, so the same malformed
        body proves each route answers a field-named 422 (route-drift
        protection, not just the shared mechanism).
        """
        client = _make_client()
        response = client.post(
            endpoint,
            json={"credentials": [], "timeout_seconds": "not-a-number"},
        )

        assert response.status_code == 422
        body = response.json()
        assert body["success"] is False
        assert "timeout_seconds" in body["message"]
        assert body["detail"][0]["field"] == "timeout_seconds"
        # The rejected value may be a credential — never echo it back.
        assert "not-a-number" not in json.dumps(body)

    def test_real_spec_reaches_the_typed_input(self) -> None:
        body = _normalize_preflight_request(
            {
                "credentials": {"extra": {}},
                "metadata": {
                    "agent-json": {
                        "agent-name": "acme-agent",
                        "secret-path": "arn:aws:secretsmanager:us-east-1:1:secret:x",
                        "host": "db.example.com",
                        "port": 1521,
                    }
                },
            }
        )
        validated = PreflightInput.model_validate(body)
        assert validated.agent_json is not None
        assert validated.agent_json.agent_name == "acme-agent"
        assert validated.agent_json.is_populated()

    def test_only_an_explicit_ingress_call_can_answer_422(self) -> None:
        """The structural half of the rule the docstrings describe.

        A 422 requires ``_validate_request``, which is the only thing that
        raises the marker the handler is registered on. A bare
        ``model_validate`` — a future endpoint validating inside its own error
        boundary, an output contract, a handler's own model — raises plain
        pydantic and so cannot be reported as the caller's fault. Without this,
        "which endpoints validate outside their boundary" is a convention held
        up by prose, and forgetting it silently returns a wrong 422.
        """
        bad = {"timeout_seconds": "not-a-number"}

        with pytest.raises(_RequestContractError) as ingress:
            _validate_request(PreflightInput, bad)
        # The pydantic failure is carried, not flattened — the handler needs
        # the field paths.
        assert ingress.value.cause.errors()[0]["loc"] == ("timeout_seconds",)

        # ...and the bare call is untouched: still plain pydantic.
        with pytest.raises(ValidationError):
            PreflightInput.model_validate(bad)

    def test_a_stray_validation_error_is_not_a_422(self) -> None:
        """A ValidationError escaping a route is a 500, whatever raises it.

        Starlette routes any escaping exception to a registered handler, so
        registering the 422 on pydantic's ``ValidationError`` would have turned
        every internal validation failure — anywhere in the app, including a
        handler's own models — into "the request did not fit the contract".
        """

        class _StrayValidationHandler(_TestHandler):
            async def preflight_check(self, input: PreflightInput) -> PreflightOutput:
                # Business logic validating something of its own, mid-request.
                outer_model = type(input)
                outer_model.model_validate({"timeout_seconds": "not-a-number"})
                raise AssertionError("unreachable")

        client = TestClient(
            create_app_handler_service(
                _StrayValidationHandler(), app_name="stray-validation"
            ),
            raise_server_exceptions=False,
        )
        response = client.post("/workflows/v1/check", json={"credentials": []})

        assert response.status_code == 500
        assert response.json()["detail"] == "Internal server error"

    def test_start_contract_misfit_answers_422_naming_the_field(self) -> None:
        """A ``/start`` body that does not fit the input contract is a 422 (FND-501).

        The body is the caller's workflow configuration: a misfit never
        succeeds on retry, so it is rejected naming the field — and before
        Temporal is touched, so the rejection does not depend on Temporal
        being reachable.
        """
        from unittest.mock import AsyncMock, MagicMock

        from application_sdk.app.base import App
        from application_sdk.app.entrypoint import entrypoint

        class _StartMisfitApp(App):
            @entrypoint
            async def run(self, input: _RoutingInput) -> _RoutingOutput:
                return _RoutingOutput()

        svc = create_app_handler_service(
            _TestHandler(),
            app_name="start-misfit",
            app_class=_StartMisfitApp,
            temporal_host="temporal:7233",
        )
        get_client = AsyncMock(return_value=MagicMock())
        with patch(
            "application_sdk.handler.service._get_temporal_client", new=get_client
        ):
            # Input.correlation_id is a `str`; pydantic v2 rejects an int.
            response = TestClient(svc, raise_server_exceptions=False).post(
                "/workflows/v1/start", json={"correlation_id": 123}
            )

        assert response.status_code == 422
        body = response.json()
        assert body["success"] is False
        assert [d["field"] for d in body["detail"]] == ["correlation_id"]
        assert "123" not in response.text  # the rejected value is never echoed
        get_client.assert_not_awaited()

    def test_start_internal_validation_failure_stays_a_500(self) -> None:
        """A ValidationError raised past body validation is still a sanitized 500.

        Only the body's contract check answers 422; a ValidationError from
        the SDK's own stamping is an internal bug, never "the request did
        not fit the contract".
        """
        from unittest.mock import AsyncMock, MagicMock

        from application_sdk.app.base import App
        from application_sdk.app.entrypoint import entrypoint

        class _StartInternalApp(App):
            @entrypoint
            async def run(self, input: _RoutingInput) -> _RoutingOutput:
                return _RoutingOutput()

        svc = create_app_handler_service(
            _TestHandler(),
            app_name="start-internal",
            app_class=_StartInternalApp,
            temporal_host="temporal:7233",
        )

        def _stamp_fails(*_args: object, **_kwargs: object) -> str:
            _RoutingInput.model_validate({"correlation_id": 123})
            raise AssertionError("unreachable")

        with (
            patch(
                "application_sdk.handler.service._get_temporal_client",
                new=AsyncMock(return_value=MagicMock()),
            ),
            patch(
                "application_sdk.handler.service.resolve_dispatch_workflow_id",
                new=_stamp_fails,
            ),
        ):
            response = TestClient(svc, raise_server_exceptions=False).post(
                "/workflows/v1/start", json={}
            )

        assert response.status_code == 500
        assert response.json()["detail"] == "Failed to start workflow"


@pytest.mark.usefixtures("clean_app_registry", "clean_task_registry")
class TestValidateWorkflowConfig:
    """``POST /workflows/v1/validate`` — a dry-run ``/start`` (FND-501)."""

    @staticmethod
    def _client(*, temporal_host: str = "") -> TestClient:
        from application_sdk.app.base import App
        from application_sdk.app.entrypoint import entrypoint

        class _ValidateRouteApp(App):
            @entrypoint
            async def run(self, input: _RoutingInput) -> _RoutingOutput:
                return _RoutingOutput()

        return TestClient(
            create_app_handler_service(
                _TestHandler(),
                app_name="validate-route",
                app_class=_ValidateRouteApp,
                temporal_host=temporal_host,
            ),
            raise_server_exceptions=False,
        )

    def test_valid_config_answers_200_without_temporal(self) -> None:
        response = self._client().post(
            "/workflows/v1/validate",
            json={"name": "ok", "credentials": [{"key": "password", "value": "s"}]},
        )

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["success"] is True
        assert body["data"]["entrypoint"] == "run"

    def test_invalid_config_answers_the_same_422_as_start(self) -> None:
        response = self._client().post(
            "/workflows/v1/validate", json={"correlation_id": 123}
        )

        assert response.status_code == 422
        assert [d["field"] for d in response.json()["detail"]] == ["correlation_id"]

    def test_validate_never_dispatches(self) -> None:
        from unittest.mock import AsyncMock

        get_client = AsyncMock()
        with patch(
            "application_sdk.handler.service._get_temporal_client", new=get_client
        ):
            response = self._client(temporal_host="temporal:7233").post(
                "/workflows/v1/validate", json={"name": "ok"}
            )

        assert response.status_code == 200
        get_client.assert_not_awaited()

    def test_unknown_entrypoint_is_rejected(self) -> None:
        response = self._client().post(
            "/workflows/v1/validate?entrypoint=no-such-entrypoint", json={}
        )

        # Same resolution, and so the same answer, as /input-contract.
        assert response.status_code == 404

    def test_non_object_body_answers_422(self) -> None:
        response = self._client().post("/workflows/v1/validate", json=[1, 2])

        assert response.status_code == 422

    def test_rejected_filter_pattern_is_not_echoed(self) -> None:
        """A regex rejection names the field, never the pattern (no-echo)."""
        from application_sdk.app.base import App
        from application_sdk.app.entrypoint import entrypoint

        class _ValidateFilterApp(App):
            @entrypoint
            async def run(self, input: ExtractionInput) -> ExtractionOutput:
                return ExtractionOutput()

        client = TestClient(
            create_app_handler_service(
                _TestHandler(),
                app_name="validate-filter",
                app_class=_ValidateFilterApp,
            ),
            raise_server_exceptions=False,
        )
        response = client.post(
            "/workflows/v1/validate",
            json={"include_filter": {"^db$": ["^SECRET_SCHEMA["]}},
        )

        assert response.status_code == 422
        assert [d["field"] for d in response.json()["detail"]] == ["include_filter"]
        assert "SECRET_SCHEMA" not in response.text

    def test_each_service_resolves_against_its_own_app(self) -> None:
        """A second service built in-process must not repoint the first.

        ``create_app_handler_service`` rebinds the module-level workflow
        config; the routes resolve against the config captured when their
        own service was built.
        """
        from application_sdk.app.base import App
        from application_sdk.app.entrypoint import entrypoint

        class _FirstServiceApp(App):
            @entrypoint
            async def run(self, input: _RoutingInput) -> _RoutingOutput:
                return _RoutingOutput()

        class _SecondServiceApp(App):
            @entrypoint
            async def extract(self, input: _RoutingInput) -> _RoutingOutput:
                return _RoutingOutput()

        first = TestClient(
            create_app_handler_service(
                _TestHandler(), app_name="first-svc", app_class=_FirstServiceApp
            ),
            raise_server_exceptions=False,
        )
        before = first.post("/workflows/v1/validate", json={}).json()
        create_app_handler_service(
            _TestHandler(), app_name="second-svc", app_class=_SecondServiceApp
        )

        response = first.post("/workflows/v1/validate", json={})

        assert response.status_code == 200, response.text
        assert response.json()["data"] == before["data"]
        assert response.json()["data"]["entrypoint"] == "run"


class TestManifestPostTransport:
    """``POST /manifest`` — the body transport for ``fe_inputs`` (CSA-539).

    GET keeps its existing behaviour, cap included; these pin that too, since
    the playground, connector tests and AE/marketplace probes still use it.
    """

    @staticmethod
    def _install_fake_core(
        monkeypatch: pytest.MonkeyPatch, entrypoint: str, fn: object
    ) -> None:
        _install_fake_app_module(monkeypatch, entrypoint, "core", compute_manifest=fn)

    @staticmethod
    def _make_ep(tmp_path: Path, entrypoint: str, manifest: dict | None = None) -> Path:
        contract_dir = tmp_path / "generated"
        ep_dir = contract_dir / entrypoint
        ep_dir.mkdir(parents=True)
        (ep_dir / "manifest.json").write_text(json.dumps(manifest or {}))
        return contract_dir

    def test_post_passes_fe_inputs_body_to_hook(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from application_sdk.handler import service as svc_module

        captured: dict = {}

        async def compute_manifest(manifest: dict, fe_inputs: dict) -> dict:
            captured["fe_inputs"] = fe_inputs
            return {"dag": {"extract": {"echo": fe_inputs}}}

        contract_dir = self._make_ep(tmp_path, "hook-ep")
        self._install_fake_core(monkeypatch, "hook-ep", compute_manifest)

        payload = {"attribute-selector": "table:a,column:b", "zip-flag": "false"}
        original = svc_module.CONTRACT_GENERATED_DIR
        svc_module.CONTRACT_GENERATED_DIR = contract_dir
        try:
            response = _make_client().post(
                "/workflows/v1/manifest",
                json={"entrypoint": "hook-ep", "fe_inputs": payload},
            )
            assert response.status_code == 200
            assert captured["fe_inputs"] == payload
            assert response.json()["dag"]["extract"]["echo"] == payload
        finally:
            svc_module.CONTRACT_GENERATED_DIR = original

    def test_post_accepts_payload_that_get_rejects_with_413(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The actual CSA-539 fix: same payload, 413 on GET, 200 on POST."""
        from application_sdk.handler import service as svc_module

        async def compute_manifest(manifest: dict, fe_inputs: dict) -> dict:
            return {"dag": {"n": len(fe_inputs["attribute-selector"])}}

        contract_dir = self._make_ep(tmp_path, "big-form")
        self._install_fake_core(monkeypatch, "big-form", compute_manifest)

        # Comfortably over the query-string cap.
        oversized = {
            "attribute-selector": ",".join(f"table:attr{i:04d}" for i in range(900))
        }
        raw = json.dumps(oversized)
        assert len(raw.encode("utf-8")) > svc_module._MAX_FE_INPUTS_BYTES

        original = svc_module.CONTRACT_GENERATED_DIR
        svc_module.CONTRACT_GENERATED_DIR = contract_dir
        try:
            client = _make_client()

            get_resp = client.get(
                "/workflows/v1/manifest",
                params={"entrypoint": "big-form", "fe_inputs": raw},
            )
            assert get_resp.status_code == 413

            post_resp = client.post(
                "/workflows/v1/manifest",
                json={"entrypoint": "big-form", "fe_inputs": oversized},
            )
            assert post_resp.status_code == 200
            assert post_resp.json()["dag"]["n"] == len(oversized["attribute-selector"])
        finally:
            svc_module.CONTRACT_GENERATED_DIR = original

    def test_get_and_post_agree_for_the_same_inputs(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Transport must not change the answer."""
        from application_sdk.handler import service as svc_module

        async def compute_manifest(manifest: dict, fe_inputs: dict) -> dict:
            return {"dag": {"echo": fe_inputs}, "execution_mode": "native"}

        contract_dir = self._make_ep(tmp_path, "parity-ep")
        self._install_fake_core(monkeypatch, "parity-ep", compute_manifest)

        payload = {"a": "1", "b": ["x", "y"], "c": {"nested": True}}
        original = svc_module.CONTRACT_GENERATED_DIR
        svc_module.CONTRACT_GENERATED_DIR = contract_dir
        try:
            client = _make_client()
            got = client.get(
                "/workflows/v1/manifest",
                params={"entrypoint": "parity-ep", "fe_inputs": json.dumps(payload)},
            )
            posted = client.post(
                "/workflows/v1/manifest",
                json={"entrypoint": "parity-ep", "fe_inputs": payload},
            )
            assert got.status_code == posted.status_code == 200
            assert got.json() == posted.json()
        finally:
            svc_module.CONTRACT_GENERATED_DIR = original

    def test_post_empty_body_serves_static_manifest(self, tmp_path: Path) -> None:
        """``{}`` is equivalent to a bare GET — no entrypoint, no fe_inputs."""
        from application_sdk.handler import service as svc_module

        contract_dir = tmp_path / "generated"
        contract_dir.mkdir(parents=True)
        (contract_dir / "manifest.json").write_text(
            json.dumps({"execution_mode": "native", "app_name": "flat-app"})
        )

        original = svc_module.CONTRACT_GENERATED_DIR
        svc_module.CONTRACT_GENERATED_DIR = contract_dir
        try:
            response = _make_client().post("/workflows/v1/manifest", json={})
            assert response.status_code == 200
            assert response.json()["app_name"] == "flat-app"
        finally:
            svc_module.CONTRACT_GENERATED_DIR = original

    def test_post_resolves_entrypoint(self, tmp_path: Path) -> None:
        from application_sdk.handler import service as svc_module

        contract_dir = self._make_ep(
            tmp_path, "snowflake", {"execution_mode": "dag", "app_name": "sf"}
        )
        original = svc_module.CONTRACT_GENERATED_DIR
        svc_module.CONTRACT_GENERATED_DIR = contract_dir
        try:
            response = _make_client().post(
                "/workflows/v1/manifest", json={"entrypoint": "snowflake"}
            )
            assert response.status_code == 200
            assert response.json()["app_name"] == "sf"
        finally:
            svc_module.CONTRACT_GENERATED_DIR = original

    def test_post_substitutes_deployment_name(self, tmp_path: Path) -> None:
        from application_sdk.handler import service as svc_module

        contract_dir = self._make_ep(
            tmp_path, "ep1", {"task_queue": "{deployment_name}-queue"}
        )
        original_dir = svc_module.CONTRACT_GENERATED_DIR
        original_dep = svc_module.DEPLOYMENT_NAME
        svc_module.CONTRACT_GENERATED_DIR = contract_dir
        svc_module.DEPLOYMENT_NAME = "tenant-x"
        try:
            response = _make_client().post(
                "/workflows/v1/manifest", json={"entrypoint": "ep1"}
            )
            assert response.status_code == 200
            assert response.json()["task_queue"] == "tenant-x-queue"
        finally:
            svc_module.CONTRACT_GENERATED_DIR = original_dir
            svc_module.DEPLOYMENT_NAME = original_dep

    def test_post_legacy_unversioned_alias(self, tmp_path: Path) -> None:
        """Callers migrate method and path independently."""
        from application_sdk.handler import service as svc_module

        contract_dir = self._make_ep(tmp_path, "legacy-ep", {"app_name": "legacy"})
        original = svc_module.CONTRACT_GENERATED_DIR
        svc_module.CONTRACT_GENERATED_DIR = contract_dir
        try:
            response = _make_client().post(
                "/manifest", json={"entrypoint": "legacy-ep"}
            )
            assert response.status_code == 200
            assert response.json()["app_name"] == "legacy"
        finally:
            svc_module.CONTRACT_GENERATED_DIR = original

    def test_post_user_id_is_accepted_and_ignored(self, tmp_path: Path) -> None:
        """Wire parity with the GET param, which the SDK also ignores."""
        from application_sdk.handler import service as svc_module

        contract_dir = self._make_ep(tmp_path, "ep-u", {"app_name": "u"})
        original = svc_module.CONTRACT_GENERATED_DIR
        svc_module.CONTRACT_GENERATED_DIR = contract_dir
        try:
            response = _make_client().post(
                "/workflows/v1/manifest",
                json={"entrypoint": "ep-u", "user_id": "kc-user-guid"},
            )
            assert response.status_code == 200
            assert response.json()["app_name"] == "u"
        finally:
            svc_module.CONTRACT_GENERATED_DIR = original

    @pytest.mark.parametrize(
        "kwargs",
        [
            pytest.param({"json": {}}, id="empty-object"),
            pytest.param({"json": {"fe_inputs": None}}, id="fe_inputs-null"),
            pytest.param({"json": {"entrypoint": None}}, id="entrypoint-null"),
            pytest.param({"content": b"null"}, id="literal-null-body"),
            pytest.param({"content": b""}, id="empty-body"),
            pytest.param({}, id="no-body-at-all"),
        ],
    )
    def test_post_empty_ish_bodies_behave_like_a_bare_get(
        self, tmp_path: Path, kwargs: dict
    ) -> None:
        """All of these mean "no inputs". Callers fall back only on 405, so a 400
        here would hard-fail them instead of degrading."""
        from application_sdk.handler import service as svc_module

        contract_dir = tmp_path / "generated"
        contract_dir.mkdir(parents=True)
        (contract_dir / "manifest.json").write_text(
            json.dumps({"app_name": "flat-app"})
        )

        original = svc_module.CONTRACT_GENERATED_DIR
        svc_module.CONTRACT_GENERATED_DIR = contract_dir
        try:
            response = _make_client().post("/workflows/v1/manifest", **kwargs)
            assert response.status_code == 200
            assert response.json()["app_name"] == "flat-app"
        finally:
            svc_module.CONTRACT_GENERATED_DIR = original

    def test_post_fe_inputs_must_be_an_object_not_a_json_string(self) -> None:
        """A caller porting a GET query param must pass the decoded object, not
        the JSON string the query string carried."""
        response = _make_client().post(
            "/workflows/v1/manifest",
            json={"fe_inputs": json.dumps({"a": "b"})},
        )
        assert response.status_code == 400
        assert "fe_inputs must be a JSON object" in response.json()["detail"]

    def test_post_malformed_json_returns_400(self) -> None:
        response = _make_client().post(
            "/workflows/v1/manifest",
            content=b"{not json",
            headers={"Content-Type": "application/json"},
        )
        assert response.status_code == 400
        assert "not valid JSON" in response.json()["detail"]

    def test_post_non_object_body_returns_400(self) -> None:
        response = _make_client().post("/workflows/v1/manifest", json=["a", "b"])
        assert response.status_code == 400
        assert "must be a JSON object" in response.json()["detail"]

    def test_post_non_object_fe_inputs_returns_400(self) -> None:
        """Same contract as the GET path's 'fe_inputs must be a JSON object'."""
        response = _make_client().post(
            "/workflows/v1/manifest", json={"fe_inputs": "a,b,c"}
        )
        assert response.status_code == 400
        assert "fe_inputs must be a JSON object" in response.json()["detail"]

    def test_post_non_string_entrypoint_returns_400(self) -> None:
        response = _make_client().post(
            "/workflows/v1/manifest", json={"entrypoint": 42}
        )
        assert response.status_code == 400
        assert "entrypoint must be a string" in response.json()["detail"]

    def test_get_still_enforces_the_query_cap(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Regression guard: adding POST must not relax the GET path."""
        from application_sdk.handler import service as svc_module

        async def compute_manifest(manifest: dict, fe_inputs: dict) -> dict:
            raise AssertionError("hook must not run for oversize fe_inputs on GET")

        contract_dir = self._make_ep(tmp_path, "capped")
        self._install_fake_core(monkeypatch, "capped", compute_manifest)

        huge = json.dumps({"x": "a" * (svc_module._MAX_FE_INPUTS_BYTES + 100)})
        original = svc_module.CONTRACT_GENERATED_DIR
        svc_module.CONTRACT_GENERATED_DIR = contract_dir
        try:
            response = _make_client().get(
                "/workflows/v1/manifest",
                params={"entrypoint": "capped", "fe_inputs": huge},
            )
            assert response.status_code == 413
        finally:
            svc_module.CONTRACT_GENERATED_DIR = original

    def test_get_with_oversize_fe_inputs_still_ok_without_a_hook(
        self, tmp_path: Path
    ) -> None:
        """Decoding must stay lazy — at the route it would 413 static apps."""
        from application_sdk.handler import service as svc_module

        contract_dir = self._make_ep(tmp_path, "static-ep", {"app_name": "static"})
        huge = json.dumps({"x": "a" * (svc_module._MAX_FE_INPUTS_BYTES + 100)})

        original = svc_module.CONTRACT_GENERATED_DIR
        svc_module.CONTRACT_GENERATED_DIR = contract_dir
        try:
            response = _make_client().get(
                "/workflows/v1/manifest",
                params={"entrypoint": "static-ep", "fe_inputs": huge},
            )
            assert response.status_code == 200
            assert response.json()["app_name"] == "static"
        finally:
            svc_module.CONTRACT_GENERATED_DIR = original


@pytest.mark.usefixtures("clean_app_registry", "clean_task_registry")
class TestBundleMarketplaceEntrypoints:
    """Bundle ("uber") apps expose *marketplace* entry points only as generated
    contract dirs (``app/generated/<ep>/manifest.json``); their registry
    entrypoints are the DAG-node workflows (e.g. ``export-post``). The
    input-contract route resolves the marketplace names from disk instead of
    404-ing (production RCA: /v1/app creation failed for every bundle entry
    point with 'No input contract for entrypoint'). Bare /manifest stays 404
    by design until the declarative resolver (FND-180) can pick correctly."""

    @staticmethod
    def _bundle_app() -> type:
        from application_sdk.app.base import App
        from application_sdk.app.entrypoint import entrypoint

        class _BundleApp(App):
            # Registry knows only the DAG-node workflows, never the
            # marketplace entry point ("export") itself.
            @entrypoint
            async def export_post(self, input: _RoutingInput) -> _RoutingOutput:
                return _RoutingOutput()

            @entrypoint
            async def export_build_raw_sql(
                self, input: _RoutingInput
            ) -> _RoutingOutput:
                return _RoutingOutput()

        return _BundleApp

    def _client(self, app_cls: type) -> TestClient:
        svc = create_app_handler_service(
            _TestHandler(), app_name="bundle-test", app_class=app_cls
        )
        return TestClient(svc, raise_server_exceptions=False)

    def test_input_contract_resolves_marketplace_entrypoint_from_disk(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """?entrypoint=<marketplace-name> misses the registry but has a
        generated dir + relocated AppInputContract → 200 with its schema."""
        import sys
        import types

        from pydantic import BaseModel

        from application_sdk.handler import service as svc_module

        (tmp_path / "export").mkdir()
        (tmp_path / "export" / "manifest.json").write_text("{}")

        class AppInputContract(BaseModel):
            delivery_type: str = "DIRECT"

        module = types.ModuleType("app.export._input")
        module.AppInputContract = AppInputContract  # type: ignore[attr-defined]

        # monkeypatch restores any app.export._input a prior import left.
        monkeypatch.setattr(svc_module, "CONTRACT_GENERATED_DIR", tmp_path)
        monkeypatch.setitem(sys.modules, "app.export._input", module)
        resp = self._client(self._bundle_app()).get(
            "/workflows/v1/input-contract?entrypoint=export"
        )
        assert resp.status_code == 200
        assert resp.json() == AppInputContract.model_json_schema()

    def test_input_contract_404_when_no_generated_dir(self, tmp_path: Path) -> None:
        """A name with neither a registry entry nor a generated dir keeps the
        existing 404 contract."""
        from application_sdk.handler import service as svc_module

        original = svc_module.CONTRACT_GENERATED_DIR
        svc_module.CONTRACT_GENERATED_DIR = tmp_path
        try:
            resp = self._client(self._bundle_app()).get(
                "/workflows/v1/input-contract?entrypoint=export"
            )
            assert resp.status_code == 404
        finally:
            svc_module.CONTRACT_GENERATED_DIR = original

    def test_input_contract_404_when_dir_has_no_contract_module(
        self, tmp_path: Path
    ) -> None:
        """A generated dir without an importable AppInputContract still 404s
        (never a 500, never a wrong sibling's schema)."""
        from application_sdk.handler import service as svc_module

        (tmp_path / "export").mkdir()
        (tmp_path / "export" / "manifest.json").write_text("{}")

        original = svc_module.CONTRACT_GENERATED_DIR
        svc_module.CONTRACT_GENERATED_DIR = tmp_path
        try:
            resp = self._client(self._bundle_app()).get(
                "/workflows/v1/input-contract?entrypoint=export"
            )
            assert resp.status_code == 404
        finally:
            svc_module.CONTRACT_GENERATED_DIR = original

    def test_bare_manifest_on_bundle_still_404s(self, tmp_path: Path) -> None:
        """A bare /manifest call on a bundle app keeps 404-ing even when
        marketplace entry-point dirs exist on disk. Serving one of N
        marketplace manifests picked arbitrarily would be confidently wrong —
        callers must name the entry point. (The declarative resolver that can
        answer this properly is FND-180.)"""
        from application_sdk.handler import service as svc_module

        for name in ("beta-export", "alpha-export"):
            (tmp_path / name).mkdir()
            (tmp_path / name / "manifest.json").write_text(json.dumps({"name": name}))

        original = svc_module.CONTRACT_GENERATED_DIR
        svc_module.CONTRACT_GENERATED_DIR = tmp_path
        try:
            resp = self._client(self._bundle_app()).get("/workflows/v1/manifest")
            assert resp.status_code == 404
            assert resp.json()["detail"] == "No manifest available"
        finally:
            svc_module.CONTRACT_GENERATED_DIR = original
