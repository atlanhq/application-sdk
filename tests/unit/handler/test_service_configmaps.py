"""Handler service tests: the configmap and workflow-config endpoints."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from application_sdk.handler.service import (
    create_app_handler_service,
)
from tests.unit.handler._service_support import (
    _AlphaInput,
    _AlphaOutput,
    _make_client,
    _RoutingInput,
    _RoutingOutput,
    _TestHandler,
)


class TestConfigMapEndpoints:
    """Tests for GET /workflows/v1/configmap/{id} and /configmaps."""

    def test_configmap_not_found_returns_404_and_logs_available_stems(
        self, tmp_path: Path
    ) -> None:
        from application_sdk.handler import service as svc_module

        (tmp_path / "sap-hana.json").write_text("{}")

        original = svc_module.CONTRACT_GENERATED_DIR
        svc_module.CONTRACT_GENERATED_DIR = tmp_path
        try:
            client = _make_client()
            with patch("application_sdk.handler.service.logger") as mock_logger:
                response = client.get("/workflows/v1/configmap/saphana")
            assert response.status_code == 404
            assert response.json()["detail"] == "ConfigMap 'saphana' not found"
            # assert_any_call, not assert_called_once_with: the no-default
            # -entrypoint fallback earlier in the handler also logs a
            # warning (with exc_info) when the app has no default
            # entrypoint — this test only cares that the configmap-specific
            # warning below was among them, not that it's the only one.
            mock_logger.warning.assert_any_call(
                "ConfigMap not found: requested=%s available=%s",
                "saphana",
                ["sap-hana"],
            )
        finally:
            svc_module.CONTRACT_GENERATED_DIR = original

    # ── Build identity (FND-1684) ────────────────────────────────────────
    #
    # The e2e version check used to read LM's marketplace install record — the
    # same record the install writes and then skips on — so once that record
    # existed for a build the check could only agree with it, whatever the
    # cluster was actually serving.
    # This route is the fix: it is the one answer only a RUNNING POD can give,
    # and it rides the already-proxied configmap route so no new Heracles rule
    # is needed.

    def test_build_identity_is_served_from_the_image_env(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from application_sdk.common.build_identity import (
            BUILD_ID_ENV,
            BUILD_IDENTITY_CONFIGMAP_ID,
        )
        from application_sdk.handler import service as svc_module

        monkeypatch.setenv(BUILD_ID_ENV, "sdr-test-abc12345")
        original = svc_module.CONTRACT_GENERATED_DIR
        svc_module.CONTRACT_GENERATED_DIR = tmp_path
        try:
            client = _make_client()
            response = client.get(
                f"/workflows/v1/configmap/{BUILD_IDENTITY_CONFIGMAP_ID}"
            )
            assert response.status_code == 200
            data = response.json()["data"]
            assert data["metadata"]["name"] == BUILD_IDENTITY_CONFIGMAP_ID
            # Both halves, because the CI reader accepts either: `data.config`
            # is the envelope every other configmap uses, and the flattened
            # keys spare a client that does not unwrap it.
            assert data["data"]["build_id"] == "sdr-test-abc12345"
            assert json.loads(data["data"]["config"])["build_id"] == (
                "sdr-test-abc12345"
            )
        finally:
            svc_module.CONTRACT_GENERATED_DIR = original

    def test_build_identity_is_empty_not_404_for_an_unstamped_image(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """ "No stamp" and "no such route" need opposite next steps.

        A 404 would collapse an image built without the stamp into the same
        answer as an SDK too old to serve the route, and the CI check decides
        differently on each.
        """
        from application_sdk.common.build_identity import (
            BUILD_ID_ENV,
            BUILD_IDENTITY_CONFIGMAP_ID,
        )
        from application_sdk.handler import service as svc_module

        monkeypatch.delenv(BUILD_ID_ENV, raising=False)
        original = svc_module.CONTRACT_GENERATED_DIR
        svc_module.CONTRACT_GENERATED_DIR = tmp_path
        try:
            client = _make_client()
            response = client.get(
                f"/workflows/v1/configmap/{BUILD_IDENTITY_CONFIGMAP_ID}"
            )
            assert response.status_code == 200
            assert response.json()["data"]["data"]["build_id"] == ""
        finally:
            svc_module.CONTRACT_GENERATED_DIR = original

    def test_a_generated_file_cannot_shadow_the_build_identity(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The reserved id is answered before the generated-file scan.

        A committed file cannot distinguish this build from one shipped months
        ago, which is the whole reason this route exists — so an app that ships
        a file of this name must not be able to answer in the pod's place.
        """
        from application_sdk.common.build_identity import (
            BUILD_ID_ENV,
            BUILD_IDENTITY_CONFIGMAP_ID,
        )
        from application_sdk.handler import service as svc_module

        (tmp_path / f"{BUILD_IDENTITY_CONFIGMAP_ID}.json").write_text(
            json.dumps({"config": {"build_id": "committed-lie"}})
        )
        monkeypatch.setenv(BUILD_ID_ENV, "sdr-test-abc12345")
        original = svc_module.CONTRACT_GENERATED_DIR
        svc_module.CONTRACT_GENERATED_DIR = tmp_path
        try:
            client = _make_client()
            response = client.get(
                f"/workflows/v1/configmap/{BUILD_IDENTITY_CONFIGMAP_ID}"
            )
            assert response.json()["data"]["data"]["build_id"] == "sdr-test-abc12345"
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
            # Absent from the file -> absent from the response.
            assert "defaultConnectorType" not in data["data"]
        finally:
            svc_module.CONTRACT_GENERATED_DIR = original

    def test_configmap_includes_default_connector_type_when_present(
        self, tmp_path: Path
    ) -> None:
        from application_sdk.handler import service as svc_module

        raw = {"defaultConnectorType": "jdbc", "config": {"key": "value"}}
        (tmp_path / "my-config.json").write_text(json.dumps(raw))

        original = svc_module.CONTRACT_GENERATED_DIR
        svc_module.CONTRACT_GENERATED_DIR = tmp_path
        try:
            client = _make_client()
            response = client.get("/workflows/v1/configmap/my-config")
            assert response.status_code == 200
            data = response.json()["data"]
            assert data["data"]["defaultConnectorType"] == "jdbc"
            parsed_config = json.loads(data["data"]["config"])
            assert parsed_config == {"key": "value"}
        finally:
            svc_module.CONTRACT_GENERATED_DIR = original

    def test_configmap_default_entrypoint_resolves_form_when_bare_id(
        self, tmp_path: Path
    ) -> None:
        """Bare-id requests resolve to the default entrypoint's form configmap.

        The marketplace UI builds configmap URLs from the app/marketplace id
        (e.g. ``atlan-snowflake``) rather than the entrypoint form stem
        (``snowflake-crawler``). When no file stem matches the request id
        exactly, the handler resolves the app's default entrypoint (same
        semantics as ``/workflows/v1/start`` and
        ``/workflows/v1/input-contract``) and serves the form configmap
        living under that entrypoint's generated dir.
        """
        from application_sdk.app.base import App
        from application_sdk.app.entrypoint import entrypoint
        from application_sdk.handler import service as svc_module

        class _MultiEpApp(App):
            @entrypoint
            async def miner(self, input: _AlphaInput) -> _AlphaOutput:
                return _AlphaOutput()

            @entrypoint(default=True)
            async def crawler(self, input: _RoutingInput) -> _RoutingOutput:
                return _RoutingOutput()

        crawler_dir = tmp_path / "crawler"
        crawler_dir.mkdir()
        (crawler_dir / "manifest.json").write_text(json.dumps({"dag": {}}))
        (crawler_dir / "snowflake-crawler.json").write_text(
            json.dumps({"config": {"key": "from-crawler-form"}})
        )

        original = svc_module.CONTRACT_GENERATED_DIR
        svc_module.CONTRACT_GENERATED_DIR = tmp_path
        try:
            # app_name= sets the task-queue name only; _resolve_app_entrypoint
            # keys on _MultiEpApp._app_name (kebab of the class name).
            svc = create_app_handler_service(
                _TestHandler(), app_name="snowflake", app_class=_MultiEpApp
            )
            client = TestClient(svc, raise_server_exceptions=False)
            response = client.get("/workflows/v1/configmap/atlan-snowflake")
            assert response.status_code == 200
            data = response.json()["data"]
            # Echoed back as the request id, not the file stem — the bare
            # marketplace id is what the caller asked for.
            assert data["metadata"]["name"] == "atlan-snowflake"
            parsed_config = json.loads(data["data"]["config"])
            assert parsed_config["key"] == "from-crawler-form"
        finally:
            svc_module.CONTRACT_GENERATED_DIR = original

    def test_configmap_exact_match_wins_over_default_fallback(
        self, tmp_path: Path
    ) -> None:
        """Exact file-stem match preempts the default-entrypoint fallback."""
        from application_sdk.app.base import App
        from application_sdk.app.entrypoint import entrypoint
        from application_sdk.handler import service as svc_module

        class _OneEpApp(App):
            @entrypoint
            async def crawler(self, input: _RoutingInput) -> _RoutingOutput:
                return _RoutingOutput()

        # Both an exact stem AND a default-entrypoint form file exist.
        (tmp_path / "snowflake.json").write_text(
            json.dumps({"config": {"key": "exact"}})
        )
        crawler_dir = tmp_path / "crawler"
        crawler_dir.mkdir()
        (crawler_dir / "snowflake-crawler.json").write_text(
            json.dumps({"config": {"key": "fallback"}})
        )

        original = svc_module.CONTRACT_GENERATED_DIR
        svc_module.CONTRACT_GENERATED_DIR = tmp_path
        try:
            # app_name= sets the task-queue name only; _resolve_app_entrypoint
            # keys on _OneEpApp._app_name (kebab of the class name).
            svc = create_app_handler_service(
                _TestHandler(), app_name="snowflake", app_class=_OneEpApp
            )
            client = TestClient(svc, raise_server_exceptions=False)
            response = client.get("/workflows/v1/configmap/snowflake")
            assert response.status_code == 200
            parsed_config = json.loads(response.json()["data"]["data"]["config"])
            assert parsed_config["key"] == "exact"
        finally:
            svc_module.CONTRACT_GENERATED_DIR = original

    def test_configmap_default_fallback_skips_manifest_and_credentials(
        self, tmp_path: Path
    ) -> None:
        """Default-entrypoint fallback skips manifest.json and atlan-connectors-*.json.

        The entrypoint's generated dir contains the DAG manifest and (for
        connectors) a credential template alongside the form configmap. The
        fallback must serve the form, not the credential schema.
        """
        from application_sdk.app.base import App
        from application_sdk.app.entrypoint import entrypoint
        from application_sdk.handler import service as svc_module

        class _OneEpApp(App):
            @entrypoint
            async def crawler(self, input: _RoutingInput) -> _RoutingOutput:
                return _RoutingOutput()

        crawler_dir = tmp_path / "crawler"
        crawler_dir.mkdir()
        (crawler_dir / "manifest.json").write_text(json.dumps({"dag": {}}))
        (crawler_dir / "atlan-connectors-snowflake.json").write_text(
            json.dumps({"config": {"key": "credential-schema"}})
        )
        (crawler_dir / "snowflake-crawler.json").write_text(
            json.dumps({"config": {"key": "form-config"}})
        )

        original = svc_module.CONTRACT_GENERATED_DIR
        svc_module.CONTRACT_GENERATED_DIR = tmp_path
        try:
            # app_name= sets the task-queue name only; _resolve_app_entrypoint
            # keys on _OneEpApp._app_name (kebab of the class name).
            svc = create_app_handler_service(
                _TestHandler(), app_name="snowflake", app_class=_OneEpApp
            )
            client = TestClient(svc, raise_server_exceptions=False)
            response = client.get("/workflows/v1/configmap/atlan-snowflake")
            assert response.status_code == 200
            parsed_config = json.loads(response.json()["data"]["data"]["config"])
            assert parsed_config["key"] == "form-config"
        finally:
            svc_module.CONTRACT_GENERATED_DIR = original

    def test_configmap_default_fallback_serves_flat_single_entrypoint_form(
        self, tmp_path: Path
    ) -> None:
        """App-id request resolves a FLAT single-entrypoint form configmap.

        Single-entrypoint apps emit their form flat in CONTRACT_GENERATED_DIR
        (e.g. ``openapi.json`` beside ``manifest.json``), not nested under an
        ``<ep.name>/`` dir like multi-entrypoint bundles. The marketplace UI
        requests the configmap by app-id (``atlan-openapi``), which never
        matches the flat form stem (``openapi``), so the handler must fall
        through the default-entrypoint search into the FLAT dir.

        Regression: previously only the nested ``<ep.name>/`` dir was searched,
        so a flat single-entrypoint app returned 404 and its setup wizard
        rendered blank even though the form was present (observed in prod:
        ``ConfigMap not found: requested=atlan-openapi
        available=['manifest', 'openapi']``).
        """
        from application_sdk.app.base import App
        from application_sdk.app.entrypoint import entrypoint
        from application_sdk.handler import service as svc_module

        class _OneEpApp(App):
            @entrypoint
            async def crawler(self, input: _RoutingInput) -> _RoutingOutput:
                return _RoutingOutput()

        # Flat layout: form + manifest + a credential template at the ROOT, with
        # NO per-entrypoint subdir. The credential file sorts before the form
        # (c < o) and must be skipped — exercising the csa-connectors- exclusion.
        (tmp_path / "manifest.json").write_text(json.dumps({"dag": {}}))
        (tmp_path / "csa-connectors-objectstore.json").write_text(
            json.dumps({"config": {"key": "credential-schema"}})
        )
        (tmp_path / "openapi.json").write_text(
            json.dumps({"config": {"key": "flat-form"}})
        )

        original = svc_module.CONTRACT_GENERATED_DIR
        svc_module.CONTRACT_GENERATED_DIR = tmp_path
        try:
            svc = create_app_handler_service(
                _TestHandler(), app_name="openapi", app_class=_OneEpApp
            )
            client = TestClient(svc, raise_server_exceptions=False)
            response = client.get("/workflows/v1/configmap/atlan-openapi")
            assert response.status_code == 200
            data = response.json()["data"]
            assert data["metadata"]["name"] == "atlan-openapi"
            parsed_config = json.loads(data["data"]["config"])
            assert parsed_config["key"] == "flat-form"
        finally:
            svc_module.CONTRACT_GENERATED_DIR = original

    def test_configmap_default_fallback_skips_artifact_schemas(
        self, tmp_path: Path
    ) -> None:
        """App-id request must not serve ``artifact_schemas.json`` as the form.

        Regression, FND-1682. Once an app declares an ``artifactSchemas`` block
        (conformance K016) the toolkit emits ``artifact_schemas.json`` beside
        the form. It is neither the manifest nor a credential template, and it
        sorts before every plausible form stem, so the fallback's sorted scan
        picked it and the handler returned ``raw.get("config", raw)`` — the
        whole schema document, which carries no ``properties``. HTTP 200, blank
        setup wizard, nothing in the logs or the network tab. Latent for every
        app adopting K016; ``atlan-metabase-app`` was the first confirmed one
        (FND-1681).
        """
        from application_sdk.app.base import App
        from application_sdk.app.entrypoint import entrypoint
        from application_sdk.handler import service as svc_module

        class _OneEpApp(App):
            @entrypoint
            async def crawler(self, input: _RoutingInput) -> _RoutingOutput:
                return _RoutingOutput()

        # Flat single-entrypoint layout, in the order `sorted()` sees it:
        # artifact_schemas < atlan-connectors-* < manifest < the form.
        (tmp_path / "artifact_schemas.json").write_text(
            json.dumps({"version": "1.0", "artifacts": {}})
        )
        (tmp_path / "atlan-connectors-metabase.json").write_text(
            json.dumps({"config": {"key": "credential-schema"}})
        )
        (tmp_path / "manifest.json").write_text(json.dumps({"dag": {}}))
        (tmp_path / "metabase.json").write_text(
            json.dumps({"config": {"key": "form-config"}})
        )

        original = svc_module.CONTRACT_GENERATED_DIR
        svc_module.CONTRACT_GENERATED_DIR = tmp_path
        try:
            svc = create_app_handler_service(
                _TestHandler(), app_name="metabase", app_class=_OneEpApp
            )
            client = TestClient(svc, raise_server_exceptions=False)
            response = client.get("/workflows/v1/configmap/atlan-metabase")
            assert response.status_code == 200
            parsed_config = json.loads(response.json()["data"]["data"]["config"])
            assert parsed_config["key"] == "form-config"
        finally:
            svc_module.CONTRACT_GENERATED_DIR = original

    def test_configmap_default_fallback_prefers_entrypoint_named_form(
        self, tmp_path: Path
    ) -> None:
        """An unrecognised sibling that sorts first does not become the form.

        The exclusion list can only name the siblings the toolkit emits today,
        so the fallback identifies the form by name first — ``<ep.name>.json``,
        or the connector convention ``<source>-<ep.name>.json`` — and only then
        falls back to the sorted scan. ``aaa-unknown-sibling.json`` stands in
        for whatever the toolkit emits next: on no exclusion list, sorting
        first. Without the naming step this is FND-1682 again with a different
        filename.
        """
        from application_sdk.app.base import App
        from application_sdk.app.entrypoint import entrypoint
        from application_sdk.handler import service as svc_module

        class _OneEpApp(App):
            @entrypoint
            async def crawler(self, input: _RoutingInput) -> _RoutingOutput:
                return _RoutingOutput()

        crawler_dir = tmp_path / "crawler"
        crawler_dir.mkdir()
        (crawler_dir / "aaa-unknown-sibling.json").write_text(
            json.dumps({"config": {"key": "not-a-form"}})
        )
        (crawler_dir / "manifest.json").write_text(json.dumps({"dag": {}}))
        (crawler_dir / "crawler.json").write_text(
            json.dumps({"config": {"key": "form-config"}})
        )

        original = svc_module.CONTRACT_GENERATED_DIR
        svc_module.CONTRACT_GENERATED_DIR = tmp_path
        try:
            svc = create_app_handler_service(
                _TestHandler(), app_name="metabase", app_class=_OneEpApp
            )
            client = TestClient(svc, raise_server_exceptions=False)
            response = client.get("/workflows/v1/configmap/atlan-metabase")
            assert response.status_code == 200
            parsed_config = json.loads(response.json()["data"]["data"]["config"])
            assert parsed_config["key"] == "form-config"
        finally:
            svc_module.CONTRACT_GENERATED_DIR = original

    def test_configmap_default_fallback_matches_connector_suffix_form(
        self, tmp_path: Path
    ) -> None:
        """``crawler`` resolves ``snowflake-crawler.json`` over a first-sorting sibling.

        The connector convention names the entry point for the *role* and the
        file for the source, so exact-name matching alone never fires across the
        connector fleet — every one of them would ride the alphabetical guess.
        """
        from application_sdk.app.base import App
        from application_sdk.app.entrypoint import entrypoint
        from application_sdk.handler import service as svc_module

        class _OneEpApp(App):
            @entrypoint
            async def crawler(self, input: _RoutingInput) -> _RoutingOutput:
                return _RoutingOutput()

        crawler_dir = tmp_path / "crawler"
        crawler_dir.mkdir()
        (crawler_dir / "aaa-unknown-sibling.json").write_text(
            json.dumps({"config": {"key": "not-a-form"}})
        )
        (crawler_dir / "manifest.json").write_text(json.dumps({"dag": {}}))
        (crawler_dir / "snowflake-crawler.json").write_text(
            json.dumps({"config": {"key": "form-config"}})
        )

        original = svc_module.CONTRACT_GENERATED_DIR
        svc_module.CONTRACT_GENERATED_DIR = tmp_path
        try:
            svc = create_app_handler_service(
                _TestHandler(), app_name="snowflake", app_class=_OneEpApp
            )
            client = TestClient(svc, raise_server_exceptions=False)
            response = client.get("/workflows/v1/configmap/atlan-snowflake")
            assert response.status_code == 200
            parsed_config = json.loads(response.json()["data"]["data"]["config"])
            assert parsed_config["key"] == "form-config"
        finally:
            svc_module.CONTRACT_GENERATED_DIR = original

    def test_configmap_alphabetical_last_resort_serves_and_warns(
        self, tmp_path: Path
    ) -> None:
        """Several candidates, none named for the entrypoint → serve + WARN.

        The alphabetical pick is the compatibility path for apps whose form name
        the SDK cannot recognise, so it still answers rather than 404ing. What
        it owes an operator is visibility: FND-1682 was invisible precisely
        because the wrong file came back as an HTTP 200. The warning names the
        file served and the candidates rejected, so the next unrecognised
        sibling shows up on the first request instead of in a blank wizard.
        """
        from application_sdk.app.base import App
        from application_sdk.app.entrypoint import entrypoint
        from application_sdk.handler import service as svc_module

        class _SplitApp(App):
            @entrypoint
            async def extract_metadata(self, input: _RoutingInput) -> _RoutingOutput:
                return _RoutingOutput()

        # Route/card-split shape: the form is named for the app, not the entry
        # point, so nothing here identifies itself and both files are eligible.
        (tmp_path / "aaa-unknown-sibling.json").write_text(
            json.dumps({"config": {"key": "guessed"}})
        )
        (tmp_path / "manifest.json").write_text(json.dumps({"dag": {}}))
        (tmp_path / "metabase.json").write_text(
            json.dumps({"config": {"key": "the-real-form"}})
        )

        original = svc_module.CONTRACT_GENERATED_DIR
        svc_module.CONTRACT_GENERATED_DIR = tmp_path
        try:
            svc = create_app_handler_service(
                _TestHandler(), app_name="metabase", app_class=_SplitApp
            )
            client = TestClient(svc, raise_server_exceptions=False)
            with patch("application_sdk.handler.service.logger") as mock_logger:
                response = client.get("/workflows/v1/configmap/atlan-metabase")
            assert response.status_code == 200
            # Still served — the guess is the compatibility path, not an error.
            parsed_config = json.loads(response.json()["data"]["data"]["config"])
            assert parsed_config["key"] == "guessed"

            warned = [
                call
                for call in mock_logger.warning.call_args_list
                if "chosen alphabetically" in call.args[0]
            ]
            assert len(warned) == 1
            # The rejected candidates are the diagnostic: they are what tells an
            # operator which file they meant to serve.
            assert warned[0].args[1:] == (
                "extract-metadata",
                2,
                "aaa-unknown-sibling",
                ["aaa-unknown-sibling", "metabase"],
            )
        finally:
            svc_module.CONTRACT_GENERATED_DIR = original

    def test_configmap_default_fallback_prefers_nested_over_flat_form(
        self, tmp_path: Path
    ) -> None:
        """Nested ``<ep.name>/`` form wins over a flat sibling for the same app.

        Pins the documented search order in :func:`get_configmap`: the
        default-entrypoint fallback searches the nested per-entrypoint dir
        BEFORE the flat ``CONTRACT_GENERATED_DIR``. Without both layouts present
        in one test, swapping the two ``search_dir`` candidates (flat before
        nested) would pass the whole suite, since the flat and nested regression
        tests each supply only one layout.
        """
        from application_sdk.app.base import App
        from application_sdk.app.entrypoint import entrypoint
        from application_sdk.handler import service as svc_module

        class _OneEpApp(App):
            @entrypoint
            async def crawler(self, input: _RoutingInput) -> _RoutingOutput:
                return _RoutingOutput()

        # Both layouts present for the same app-id request: a nested
        # per-entrypoint form (ep.name == "crawler") and a flat form at the
        # root. The nested one must win.
        nested_dir = tmp_path / "crawler"
        nested_dir.mkdir()
        (nested_dir / "openapi.json").write_text(
            json.dumps({"config": {"key": "nested-form"}})
        )
        (tmp_path / "manifest.json").write_text(json.dumps({"dag": {}}))
        (tmp_path / "openapi.json").write_text(
            json.dumps({"config": {"key": "flat-form"}})
        )

        original = svc_module.CONTRACT_GENERATED_DIR
        svc_module.CONTRACT_GENERATED_DIR = tmp_path
        try:
            svc = create_app_handler_service(
                _TestHandler(), app_name="openapi", app_class=_OneEpApp
            )
            client = TestClient(svc, raise_server_exceptions=False)
            response = client.get("/workflows/v1/configmap/atlan-openapi")
            assert response.status_code == 200
            parsed_config = json.loads(response.json()["data"]["data"]["config"])
            assert parsed_config["key"] == "nested-form"
        finally:
            svc_module.CONTRACT_GENERATED_DIR = original

    def test_configmap_default_fallback_returns_404_when_only_excluded_files(
        self, tmp_path: Path
    ) -> None:
        """Default-entrypoint dir with only manifest + credential → 404.

        Regression guard: when the entrypoint's generated dir exists but
        every file in it is on the exclusion list (no form configmap), the
        fallback finds no candidate and the handler returns 404 rather than
        serving the credential template or the manifest.
        """
        from application_sdk.app.base import App
        from application_sdk.app.entrypoint import entrypoint
        from application_sdk.handler import service as svc_module

        class _OneEpApp(App):
            @entrypoint
            async def crawler(self, input: _RoutingInput) -> _RoutingOutput:
                return _RoutingOutput()

        crawler_dir = tmp_path / "crawler"
        crawler_dir.mkdir()
        (crawler_dir / "manifest.json").write_text(json.dumps({"dag": {}}))
        (crawler_dir / "atlan-connectors-snowflake.json").write_text(
            json.dumps({"config": {"key": "credential"}})
        )

        original = svc_module.CONTRACT_GENERATED_DIR
        svc_module.CONTRACT_GENERATED_DIR = tmp_path
        try:
            # app_name= sets the task-queue name only; _resolve_app_entrypoint
            # keys on _OneEpApp._app_name (kebab of the class name).
            svc = create_app_handler_service(
                _TestHandler(), app_name="snowflake", app_class=_OneEpApp
            )
            client = TestClient(svc, raise_server_exceptions=False)
            response = client.get("/workflows/v1/configmap/atlan-snowflake")
            assert response.status_code == 404
        finally:
            svc_module.CONTRACT_GENERATED_DIR = original

    def test_configmap_returns_404_when_fallback_ep_dir_missing(
        self, tmp_path: Path
    ) -> None:
        """Default-entrypoint fallback → 404 when ep directory missing on disk.

        The fallback resolves the app's default entrypoint, then looks for a
        form configmap under ``CONTRACT_GENERATED_DIR/<ep.name>/``.  When that
        directory does not exist, ``entrypoint_dir.is_dir()`` is False, no
        candidate is found, and the handler returns 404.
        """
        from application_sdk.app.base import App
        from application_sdk.app.entrypoint import entrypoint
        from application_sdk.handler import service as svc_module

        class _NoEpDirApp(App):
            @entrypoint
            async def crawler(self, input: _RoutingInput) -> _RoutingOutput:
                return _RoutingOutput()

        # tmp_path exists but has no crawler/ subdirectory.
        original = svc_module.CONTRACT_GENERATED_DIR
        svc_module.CONTRACT_GENERATED_DIR = tmp_path
        try:
            # app_name= sets the task-queue name only; _resolve_app_entrypoint
            # keys on _NoEpDirApp._app_name (kebab of the class name).
            svc = create_app_handler_service(
                _TestHandler(), app_name="snowflake", app_class=_NoEpDirApp
            )
            client = TestClient(svc, raise_server_exceptions=False)
            response = client.get("/workflows/v1/configmap/atlan-snowflake")
            assert response.status_code == 404
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

    def test_configmaps_lists_the_names_the_endpoint_answers_to(
        self, tmp_path: Path
    ) -> None:
        """The listing is "what can be fetched", not "which file is the form".

        Those two rules look interchangeable and are not, so both halves are
        pinned here. A credential template must stay LISTED — the setup form's
        `credential` widget fetches `atlan-connectors-<source>` as a configmap
        in its own right, so filtering it out would advertise fewer names than
        the endpoint actually serves. The manifest must stay OUT, because
        `/workflows/v1/manifest` owns it.

        `artifact_schemas` is listed too, and deliberately: this endpoint does
        serve it by exact stem. What FND-1682 fixed was the *fallback* picking
        it when asked for something else, which is a different question from
        which names resolve.
        """
        from application_sdk.handler import service as svc_module

        for name in (
            "artifact_schemas.json",
            "atlan-connectors-snowflake.json",
            "manifest.json",
            "snowflake.json",
        ):
            (tmp_path / name).write_text("{}")

        original = svc_module.CONTRACT_GENERATED_DIR
        svc_module.CONTRACT_GENERATED_DIR = tmp_path
        try:
            response = _make_client().get("/workflows/v1/configmaps")
            assert response.status_code == 200
            configmaps = response.json()["data"]["configmaps"]
        finally:
            svc_module.CONTRACT_GENERATED_DIR = original

        assert sorted(configmaps) == [
            "artifact_schemas",
            "atlan-connectors-snowflake",
            "snowflake",
        ]

    def test_the_excluded_stem_is_the_shared_one(self) -> None:
        """One vocabulary, not a literal re-spelled at each site.

        A hand-written `"manifest"` here was the last copy of that vocabulary
        left in this module after FND-1682, and one divergent spelling is all
        the artifact_schemas bug needed. If `_generated_tree` renames the stem,
        this endpoint has to move with it rather than quietly keep listing a
        file the rest of the SDK stopped calling a manifest.
        """
        from application_sdk.common._generated_tree import MANIFEST_STEM
        from application_sdk.handler import service as svc_module

        assert svc_module.MANIFEST_STEM is MANIFEST_STEM

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


