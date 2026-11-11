"""Handler service tests: the served manifest and entrypoint manifest resolution."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from application_sdk.common.task_queue import (
    APP_NAME_TOKEN,
    DEPLOYMENT_NAME_TOKEN,
    derive_task_queue,
)
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

    def test_manifest_disk_bakes_app_name_and_substitutes_deployment(
        self, tmp_path: Path
    ) -> None:
        """app_name is baked into the manifest by the contract toolkit (from the
        contract `name`) and served unchanged — the endpoint substitutes only the
        per-deployment token. The baked value is what logs are tagged with, so the
        Workflow Center's log filter matches it (HYP-1678)."""
        from application_sdk.handler import service as svc_module

        manifest_data = {
            "execution_mode": "dag",
            "dag": {
                "extract": {
                    "activity_name": "execute_workflow",
                    "activity_display_name": "Extract",
                    "app_name": "baked-name",
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
            # app_name passes through unchanged (baked, not substituted).
            assert body["dag"]["extract"]["app_name"] == "baked-name"
            # deployment token is still substituted.
            assert body["dag"]["extract"]["inputs"]["task_queue"] == "prod-deploy-queue"
        finally:
            svc_module.CONTRACT_GENERATED_DIR = original_dir
            svc_module.DEPLOYMENT_NAME = original_dep

    def test_manifest_queue_matches_the_configured_worker_queue(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """FND-195: the served queue is stamped from the queue this process was
        configured with, not re-derived from env and hoped to match.

        The baked name here (``dbt``) disagrees with the deployment's app name
        (``dbt-v3``) — the shape that used to serve a plausible ``atlan-dbt-prod``
        that no worker polled, hanging the run to its 24h backstop (CONNECT-183).
        """
        from application_sdk.handler import service as svc_module

        manifest_data = {
            "execution_mode": "dag",
            "dag": {
                "extract": {
                    "activity_name": "execute_workflow",
                    "app_name": "dbt",
                    "inputs": {"task_queue": "atlan-dbt-{deployment_name}"},
                }
            },
        }
        (tmp_path / "manifest.json").write_text(json.dumps(manifest_data))
        monkeypatch.setenv("ATLAN_APPLICATION_NAME", "dbt-v3")
        monkeypatch.setenv("ATLAN_DEPLOYMENT_NAME", "prod")

        original_dir = svc_module.CONTRACT_GENERATED_DIR
        svc_module.CONTRACT_GENERATED_DIR = tmp_path
        try:
            app = create_app_handler_service(
                _TestHandler(), app_name="dbt", task_queue="atlan-dbt-v3-prod"
            )
            response = TestClient(app).get("/workflows/v1/manifest")
            assert response.status_code == 200
            served = response.json()["dag"]["extract"]["inputs"]["task_queue"]
            assert served == "atlan-dbt-v3-prod"
        finally:
            svc_module.CONTRACT_GENERATED_DIR = original_dir

    def test_manifest_unresolvable_app_name_keeps_the_token_visible(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """With no app name anywhere the route must not manufacture one: a literal
        ``{app_name}`` is greppable, whereas ``atlan-default-prod`` reads as a real
        queue and reproduces the original silent hang."""
        from application_sdk.handler import service as svc_module

        manifest_data = {
            "execution_mode": "dag",
            "dag": {
                "extract": {
                    "activity_name": "execute_workflow",
                    "app_name": "{app_name}",
                    "inputs": {"task_queue": "atlan-{app_name}-{deployment_name}"},
                }
            },
        }
        (tmp_path / "manifest.json").write_text(json.dumps(manifest_data))
        monkeypatch.delenv("ATLAN_APPLICATION_NAME", raising=False)
        monkeypatch.setenv("ATLAN_DEPLOYMENT_NAME", "prod")

        original_dir = svc_module.CONTRACT_GENERATED_DIR
        svc_module.CONTRACT_GENERATED_DIR = tmp_path
        try:
            # app_name="" so nothing is registered to fall back to either.
            app = create_app_handler_service(_TestHandler(), app_name="")
            response = TestClient(app).get("/workflows/v1/manifest")
            assert response.status_code == 200
            served = response.json()["dag"]["extract"]["inputs"]["task_queue"]
            assert "{app_name}" in served
            assert "default" not in served
        finally:
            svc_module.CONTRACT_GENERATED_DIR = original_dir

    def test_manifest_programmatic_queue_matches_the_configured_worker_queue(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A programmatic ``AppManifest`` gets the same reconciliation as the disk
        branches — it is the shape with no toolkit bake behind it at all, so it is
        the *most* likely to carry an unresolved template, not the least.

        Asserted as equality with the configured worker queue rather than a
        hard-coded string, so the programmatic path cannot drift from the disk
        paths the way it did before FND-195.
        """
        from application_sdk.handler.manifest import (
            AppManifest,
            DagNode,
            ExecuteWorkflowInputs,
        )

        worker_queue = "atlan-dbt-v3-prod"
        manifest = AppManifest(
            execution_mode="dag",
            dag={
                "extract": DagNode(
                    activity_name="execute_workflow",
                    activity_display_name="Extract",
                    app_name="{app_name}",
                    inputs=ExecuteWorkflowInputs(
                        workflow_type="extraction",
                        # Un-baked template: the toolkit never ran on this one.
                        task_queue="atlan-{app_name}-{deployment_name}",
                    ),
                ),
            },
        )
        monkeypatch.setenv("ATLAN_APPLICATION_NAME", "dbt-v3")
        monkeypatch.setenv("ATLAN_DEPLOYMENT_NAME", "prod")

        app = create_app_handler_service(
            _TestHandler(),
            app_name="dbt-v3",
            task_queue=worker_queue,
            manifest=manifest,
        )
        response = TestClient(app).get("/workflows/v1/manifest")
        assert response.status_code == 200
        node = response.json()["dag"]["extract"]
        assert node["inputs"]["task_queue"] == worker_queue
        # Residual {app_name} is per-node log identity (HYP-1954), filled too.
        assert node["app_name"] == "dbt-v3"

    def test_manifest_programmatic_unresolvable_app_name_keeps_the_token_visible(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """With no app name available the programmatic path must not manufacture
        one either — same contract as the disk branches."""
        from application_sdk.handler.manifest import (
            AppManifest,
            DagNode,
            ExecuteWorkflowInputs,
        )

        manifest = AppManifest(
            execution_mode="dag",
            dag={
                "extract": DagNode(
                    activity_name="execute_workflow",
                    activity_display_name="Extract",
                    app_name="{app_name}",
                    inputs=ExecuteWorkflowInputs(
                        workflow_type="extraction",
                        task_queue="atlan-{app_name}-{deployment_name}",
                    ),
                ),
            },
        )
        monkeypatch.delenv("ATLAN_APPLICATION_NAME", raising=False)
        monkeypatch.setenv("ATLAN_DEPLOYMENT_NAME", "prod")

        app = create_app_handler_service(_TestHandler(), app_name="", manifest=manifest)
        response = TestClient(app).get("/workflows/v1/manifest")
        assert response.status_code == 200
        served = response.json()["dag"]["extract"]["inputs"]["task_queue"]
        assert "{app_name}" in served
        assert "default" not in served

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

    def test_manifest_default_entrypoint_fallback_when_no_root_manifest(
        self, tmp_path: Path
    ) -> None:
        """No `?entrypoint=` + no root manifest → resolve default entrypoint.

        Multi-entrypoint apps (postgres crawler+miner, snowflake crawler+miner)
        don't have a root manifest.json — each entrypoint has its own under
        ``<ep.name>/manifest.json``. When Heracles/AE calls
        ``/workflows/v1/manifest`` without ``?entrypoint=`` for pre-flight app
        validation, the SDK falls back to the default entrypoint's manifest
        instead of 404-ing (same semantics as ``/start`` and ``/input-contract``).
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
        (crawler_dir / "manifest.json").write_text(
            json.dumps({"execution_mode": "automation-engine", "ep": "crawler"})
        )
        miner_dir = tmp_path / "miner"
        miner_dir.mkdir()
        (miner_dir / "manifest.json").write_text(
            json.dumps({"execution_mode": "automation-engine", "ep": "miner"})
        )

        original = svc_module.CONTRACT_GENERATED_DIR
        svc_module.CONTRACT_GENERATED_DIR = tmp_path
        try:
            # app_name= sets the task-queue name only; _resolve_app_entrypoint
            # keys on _MultiEpApp._app_name (kebab of the class name).
            svc = create_app_handler_service(
                _TestHandler(), app_name="postgres", app_class=_MultiEpApp
            )
            client = TestClient(svc, raise_server_exceptions=False)
            response = client.get("/workflows/v1/manifest")
            assert response.status_code == 200
            assert response.json()["ep"] == "crawler"
        finally:
            svc_module.CONTRACT_GENERATED_DIR = original

    def test_manifest_entrypoint_param_falls_back_to_flat_single_entrypoint(
        self, tmp_path: Path
    ) -> None:
        """``?entrypoint=<name>`` on a FLAT single-entrypoint app serves the root manifest.

        Single-entrypoint apps emit a flat manifest at
        ``CONTRACT_GENERATED_DIR/manifest.json`` (no ``<ep>/`` subdir). Heracles/AE
        always sends ``?entrypoint=<name>`` on package-workflow submit, which routes
        straight to ``_serve_entrypoint_manifest`` — a nested-only lookup. Without
        the flat fallback the app 404s ("No manifest found for entrypoint 'openapi'")
        and the platform surfaces a 500, blocking every run (observed on the openapi
        connector's first submit). Regression guard for that path.
        """
        from application_sdk.app.base import App
        from application_sdk.app.entrypoint import entrypoint
        from application_sdk.handler import service as svc_module

        class _OneEpApp(App):
            @entrypoint
            async def crawler(self, input: _RoutingInput) -> _RoutingOutput:
                return _RoutingOutput()

        # Flat layout: manifest at the ROOT, no per-entrypoint subdir.
        (tmp_path / "manifest.json").write_text(
            json.dumps({"execution_mode": "linear", "source": "flat-root"})
        )

        original = svc_module.CONTRACT_GENERATED_DIR
        svc_module.CONTRACT_GENERATED_DIR = tmp_path
        try:
            svc = create_app_handler_service(
                _TestHandler(), app_name="openapi", app_class=_OneEpApp
            )
            client = TestClient(svc, raise_server_exceptions=False)
            response = client.get("/workflows/v1/manifest?entrypoint=openapi")
            assert response.status_code == 200
            assert response.json()["source"] == "flat-root"
        finally:
            svc_module.CONTRACT_GENERATED_DIR = original

    def test_manifest_root_file_wins_over_default_entrypoint_fallback(
        self, tmp_path: Path
    ) -> None:
        """Root manifest.json preempts the default-entrypoint fallback."""
        from application_sdk.app.base import App
        from application_sdk.app.entrypoint import entrypoint
        from application_sdk.handler import service as svc_module

        class _OneEpApp(App):
            @entrypoint
            async def crawler(self, input: _RoutingInput) -> _RoutingOutput:
                return _RoutingOutput()

        (tmp_path / "manifest.json").write_text(
            json.dumps({"execution_mode": "linear", "source": "root"})
        )
        crawler_dir = tmp_path / "crawler"
        crawler_dir.mkdir()
        (crawler_dir / "manifest.json").write_text(
            json.dumps({"execution_mode": "automation-engine", "source": "ep"})
        )

        original = svc_module.CONTRACT_GENERATED_DIR
        svc_module.CONTRACT_GENERATED_DIR = tmp_path
        try:
            # app_name= sets the task-queue name only; _resolve_app_entrypoint
            # keys on _OneEpApp._app_name (kebab of the class name).
            svc = create_app_handler_service(
                _TestHandler(), app_name="postgres", app_class=_OneEpApp
            )
            client = TestClient(svc, raise_server_exceptions=False)
            response = client.get("/workflows/v1/manifest")
            assert response.status_code == 200
            assert response.json()["source"] == "root"
        finally:
            svc_module.CONTRACT_GENERATED_DIR = original

    def test_manifest_404_when_default_entrypoint_dir_has_no_manifest(
        self, tmp_path: Path
    ) -> None:
        """Default-entrypoint resolves but its dir has no manifest.json → 404.

        Regression guard: the fallback must not silently swallow a missing
        entrypoint manifest. Returning 404 keeps the failure visible.
        """
        from application_sdk.app.base import App
        from application_sdk.app.entrypoint import entrypoint
        from application_sdk.handler import service as svc_module

        class _OneEpApp(App):
            @entrypoint
            async def crawler(self, input: _RoutingInput) -> _RoutingOutput:
                return _RoutingOutput()

        # Entrypoint dir is empty (e.g. contract generation skipped manifest).
        (tmp_path / "crawler").mkdir()

        original = svc_module.CONTRACT_GENERATED_DIR
        svc_module.CONTRACT_GENERATED_DIR = tmp_path
        try:
            # app_name= sets the task-queue name only; _resolve_app_entrypoint
            # keys on _OneEpApp._app_name (kebab of the class name).
            svc = create_app_handler_service(
                _TestHandler(), app_name="postgres", app_class=_OneEpApp
            )
            client = TestClient(svc, raise_server_exceptions=False)
            response = client.get("/workflows/v1/manifest")
            assert response.status_code == 404
            assert response.json()["detail"] == "No manifest available"
        finally:
            svc_module.CONTRACT_GENERATED_DIR = original

    def test_manifest_returns_404_when_fallback_ep_dir_missing(
        self, tmp_path: Path
    ) -> None:
        """Default-entrypoint fallback → 404 when ep manifest missing on disk.

        Multi-entrypoint apps place each manifest under ``<ep.name>/manifest.json``.
        When the default ep's directory does not exist, ``_serve_entrypoint_manifest``
        raises HTTPException(404), which is caught, and the fallback returns
        ``"No manifest available"``.
        """
        from application_sdk.app.base import App
        from application_sdk.app.entrypoint import entrypoint
        from application_sdk.handler import service as svc_module

        class _NoEpDirManifestApp(App):
            @entrypoint(default=True)
            async def crawler(self, input: _RoutingInput) -> _RoutingOutput:
                return _RoutingOutput()

            @entrypoint
            async def miner(self, input: _AlphaInput) -> _AlphaOutput:
                return _AlphaOutput()

        # tmp_path exists but has no crawler/ or miner/ subdirectory and no
        # root manifest.json — forces the fallback to reach the missing-ep-dir path.
        original = svc_module.CONTRACT_GENERATED_DIR
        svc_module.CONTRACT_GENERATED_DIR = tmp_path
        try:
            # app_name= sets the task-queue name only; _resolve_app_entrypoint
            # keys on _NoEpDirManifestApp._app_name (kebab of the class name).
            svc = create_app_handler_service(
                _TestHandler(), app_name="postgres", app_class=_NoEpDirManifestApp
            )
            client = TestClient(svc, raise_server_exceptions=False)
            response = client.get("/workflows/v1/manifest")
            assert response.status_code == 404
            assert response.json()["detail"] == "No manifest available"
        finally:
            svc_module.CONTRACT_GENERATED_DIR = original

    def test_manifest_falls_back_to_explicit_ep_when_implicit_run_is_default(
        self, tmp_path: Path
    ) -> None:
        """Mixed run()+@entrypoint app: implicit run() default has no manifest.

        A SQL-template connector (e.g. ``PostgresApp(SqlMetadataExtractor)``)
        both implements ``run()`` AND declares explicit ``@entrypoint``s
        (crawler, miner). The framework forces the implicit ``run()`` as the
        default entrypoint and prohibits ``@entrypoint(default=True)`` elsewhere
        (see ``_ep_registration._build_entry_points``) — but ``run()`` has no
        generated manifest dir, so the no-``?entrypoint=`` route AE/Heracles use
        for pre-flight validation would 404. The handler must instead fall back
        to the explicit entrypoints alphabetically and serve ``crawler``
        (c < m), the connector's primary path, rather than 404-ing.
        """
        from application_sdk.app.base import App
        from application_sdk.app.entrypoint import entrypoint
        from application_sdk.handler import service as svc_module

        class _MixedRunApp(App):
            async def run(self, input: _RoutingInput) -> _RoutingOutput:
                return _RoutingOutput()

            @entrypoint
            async def crawler(self, input: _RoutingInput) -> _RoutingOutput:
                return _RoutingOutput()

            @entrypoint
            async def miner(self, input: _AlphaInput) -> _AlphaOutput:
                return _AlphaOutput()

        # Only the explicit entrypoints have generated manifests; the implicit
        # run() has no dir — exactly the postgres/bundle on-disk layout.
        for ep_name in ("crawler", "miner"):
            ep_dir = tmp_path / ep_name
            ep_dir.mkdir()
            (ep_dir / "manifest.json").write_text(
                json.dumps({"execution_mode": "automation-engine", "ep": ep_name})
            )

        original = svc_module.CONTRACT_GENERATED_DIR
        svc_module.CONTRACT_GENERATED_DIR = tmp_path
        try:
            # app_name= sets the task-queue name only; _resolve_app_entrypoint
            # keys on _MixedRunApp._app_name (kebab of the class name).
            svc = create_app_handler_service(
                _TestHandler(), app_name="postgres", app_class=_MixedRunApp
            )
            client = TestClient(svc, raise_server_exceptions=False)
            response = client.get("/workflows/v1/manifest")
            assert response.status_code == 200
            # Default (implicit run) has no manifest → alphabetically-first
            # explicit entrypoint, i.e. crawler, NOT miner.
            assert response.json()["ep"] == "crawler"
        finally:
            svc_module.CONTRACT_GENERATED_DIR = original

    def test_manifest_404_when_mixed_run_app_has_no_servable_manifest(
        self, tmp_path: Path
    ) -> None:
        """Mixed run()+@entrypoint app with no manifest dirs at all → 404.

        Negative mirror of
        ``test_manifest_falls_back_to_explicit_ep_when_implicit_run_is_default``
        (and of the sibling ``test_manifest_returns_404_when_fallback_ep_dir_missing``)
        for the mixed ``run()`` shape: the implicit ``run()`` default has no
        manifest dir AND neither explicit entrypoint dir exists, so every
        candidate misses on disk and the route terminates in the canonical
        ``"No manifest available"`` 404 rather than serving stale data or 500-ing.
        """
        from application_sdk.app.base import App
        from application_sdk.app.entrypoint import entrypoint
        from application_sdk.handler import service as svc_module

        class _MixedRunNoManifestApp(App):
            async def run(self, input: _RoutingInput) -> _RoutingOutput:
                return _RoutingOutput()

            @entrypoint
            async def crawler(self, input: _RoutingInput) -> _RoutingOutput:
                return _RoutingOutput()

            @entrypoint
            async def miner(self, input: _AlphaInput) -> _AlphaOutput:
                return _AlphaOutput()

        # tmp_path has no crawler/ or miner/ subdir and no root manifest.json —
        # every candidate (implicit run default + both explicit eps) misses.
        original = svc_module.CONTRACT_GENERATED_DIR
        svc_module.CONTRACT_GENERATED_DIR = tmp_path
        try:
            svc = create_app_handler_service(
                _TestHandler(), app_name="postgres", app_class=_MixedRunNoManifestApp
            )
            client = TestClient(svc, raise_server_exceptions=False)
            response = client.get("/workflows/v1/manifest")
            assert response.status_code == 404
            assert response.json()["detail"] == "No manifest available"
        finally:
            svc_module.CONTRACT_GENERATED_DIR = original

    def test_manifest_recovery_block_serves_explicit_ep_when_no_default_marked(
        self, tmp_path: Path
    ) -> None:
        """No-default multi-ep app: the ``except`` recovery branch still serves.

        The builder auto-marks a default for every multi-``@entrypoint`` app
        (``_ep_registration._build_entry_points``), so ``_resolve_app_entrypoint``
        never raises the 400 "entrypoint required" for a builder-produced app —
        the handler's ``except HTTPException`` recovery block (re-fetch
        ``app_meta`` from the registry, then fall back to the explicit
        entrypoints) is defensive future-proofing against that invariant
        changing. This test locks that branch: it clears the auto-marked default
        on the registry entry so default resolution raises 400, then asserts the
        recovery path re-fetches the app and serves the alphabetically-first
        explicit entrypoint (``crawler``) rather than 404-ing.
        """
        from dataclasses import replace

        from application_sdk.app.base import App
        from application_sdk.app.entrypoint import entrypoint
        from application_sdk.app.registry import AppRegistry
        from application_sdk.handler import service as svc_module

        class _NoDefaultMultiEpApp(App):
            @entrypoint
            async def crawler(self, input: _RoutingInput) -> _RoutingOutput:
                return _RoutingOutput()

            @entrypoint
            async def miner(self, input: _AlphaInput) -> _AlphaOutput:
                return _AlphaOutput()

        # Re-register the app with every default flag cleared, so
        # _resolve_default_entrypoint returns None (multi-ep, no marked default)
        # and _resolve_app_entrypoint raises 400 — the only way to drive the
        # handler's except-block recovery for a registered app.
        registry = AppRegistry.get_instance()
        meta = registry.get(_NoDefaultMultiEpApp._app_name)
        no_default_eps = {
            name: replace(ep, default=False) for name, ep in meta.entry_points.items()
        }
        registry.register(
            name=meta.name,
            version=meta.version,
            app_cls=meta.app_cls,
            input_type=meta.input_type,
            output_type=meta.output_type,
            entry_points=no_default_eps,
            allow_override=True,
        )

        # Both explicit entrypoints have manifests on disk; the recovery block
        # must pick crawler (c < m) after default resolution fails.
        for ep_name in ("crawler", "miner"):
            ep_dir = tmp_path / ep_name
            ep_dir.mkdir()
            (ep_dir / "manifest.json").write_text(
                json.dumps({"execution_mode": "automation-engine", "ep": ep_name})
            )

        original = svc_module.CONTRACT_GENERATED_DIR
        svc_module.CONTRACT_GENERATED_DIR = tmp_path
        try:
            svc = create_app_handler_service(
                _TestHandler(), app_name="postgres", app_class=_NoDefaultMultiEpApp
            )
            client = TestClient(svc, raise_server_exceptions=False)
            response = client.get("/workflows/v1/manifest")
            assert response.status_code == 200
            # No default resolvable → recovery block re-fetches app_meta and
            # falls back to the alphabetically-first explicit ep, crawler.
            assert response.json()["ep"] == "crawler"
        finally:
            svc_module.CONTRACT_GENERATED_DIR = original

    def test_simple_app_manifest_lives_at_root_not_in_subdir(
        self, tmp_path: Path
    ) -> None:
        """Simple apps (only run(), no @entrypoint) place manifest.json at
        CONTRACT_GENERATED_DIR/manifest.json — no subdirectory.

        Heracles does not pass ?entrypoint= for these apps.  The endpoint must
        serve the root file when no param is present, and must NOT search inside
        any named subdirectory.
        """
        from application_sdk.handler import service as svc_module

        contract_dir = tmp_path / "generated"
        contract_dir.mkdir(parents=True)
        # Root manifest — correct location for a simple app.
        (contract_dir / "manifest.json").write_text(
            json.dumps({"execution_mode": "linear", "app_name": "simple-app"})
        )
        # A subdir manifest should NOT be found when no ?entrypoint= is provided.
        subdir = contract_dir / "run"
        subdir.mkdir()
        (subdir / "manifest.json").write_text(json.dumps({"app_name": "wrong"}))

        original = svc_module.CONTRACT_GENERATED_DIR
        svc_module.CONTRACT_GENERATED_DIR = contract_dir
        try:
            client = _make_client()
            # No ?entrypoint= → root manifest, not the subdir one.
            response = client.get("/workflows/v1/manifest")
            assert response.status_code == 200
            body = response.json()
            assert body["app_name"] == "simple-app"
            assert body["execution_mode"] == "linear"
        finally:
            svc_module.CONTRACT_GENERATED_DIR = original

    def test_simple_app_manifest_convention_vs_multi_entrypoint(
        self, tmp_path: Path
    ) -> None:
        """Documents the path convention side-by-side:

        - Simple app (no @entrypoint):  CONTRACT_GENERATED_DIR/manifest.json
          Served via GET /manifest (no ?entrypoint= param).
        - Multi-entrypoint app:         CONTRACT_GENERATED_DIR/{name}/manifest.json
          Served via GET /manifest?entrypoint={name}.
        """
        from application_sdk.handler import service as svc_module

        contract_dir = tmp_path / "generated"
        contract_dir.mkdir(parents=True)

        # Simple-app root manifest.
        (contract_dir / "manifest.json").write_text(json.dumps({"app_name": "simple"}))
        # Multi-entrypoint subdir manifests.
        for ep_name in ("crawler", "miner"):
            ep_dir = contract_dir / ep_name
            ep_dir.mkdir()
            (ep_dir / "manifest.json").write_text(json.dumps({"app_name": ep_name}))

        original = svc_module.CONTRACT_GENERATED_DIR
        svc_module.CONTRACT_GENERATED_DIR = contract_dir
        try:
            client = _make_client()

            # Simple app path: no param → root.
            resp = client.get("/workflows/v1/manifest")
            assert resp.status_code == 200
            assert resp.json()["app_name"] == "simple"

            # Multi-entrypoint paths: ?entrypoint= → subdir.
            for ep_name in ("crawler", "miner"):
                resp = client.get(f"/workflows/v1/manifest?entrypoint={ep_name}")
                assert resp.status_code == 200
                assert resp.json()["app_name"] == ep_name
        finally:
            svc_module.CONTRACT_GENERATED_DIR = original


class TestServedManifestTaskQueueContract:
    """The served manifest's resolved ``task_queue`` is a cross-repo contract.

    :func:`~application_sdk.common.task_queue.resolve_manifest_tokens` *stamps*
    the queue this process was configured with into the manifest the route
    serves, rather than letting the consumer re-derive it (FND-195). The
    Automation Engine — and, from FND-224, the runtime scenario suite's contract
    tier — read that resolved value straight out of the response and submit work
    to it.

    That makes a value this repo writes an **input to another repo's test
    suite**, and the unit tests over ``task_queue.py`` do not pin it: they
    exercise the resolver directly, so a refactor that renamed the key, moved
    where the stamp is applied, or reverted the route to byte substitution would
    keep them green while reddening a suite that cannot vote here.

    These tests therefore assert through the HTTP boundary the consumer actually
    reads, at the documented key, for every shape ``_rewrite_task_queue_fields``
    walks. See ``docs/standards/cross-repo-contracts.md`` (FND-403).
    """

    #: ``(id, manifest, path to the queue)`` for the three shapes a real DAG
    #: manifest carries the dispatch queue at. Kept as data so a fourth shape is
    #: one tuple rather than a fourth near-duplicate test.
    _SHAPES: tuple[tuple[str, dict[str, object], tuple[str, ...]], ...] = (
        (
            "dag-node",
            {
                "execution_mode": "dag",
                "dag": {
                    "extract": {
                        "activity_name": "execute_workflow",
                        "app_name": "{app_name}",
                        "task_queue": "atlan-{app_name}-{deployment_name}",
                    }
                },
            },
            ("dag", "extract", "task_queue"),
        ),
        (
            "dag-node-inputs",
            {
                "execution_mode": "dag",
                "dag": {
                    "extract": {
                        "activity_name": "execute_workflow",
                        "app_name": "{app_name}",
                        "inputs": {
                            "workflow_type": "extraction",
                            "task_queue": "atlan-{app_name}-{deployment_name}",
                        },
                    }
                },
            },
            ("dag", "extract", "inputs", "task_queue"),
        ),
        (
            "top-level",
            {
                "execution_mode": "dag",
                "app_name": "{app_name}",
                "task_queue": "atlan-{app_name}-{deployment_name}",
            },
            ("task_queue",),
        ),
    )

    @staticmethod
    def _dig(body: object, path: tuple[str, ...]) -> object:
        """Read ``path`` out of a parsed manifest, failing on a missing key.

        Deliberately not ``.get()`` chaining: a renamed or relocated key is the
        defect these tests exist to catch, and it must surface at the exact
        segment that moved rather than as ``None != "atlan-dbt-prod"``.
        """
        node = body
        for i, segment in enumerate(path):
            assert isinstance(node, dict), (
                f"{'.'.join(path[:i]) or '<root>'} is {type(node).__name__}, "
                "not an object — the served manifest's shape changed"
            )
            assert segment in node, (
                f"served manifest has no {'.'.join(path[: i + 1])}; keys at "
                f"{'.'.join(path[:i]) or '<root>'}: {sorted(node)}"
            )
            node = node[segment]
        return node

    @staticmethod
    def _serve(
        manifest: dict[str, object],
        contract_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
        *,
        task_queue: str = "",
    ) -> dict[str, object]:
        """Write ``manifest`` to disk and fetch it back through the route."""
        from application_sdk.handler import service as svc_module

        (contract_dir / "manifest.json").write_text(json.dumps(manifest))
        monkeypatch.setattr(svc_module, "CONTRACT_GENERATED_DIR", contract_dir)
        app = create_app_handler_service(
            _TestHandler(), app_name="test-app", task_queue=task_queue
        )
        response = TestClient(app).get("/workflows/v1/manifest")
        assert response.status_code == 200
        return response.json()

    @pytest.mark.parametrize(
        ("manifest", "path"),
        [pytest.param(m, p, id=i) for i, m, p in _SHAPES],
    )
    def test_served_queue_is_resolved_at_the_documented_key(
        self,
        manifest: dict[str, object],
        path: tuple[str, ...],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Every shape is served with the queue the worker polls, at its key.

        The expectation is :func:`derive_task_queue`'s answer rather than a
        literal ``"atlan-dbt-prod"``: the contract is *agreement with the
        worker*, so hard-coding the string would let a change to the naming rule
        satisfy this test while pointing AE at a queue nobody polls.
        """
        monkeypatch.setenv("ATLAN_APPLICATION_NAME", "dbt")
        monkeypatch.setenv("ATLAN_DEPLOYMENT_NAME", "prod")

        body = self._serve(manifest, tmp_path, monkeypatch)

        assert self._dig(body, path) == derive_task_queue("dbt", "prod")

    @pytest.mark.parametrize(
        ("manifest", "path"),
        [pytest.param(m, p, id=i) for i, m, p in _SHAPES],
    )
    def test_no_unresolved_token_survives_in_the_served_bytes(
        self,
        manifest: dict[str, object],
        path: tuple[str, ...],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A literal token reaching the consumer is the CONNECT-183 failure.

        AE submits whatever it reads, so ``atlan-{app_name}-prod`` is a queue
        name with no worker behind it. Asserting over the whole response rather
        than just the queue field also covers the residual ``{app_name}`` fill
        that carries log identity (HYP-1954).
        """
        monkeypatch.setenv("ATLAN_APPLICATION_NAME", "dbt")
        monkeypatch.setenv("ATLAN_DEPLOYMENT_NAME", "prod")

        served = json.dumps(self._serve(manifest, tmp_path, monkeypatch))

        assert APP_NAME_TOKEN not in served
        assert DEPLOYMENT_NAME_TOKEN not in served

    @pytest.mark.parametrize(
        ("manifest", "path"),
        [pytest.param(m, p, id=i) for i, m, p in _SHAPES],
    )
    def test_served_queue_is_stamped_from_the_handler_not_re_derived(
        self,
        manifest: dict[str, object],
        path: tuple[str, ...],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """An explicit queue reaches the consumer, beating the env derivation.

        This is the property the stamp exists for. ``ATLAN_TASK_QUEUE`` /
        ``--task-queue`` is an override no re-derivation can reproduce, so a
        route that re-derived from the env would serve ``atlan-dbt-prod`` while
        the worker polled ``custom-override-queue`` — the two paths disagreeing
        silently, which is exactly FND-195.
        """
        monkeypatch.setenv("ATLAN_APPLICATION_NAME", "dbt")
        monkeypatch.setenv("ATLAN_DEPLOYMENT_NAME", "prod")

        body = self._serve(
            manifest, tmp_path, monkeypatch, task_queue="custom-override-queue"
        )

        assert self._dig(body, path) == "custom-override-queue"
        assert derive_task_queue("dbt", "prod") not in json.dumps(body)


# ---------------------------------------------------------------------------
# Entrypoint manifest resolution tests (Option B: name == folder name)
# ---------------------------------------------------------------------------


class TestEntrypointManifestResolution:
    """Tests for GET /workflows/v1/manifest?entrypoint={name}.

    Option B: entrypoint name IS the folder name — no atlan.yaml parsing.
    GET /manifest?entrypoint=X serves CONTRACT_GENERATED_DIR/X/manifest.json.
    """

    def test_manifest_with_entrypoint_returns_correct_manifest(
        self, tmp_path: Path
    ) -> None:
        """GET /manifest?entrypoint=snowflake resolves to snowflake/manifest.json."""
        from application_sdk.handler import service as svc_module

        contract_dir = tmp_path / "generated"
        ep_dir = contract_dir / "snowflake"
        ep_dir.mkdir(parents=True)
        manifest_data = {"execution_mode": "dag", "app_name": "snowflake-extractor"}
        (ep_dir / "manifest.json").write_text(json.dumps(manifest_data))

        original_dir = svc_module.CONTRACT_GENERATED_DIR
        svc_module.CONTRACT_GENERATED_DIR = contract_dir
        try:
            client = _make_client()
            response = client.get("/workflows/v1/manifest?entrypoint=snowflake")
            assert response.status_code == 200
            body = response.json()
            assert body["execution_mode"] == "dag"
            assert body["app_name"] == "snowflake-extractor"
        finally:
            svc_module.CONTRACT_GENERATED_DIR = original_dir

    def test_manifest_substitutes_deployment_name_for_entrypoint(
        self, tmp_path: Path
    ) -> None:
        """Entrypoint manifest replaces {deployment_name} placeholder."""
        from application_sdk.handler import service as svc_module

        contract_dir = tmp_path / "generated"
        ep_dir = contract_dir / "ep1"
        ep_dir.mkdir(parents=True)
        manifest_data = {"task_queue": "{deployment_name}-queue"}
        (ep_dir / "manifest.json").write_text(json.dumps(manifest_data))

        original_dir = svc_module.CONTRACT_GENERATED_DIR
        original_dep = svc_module.DEPLOYMENT_NAME
        svc_module.CONTRACT_GENERATED_DIR = contract_dir
        svc_module.DEPLOYMENT_NAME = "prod-deploy"
        try:
            client = _make_client()
            response = client.get("/workflows/v1/manifest?entrypoint=ep1")
            assert response.status_code == 200
            body = response.json()
            assert body["task_queue"] == "prod-deploy-queue"
        finally:
            svc_module.CONTRACT_GENERATED_DIR = original_dir
            svc_module.DEPLOYMENT_NAME = original_dep

    def test_valid_but_unknown_entrypoint_returns_404(self, tmp_path: Path) -> None:
        """Well-formed name with no matching subdir on disk returns 404."""
        from application_sdk.handler import service as svc_module

        contract_dir = tmp_path / "generated"
        contract_dir.mkdir(parents=True)

        original_dir = svc_module.CONTRACT_GENERATED_DIR
        svc_module.CONTRACT_GENERATED_DIR = contract_dir
        try:
            client = _make_client()
            response = client.get("/workflows/v1/manifest?entrypoint=unknown")
            assert response.status_code == 404
            assert "No manifest found" in response.json()["detail"]
        finally:
            svc_module.CONTRACT_GENERATED_DIR = original_dir

    def test_entrypoint_with_missing_manifest_file_returns_404(
        self, tmp_path: Path
    ) -> None:
        """Subdir exists but manifest.json is absent — returns 404."""
        from application_sdk.handler import service as svc_module

        contract_dir = tmp_path / "generated"
        (contract_dir / "snowflake").mkdir(parents=True)  # dir, but no manifest.json

        original_dir = svc_module.CONTRACT_GENERATED_DIR
        svc_module.CONTRACT_GENERATED_DIR = contract_dir
        try:
            client = _make_client()
            response = client.get("/workflows/v1/manifest?entrypoint=snowflake")
            assert response.status_code == 404
        finally:
            svc_module.CONTRACT_GENERATED_DIR = original_dir

    def test_invalid_entrypoint_name_returns_400(self, tmp_path: Path) -> None:
        """Entrypoint names with path-traversal or illegal chars return 400."""
        from application_sdk.handler import service as svc_module

        contract_dir = tmp_path / "generated"
        contract_dir.mkdir(parents=True)

        original_dir = svc_module.CONTRACT_GENERATED_DIR
        svc_module.CONTRACT_GENERATED_DIR = contract_dir
        try:
            client = _make_client()
            for bad_name in ["../etc", "a/b", "name with spaces", "name@bad"]:
                resp = client.get(f"/workflows/v1/manifest?entrypoint={bad_name}")
                assert resp.status_code == 400, f"Expected 400 for {bad_name!r}"
                assert resp.json()["detail"] == "Invalid entrypoint name"
        finally:
            svc_module.CONTRACT_GENERATED_DIR = original_dir

    def test_valid_name_not_in_registry_returns_404(self, tmp_path: Path) -> None:
        """A well-formed name with no matching manifest on disk returns 404.

        The glob-based registry only contains entrypoints whose manifest.json
        exists; a valid-format name with no file returns 404, not 400. Path escape
        is structurally impossible because glob() only yields paths under
        CONTRACT_GENERATED_DIR — no is_relative_to guard is needed.
        """
        from application_sdk.handler import service as svc_module

        contract_dir = tmp_path / "generated"
        contract_dir.mkdir(parents=True)

        original_dir = svc_module.CONTRACT_GENERATED_DIR
        svc_module.CONTRACT_GENERATED_DIR = contract_dir
        try:
            client = _make_client()
            resp = client.get("/workflows/v1/manifest?entrypoint=valid")
            assert resp.status_code == 404
            assert "valid" in resp.json()["detail"]
        finally:
            svc_module.CONTRACT_GENERATED_DIR = original_dir

    def test_legacy_manifest_alias_forwards_entrypoint(self, tmp_path: Path) -> None:
        """GET /manifest?entrypoint=name uses the legacy alias correctly."""
        from application_sdk.handler import service as svc_module

        contract_dir = tmp_path / "generated"
        ep_dir = contract_dir / "snow"
        ep_dir.mkdir(parents=True)
        (ep_dir / "manifest.json").write_text(json.dumps({"app_name": "snow-app"}))

        original_dir = svc_module.CONTRACT_GENERATED_DIR
        svc_module.CONTRACT_GENERATED_DIR = contract_dir
        try:
            client = _make_client()
            response = client.get("/manifest?entrypoint=snow")
            assert response.status_code == 200
            assert response.json()["app_name"] == "snow-app"
        finally:
            svc_module.CONTRACT_GENERATED_DIR = original_dir

    def test_no_entrypoint_param_serves_root_manifest(self, tmp_path: Path) -> None:
        """Without entrypoint param, serves root manifest unchanged."""
        from application_sdk.handler import service as svc_module

        contract_dir = tmp_path / "generated"
        contract_dir.mkdir(parents=True)
        (contract_dir / "manifest.json").write_text(
            json.dumps({"execution_mode": "linear"})
        )

        original_dir = svc_module.CONTRACT_GENERATED_DIR
        svc_module.CONTRACT_GENERATED_DIR = contract_dir
        try:
            client = _make_client()
            response = client.get("/workflows/v1/manifest")
            assert response.status_code == 200
            assert response.json()["execution_mode"] == "linear"
        finally:
            svc_module.CONTRACT_GENERATED_DIR = original_dir