# ---------------------------------------------------------------------------
# Configmaps deduplication tests
# ---------------------------------------------------------------------------


class TestConfigMapsDeduplication:
    """Tests for list_configmaps deduplication across subdirectories."""

    def test_configmaps_dedupes_across_subdirs(self, tmp_path: Path) -> None:
        """Configmaps with same stem in different subdirs appear only once."""
        from application_sdk.handler import service as svc_module

        # Root-level configs
        (tmp_path / "config-a.json").write_text("{}")
        (tmp_path / "config-b.json").write_text("{}")

        # Sub-directory with duplicate stem + new config
        subdir = tmp_path / "snow-gen"
        subdir.mkdir()
        (subdir / "config-a.json").write_text("{}")  # duplicate of root
        (subdir / "config-c.json").write_text("{}")

        original = svc_module.CONTRACT_GENERATED_DIR
        svc_module.CONTRACT_GENERATED_DIR = tmp_path
        try:
            client = _make_client()
            response = client.get("/workflows/v1/configmaps")
            assert response.status_code == 200
            configmaps = response.json()["data"]["configmaps"]
            # config-a should appear only once despite being in root and subdir
            assert configmaps.count("config-a") == 1
            assert "config-b" in configmaps
            assert "config-c" in configmaps
            assert "manifest" not in configmaps
        finally:
            svc_module.CONTRACT_GENERATED_DIR = original

    def test_configmaps_rglob_finds_nested_configs(self, tmp_path: Path) -> None:
        """rglob finds JSON files in nested sub-directories."""
        from application_sdk.handler import service as svc_module

        # Only sub-directory configs, no root configs
        subdir = tmp_path / "entrypoint-a"
        subdir.mkdir()
        (subdir / "nested-config.json").write_text("{}")

        deep_subdir = tmp_path / "entrypoint-b" / "deep"
        deep_subdir.mkdir(parents=True)
        (deep_subdir / "deep-config.json").write_text("{}")

        original = svc_module.CONTRACT_GENERATED_DIR
        svc_module.CONTRACT_GENERATED_DIR = tmp_path
        try:
            client = _make_client()
            response = client.get("/workflows/v1/configmaps")
            assert response.status_code == 200
            configmaps = response.json()["data"]["configmaps"]
            assert "nested-config" in configmaps
            assert "deep-config" in configmaps
        finally:
            svc_module.CONTRACT_GENERATED_DIR = original

    def test_configmaps_excludes_manifest_in_subdirs(self, tmp_path: Path) -> None:
        """manifest.json in subdirectories is also excluded."""
        from application_sdk.handler import service as svc_module

        subdir = tmp_path / "ep-gen"
        subdir.mkdir()
        (subdir / "manifest.json").write_text("{}")
        (subdir / "real-config.json").write_text("{}")

        original = svc_module.CONTRACT_GENERATED_DIR
        svc_module.CONTRACT_GENERATED_DIR = tmp_path
        try:
            client = _make_client()
            response = client.get("/workflows/v1/configmaps")
            assert response.status_code == 200
            configmaps = response.json()["data"]["configmaps"]
            assert "manifest" not in configmaps
            assert "real-config" in configmaps
        finally:
            svc_module.CONTRACT_GENERATED_DIR = original


class TestConfigEndpointPersistence:
    """Tests for /workflows/v1/config/{id} round-trip with a real obstore mock."""

    def test_get_config_503_when_no_storage(self) -> None:
        client = _make_client()
        response = client.get("/workflows/v1/config/some-id")
        assert response.status_code == 503

    def test_post_config_503_when_no_storage(self) -> None:
        client = _make_client()
        response = client.post("/workflows/v1/config/some-id", json={"k": "v"})
        assert response.status_code == 503

    def test_get_config_404_when_storage_present_but_key_missing(self) -> None:
        from unittest.mock import AsyncMock, MagicMock, patch

        mock_storage = MagicMock()
        app = create_app_handler_service(
            _TestHandler(), app_name="cfg-test", storage=mock_storage
        )
        client = TestClient(app)
        # download_file raises → _config_load_from_objectstore returns None
        with patch(
            "application_sdk.storage.ops.download_file",
            new=AsyncMock(side_effect=FileNotFoundError("missing")),
        ):
            response = client.get("/workflows/v1/config/abc-123")
        assert response.status_code == 404
        assert "abc-123" in response.json()["detail"]

    @pytest.mark.parametrize(
        ("error", "status"),
        [
            (PermissionError("denied"), 503),
            (OSError("connection reset"), 503),
        ],
    )
    def test_get_config_load_failure_is_not_a_missing_config(
        self, error: Exception, status: int
    ) -> None:
        """Only a missing key is a 404; any other load failure is a 503."""
        from unittest.mock import AsyncMock, MagicMock, patch

        app = create_app_handler_service(
            _TestHandler(), app_name="cfg-test", storage=MagicMock()
        )
        client = TestClient(app, raise_server_exceptions=False)
        with patch(
            "application_sdk.storage.ops.download_file",
            new=AsyncMock(side_effect=error),
        ):
            response = client.get("/workflows/v1/config/abc-123")
        assert response.status_code == status
        assert response.json()["detail"] == "Object store unavailable"

    def test_get_config_404_on_a_storage_not_found(self) -> None:
        from unittest.mock import AsyncMock, MagicMock, patch

        from application_sdk.storage.errors import StorageNotFoundError

        app = create_app_handler_service(
            _TestHandler(), app_name="cfg-test", storage=MagicMock()
        )
        client = TestClient(app)
        with patch(
            "application_sdk.storage.ops.download_file",
            new=AsyncMock(side_effect=StorageNotFoundError("missing", key="k")),
        ):
            response = client.get("/workflows/v1/config/abc-123")
        assert response.status_code == 404

    def test_post_config_200_when_storage_present(self) -> None:
        from unittest.mock import AsyncMock, MagicMock, patch

        mock_storage = MagicMock()
        app = create_app_handler_service(
            _TestHandler(), app_name="cfg-post", storage=mock_storage
        )
        client = TestClient(app)
        with patch(
            "application_sdk.storage.ops.put_json", new=AsyncMock(return_value=None)
        ) as mock_put:
            response = client.post(
                "/workflows/v1/config/abc-123",
                params={"type": "credentials"},
                json={"foo": "bar"},
            )
        assert response.status_code == 200
        body = response.json()
        assert body["success"] is True
        assert body["data"] == {"foo": "bar"}
        mock_put.assert_awaited_once()
        # Key must conform to the v2 path convention
        called_key = mock_put.await_args.args[0]
        assert called_key.endswith("/credentials/abc-123/config.json")

    def test_get_config_200_round_trip(self) -> None:
        """Full round-trip: object store returns bytes, endpoint deserializes them."""
        from unittest.mock import AsyncMock, MagicMock, patch

        import orjson

        mock_storage = MagicMock()
        app = create_app_handler_service(
            _TestHandler(), app_name="cfg-rt", storage=mock_storage
        )
        client = TestClient(app)

        async def fake_download(key, dest, store):
            Path(dest).write_bytes(orjson.dumps({"hello": "world"}))

        with patch(
            "application_sdk.storage.ops.download_file",
            new=AsyncMock(side_effect=fake_download),
        ):
            response = client.get("/workflows/v1/config/round-trip")
        assert response.status_code == 200
        assert response.json()["data"] == {"hello": "world"}
