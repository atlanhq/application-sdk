"""Handler service tests: per-entrypoint compute_manifest and handler hooks."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from application_sdk.handler.contracts import (
    ApiMetadataOutput,
    AuthInput,
    AuthOutput,
    AuthStatus,
    MetadataInput,
    PreflightCheck,
    PreflightInput,
    PreflightOutput,
    PreflightStatus,
    SqlMetadataObject,
    SqlMetadataOutput,
)
from application_sdk.handler.service import (
    create_app_handler_service,
)
from tests.unit.handler._service_support import (
    _install_fake_app_module,
    _make_client,
    _TestHandler,
)


class TestComputeManifestHook:
    """Per-entrypoint dynamic manifest hook.

    Apps that need to compute the manifest per submission (placeholder
    fill-in, SQL gen, full DAG rewrite) drop a `core.py` at
    ``app.<entrypoint_snake>.core`` exposing a
    ``compute_manifest(manifest, fe_inputs) -> dict`` callable. The SDK's
    /manifest handler discovers it via importlib and substitutes the
    return value as the response body.

    Tests inject a fake module into ``sys.modules`` to simulate the
    convention without touching the filesystem.
    """

    @staticmethod
    def _install_fake_core(
        monkeypatch: pytest.MonkeyPatch, entrypoint: str, fn: object
    ) -> None:
        """Register ``app.<snake>.core`` with ``compute_manifest = fn``."""
        _install_fake_app_module(monkeypatch, entrypoint, "core", compute_manifest=fn)

    def test_optional_import_returns_none_when_target_absent(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A genuinely-missing module (target or a parent) → None (fall-through)."""
        import importlib

        from application_sdk.handler import service as svc

        def _missing(name):
            raise ModuleNotFoundError(f"No module named {name!r}", name=name)

        monkeypatch.setattr(importlib, "import_module", _missing)
        assert svc._import_optional_app_module("app.foo.core") is None

    def test_optional_import_reraises_transitive_failure(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A module that exists but whose own import fails (missing dependency)
        must surface, not be swallowed into a silent fall-through."""
        import importlib

        from application_sdk.handler import service as svc

        def _broken(name):
            # The target's code imports 'somedep', which isn't installed.
            raise ModuleNotFoundError("No module named 'somedep'", name="somedep")

        monkeypatch.setattr(importlib, "import_module", _broken)
        with pytest.raises(ModuleNotFoundError):
            svc._import_optional_app_module("app.foo.core")

    def test_hook_invoked_when_module_exists(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """compute_manifest is called and its return becomes the body."""
        from application_sdk.handler import service as svc_module

        contract_dir = tmp_path / "generated"
        ep_dir = contract_dir / "hook-ep"
        ep_dir.mkdir(parents=True)
        (ep_dir / "manifest.json").write_text(
            json.dumps({"dag": {"extract": {"static": True}}})
        )

        captured: dict[str, object] = {}

        async def compute_manifest(manifest: dict, fe_inputs: dict) -> dict:
            captured["manifest"] = manifest
            captured["fe_inputs"] = fe_inputs
            return {"dag": {"extract": {"computed": True, "echo": fe_inputs}}}

        self._install_fake_core(monkeypatch, "hook-ep", compute_manifest)

        original = svc_module.CONTRACT_GENERATED_DIR
        svc_module.CONTRACT_GENERATED_DIR = contract_dir
        try:
            client = _make_client()
            payload = {"foo": "bar"}
            response = client.get(
                "/workflows/v1/manifest",
                params={"entrypoint": "hook-ep", "fe_inputs": json.dumps(payload)},
            )
            assert response.status_code == 200
            body = response.json()
            assert body == {"dag": {"extract": {"computed": True, "echo": payload}}}
            # The hook receives the static manifest unmodified and the decoded form.
            assert captured["manifest"] == {"dag": {"extract": {"static": True}}}
            assert captured["fe_inputs"] == payload
        finally:
            svc_module.CONTRACT_GENERATED_DIR = original

    def test_hook_output_is_reconciled_not_served_verbatim(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """FND-195: the hook *replaces* the manifest, so its output needs the same
        queue reconciliation as the static file — otherwise the guarantee is void
        for precisely the apps that need it, since a bundle's marketplace entry
        points have their DAG computed per submission by this hook.

        The static file here carries no queue at all; the template appears only
        in what the hook emits, so a pre-hook-only substitution cannot catch it.
        """
        from application_sdk.handler import service as svc_module

        contract_dir = tmp_path / "generated"
        ep_dir = contract_dir / "hook-ep"
        ep_dir.mkdir(parents=True)
        (ep_dir / "manifest.json").write_text(json.dumps({"dag": {}}))

        async def compute_manifest(manifest: dict, fe_inputs: dict) -> dict:
            # A per-submission DAG built at request time, carrying the un-baked
            # template — nothing baked this, the hook just wrote it.
            return {
                "dag": {
                    "extract": {
                        "app_name": "{app_name}",
                        "inputs": {"task_queue": "atlan-{app_name}-{deployment_name}"},
                    }
                }
            }

        self._install_fake_core(monkeypatch, "hook-ep", compute_manifest)
        monkeypatch.setenv("ATLAN_APPLICATION_NAME", "dbt-v3")
        monkeypatch.setenv("ATLAN_DEPLOYMENT_NAME", "prod")

        worker_queue = "atlan-dbt-v3-prod"
        original = svc_module.CONTRACT_GENERATED_DIR
        svc_module.CONTRACT_GENERATED_DIR = contract_dir
        try:
            app = create_app_handler_service(
                _TestHandler(), app_name="dbt-v3", task_queue=worker_queue
            )
            response = TestClient(app).get(
                "/workflows/v1/manifest", params={"entrypoint": "hook-ep"}
            )
            assert response.status_code == 200
            node = response.json()["dag"]["extract"]
            assert node["inputs"]["task_queue"] == worker_queue
            assert node["app_name"] == "dbt-v3"
        finally:
            svc_module.CONTRACT_GENERATED_DIR = original

    def test_hook_output_unresolvable_app_name_keeps_the_token_visible(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """With no app name available the hook's output must not have one
        manufactured for it either — same contract as the static paths.

        Asserts the *graded* outcome, not just the surviving token: the
        deployment token is filled (proving the reconciliation pass ran on the
        hook's output at all) while ``{app_name}`` is left visible. Checking only
        for the surviving token would pass against a build that never reconciles
        the hook output, since serving it verbatim also leaves the token in.
        """
        from application_sdk.handler import service as svc_module

        contract_dir = tmp_path / "generated"
        ep_dir = contract_dir / "hook-ep"
        ep_dir.mkdir(parents=True)
        (ep_dir / "manifest.json").write_text(json.dumps({"dag": {}}))

        async def compute_manifest(manifest: dict, fe_inputs: dict) -> dict:
            return {
                "dag": {
                    "extract": {
                        "inputs": {"task_queue": "atlan-{app_name}-{deployment_name}"}
                    }
                }
            }

        self._install_fake_core(monkeypatch, "hook-ep", compute_manifest)
        monkeypatch.delenv("ATLAN_APPLICATION_NAME", raising=False)
        monkeypatch.setenv("ATLAN_DEPLOYMENT_NAME", "prod")

        original = svc_module.CONTRACT_GENERATED_DIR
        svc_module.CONTRACT_GENERATED_DIR = contract_dir
        try:
            app = create_app_handler_service(_TestHandler(), app_name="")
            response = TestClient(app).get(
                "/workflows/v1/manifest", params={"entrypoint": "hook-ep"}
            )
            assert response.status_code == 200
            served = response.json()["dag"]["extract"]["inputs"]["task_queue"]
            # The pass ran: the deployment token resolved.
            assert "{deployment_name}" not in served
            assert "prod" in served
            # ...but nothing was invented for the app name.
            assert "{app_name}" in served
            assert "default" not in served
        finally:
            svc_module.CONTRACT_GENERATED_DIR = original

    def test_async_hook_is_awaited(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An ``async def compute_manifest`` is awaited directly (not run via
        ``run_in_thread``, which would hand back an un-awaited coroutine)."""
        from application_sdk.handler import service as svc_module

        contract_dir = tmp_path / "generated"
        ep_dir = contract_dir / "hook-ep"
        ep_dir.mkdir(parents=True)
        (ep_dir / "manifest.json").write_text(json.dumps({"dag": {}}))

        async def compute_manifest(manifest: dict, fe_inputs: dict) -> dict:
            return {"dag": {"computed": "async", "echo": fe_inputs}}

        self._install_fake_core(monkeypatch, "hook-ep", compute_manifest)

        original = svc_module.CONTRACT_GENERATED_DIR
        svc_module.CONTRACT_GENERATED_DIR = contract_dir
        try:
            client = _make_client()
            response = client.get(
                "/workflows/v1/manifest",
                params={"entrypoint": "hook-ep", "fe_inputs": json.dumps({"k": "v"})},
            )
            assert response.status_code == 200
            assert response.json() == {"dag": {"computed": "async", "echo": {"k": "v"}}}
        finally:
            svc_module.CONTRACT_GENERATED_DIR = original

    def test_static_manifest_when_no_hook(self, tmp_path: Path) -> None:
        """No app.<snake>.core module → static manifest returned unchanged."""
        from application_sdk.handler import service as svc_module

        contract_dir = tmp_path / "generated"
        ep_dir = contract_dir / "no-hook"
        ep_dir.mkdir(parents=True)
        manifest_data = {"dag": {"extract": {"static": True}}}
        (ep_dir / "manifest.json").write_text(json.dumps(manifest_data))

        original = svc_module.CONTRACT_GENERATED_DIR
        svc_module.CONTRACT_GENERATED_DIR = contract_dir
        try:
            client = _make_client()
            response = client.get("/workflows/v1/manifest?entrypoint=no-hook")
            assert response.status_code == 200
            assert response.json() == manifest_data
        finally:
            svc_module.CONTRACT_GENERATED_DIR = original

    def test_non_callable_compute_manifest_ignored(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A `compute_manifest` attribute that isn't callable (e.g. an app that
        writes `compute_manifest = SOME_DICT`) is ignored → static manifest."""
        from application_sdk.handler import service as svc_module

        contract_dir = tmp_path / "generated"
        ep_dir = contract_dir / "bad-hook"
        ep_dir.mkdir(parents=True)
        manifest_data = {"dag": {"static": True}}
        (ep_dir / "manifest.json").write_text(json.dumps(manifest_data))

        # Not callable — a plain dict assigned to the attribute name.
        self._install_fake_core(monkeypatch, "bad-hook", {"not": "callable"})

        original = svc_module.CONTRACT_GENERATED_DIR
        svc_module.CONTRACT_GENERATED_DIR = contract_dir
        try:
            client = _make_client()
            response = client.get("/workflows/v1/manifest?entrypoint=bad-hook")
            assert response.status_code == 200
            assert response.json() == manifest_data
        finally:
            svc_module.CONTRACT_GENERATED_DIR = original

    def test_sync_compute_manifest_ignored(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The hook is async-only: a *sync* ``def compute_manifest`` is not
        discovered and the route serves the static manifest unchanged (rather
        than running it via ``run_in_thread``)."""
        from application_sdk.handler import service as svc_module

        contract_dir = tmp_path / "generated"
        ep_dir = contract_dir / "sync-hook"
        ep_dir.mkdir(parents=True)
        manifest_data = {"dag": {"static": True}}
        (ep_dir / "manifest.json").write_text(json.dumps(manifest_data))

        # Sync def — must be ignored under the async-only contract.
        def compute_manifest(manifest: dict, fe_inputs: dict) -> dict:
            return {"dag": {"computed": "should-not-appear"}}

        self._install_fake_core(monkeypatch, "sync-hook", compute_manifest)

        original = svc_module.CONTRACT_GENERATED_DIR
        svc_module.CONTRACT_GENERATED_DIR = contract_dir
        try:
            response = _make_client().get("/workflows/v1/manifest?entrypoint=sync-hook")
            assert response.status_code == 200
            assert response.json() == manifest_data
        finally:
            svc_module.CONTRACT_GENERATED_DIR = original

    def test_fe_inputs_defaults_to_empty_dict(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When the FE doesn't send `fe_inputs`, the hook gets an empty dict."""
        from application_sdk.handler import service as svc_module

        contract_dir = tmp_path / "generated"
        ep_dir = contract_dir / "default-form"
        ep_dir.mkdir(parents=True)
        (ep_dir / "manifest.json").write_text(json.dumps({"k": "v"}))

        captured: dict[str, object] = {}

        async def compute_manifest(manifest: dict, fe_inputs: dict) -> dict:
            captured["fe_inputs"] = fe_inputs
            return manifest

        self._install_fake_core(monkeypatch, "default-form", compute_manifest)

        original = svc_module.CONTRACT_GENERATED_DIR
        svc_module.CONTRACT_GENERATED_DIR = contract_dir
        try:
            client = _make_client()
            response = client.get("/workflows/v1/manifest?entrypoint=default-form")
            assert response.status_code == 200
            assert captured["fe_inputs"] == {}
        finally:
            svc_module.CONTRACT_GENERATED_DIR = original

    def test_invalid_fe_inputs_returns_400(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Malformed fe_inputs JSON → 400 (not 500)."""
        from application_sdk.handler import service as svc_module

        contract_dir = tmp_path / "generated"
        ep_dir = contract_dir / "bad-form"
        ep_dir.mkdir(parents=True)
        (ep_dir / "manifest.json").write_text("{}")

        async def compute_manifest(manifest: dict, fe_inputs: dict) -> dict:
            return manifest

        self._install_fake_core(monkeypatch, "bad-form", compute_manifest)

        original = svc_module.CONTRACT_GENERATED_DIR
        svc_module.CONTRACT_GENERATED_DIR = contract_dir
        try:
            client = _make_client()
            response = client.get(
                "/workflows/v1/manifest",
                params={"entrypoint": "bad-form", "fe_inputs": "not-json{"},
            )
            assert response.status_code == 400
            assert "fe_inputs is not valid JSON" in response.json()["detail"]
        finally:
            svc_module.CONTRACT_GENERATED_DIR = original

    def test_oversize_fe_inputs_returns_413(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """fe_inputs over the byte cap → 413 (clear error, not opaque
        downstream truncation), and the hook is never invoked."""
        from application_sdk.handler import service as svc_module

        contract_dir = tmp_path / "generated"
        ep_dir = contract_dir / "big-form"
        ep_dir.mkdir(parents=True)
        (ep_dir / "manifest.json").write_text("{}")

        async def compute_manifest(manifest: dict, fe_inputs: dict) -> dict:
            raise AssertionError("hook must not run for oversize fe_inputs")

        self._install_fake_core(monkeypatch, "big-form", compute_manifest)

        # Valid JSON, but larger than the cap.
        huge = json.dumps({"x": "a" * (svc_module._MAX_FE_INPUTS_BYTES + 100)})
        assert len(huge.encode("utf-8")) > svc_module._MAX_FE_INPUTS_BYTES

        original = svc_module.CONTRACT_GENERATED_DIR
        svc_module.CONTRACT_GENERATED_DIR = contract_dir
        try:
            client = _make_client()
            response = client.get(
                "/workflows/v1/manifest",
                params={"entrypoint": "big-form", "fe_inputs": huge},
            )
            assert response.status_code == 413
            assert "fe_inputs exceeds" in response.json()["detail"]
        finally:
            svc_module.CONTRACT_GENERATED_DIR = original

    def _run_hook(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        entrypoint: str,
        hook: object,
    ):
        """Install ``hook`` as compute_manifest for ``entrypoint`` and GET the
        manifest; returns the response."""
        from application_sdk.handler import service as svc_module

        contract_dir = tmp_path / "generated"
        ep_dir = contract_dir / entrypoint
        ep_dir.mkdir(parents=True)
        (ep_dir / "manifest.json").write_text("{}")
        self._install_fake_core(monkeypatch, entrypoint, hook)

        original = svc_module.CONTRACT_GENERATED_DIR
        svc_module.CONTRACT_GENERATED_DIR = contract_dir
        try:
            return _make_client().get(
                "/workflows/v1/manifest", params={"entrypoint": entrypoint}
            )
        finally:
            svc_module.CONTRACT_GENERATED_DIR = original

    def test_hook_generic_exception_returns_generic_500(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A hook raising a generic exception → 500 with a generic body; the
        exception text is NOT leaked to the client."""

        async def boom(manifest: dict, fe_inputs: dict) -> dict:
            raise RuntimeError("secret connection string leaked here")

        response = self._run_hook(tmp_path, monkeypatch, "hook-boom", boom)
        assert response.status_code == 500
        assert response.json()["detail"] == "Internal server error"
        assert "secret connection string" not in response.text

    def test_hook_httpexception_passes_through(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A hook raising HTTPException → its status/detail pass through
        (the ``except HTTPException: raise`` branch), not coerced to 500."""
        from fastapi import HTTPException

        async def conflict(manifest: dict, fe_inputs: dict) -> dict:
            raise HTTPException(status_code=409, detail="entrypoint conflict")

        response = self._run_hook(tmp_path, monkeypatch, "hook-conflict", conflict)
        assert response.status_code == 409
        assert response.json()["detail"] == "entrypoint conflict"

    def test_hook_non_dict_return_returns_500(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A hook returning a non-dict (list/str/None) → 500 (isinstance guard)."""

        async def returns_list(manifest: dict, fe_inputs: dict):
            return [1, 2, 3]

        response = self._run_hook(tmp_path, monkeypatch, "hook-list", returns_list)
        assert response.status_code == 500
        assert response.json()["detail"] == "Internal server error"

    def test_fe_inputs_at_exact_cap_passes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Exactly ``_MAX_FE_INPUTS_BYTES`` is allowed — the cap is ``>``, not
        ``>=`` (boundary guard against an off-by-one)."""
        from application_sdk.handler import service as svc_module

        contract_dir = tmp_path / "generated"
        ep_dir = contract_dir / "at-cap"
        ep_dir.mkdir(parents=True)
        (ep_dir / "manifest.json").write_text("{}")

        async def compute_manifest(manifest: dict, fe_inputs: dict) -> dict:
            return {"ok": True}

        self._install_fake_core(monkeypatch, "at-cap", compute_manifest)

        overhead = len(json.dumps({"x": ""}).encode("utf-8"))
        at_cap = json.dumps({"x": "a" * (svc_module._MAX_FE_INPUTS_BYTES - overhead)})
        assert len(at_cap.encode("utf-8")) == svc_module._MAX_FE_INPUTS_BYTES

        original = svc_module.CONTRACT_GENERATED_DIR
        svc_module.CONTRACT_GENERATED_DIR = contract_dir
        try:
            response = _make_client().get(
                "/workflows/v1/manifest",
                params={"entrypoint": "at-cap", "fe_inputs": at_cap},
            )
            assert response.status_code == 200
            assert response.json() == {"ok": True}
        finally:
            svc_module.CONTRACT_GENERATED_DIR = original

    def test_fe_inputs_one_byte_over_cap_returns_413(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """One byte over the cap → 413 (the other half of the boundary)."""
        from application_sdk.handler import service as svc_module

        contract_dir = tmp_path / "generated"
        ep_dir = contract_dir / "over-cap"
        ep_dir.mkdir(parents=True)
        (ep_dir / "manifest.json").write_text("{}")

        async def compute_manifest(manifest: dict, fe_inputs: dict) -> dict:
            return {"ok": True}

        self._install_fake_core(monkeypatch, "over-cap", compute_manifest)

        overhead = len(json.dumps({"x": ""}).encode("utf-8"))
        over = json.dumps({"x": "a" * (svc_module._MAX_FE_INPUTS_BYTES - overhead + 1)})
        assert len(over.encode("utf-8")) == svc_module._MAX_FE_INPUTS_BYTES + 1

        original = svc_module.CONTRACT_GENERATED_DIR
        svc_module.CONTRACT_GENERATED_DIR = contract_dir
        try:
            response = _make_client().get(
                "/workflows/v1/manifest",
                params={"entrypoint": "over-cap", "fe_inputs": over},
            )
            assert response.status_code == 413
        finally:
            svc_module.CONTRACT_GENERATED_DIR = original

    def test_legacy_alias_forwards_fe_inputs(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Heracles' /manifest (no version) also routes through the hook."""
        from application_sdk.handler import service as svc_module

        contract_dir = tmp_path / "generated"
        ep_dir = contract_dir / "legacy-ep"
        ep_dir.mkdir(parents=True)
        (ep_dir / "manifest.json").write_text(json.dumps({"orig": True}))

        async def compute_manifest(manifest: dict, fe_inputs: dict) -> dict:
            return {**manifest, "fe": fe_inputs}

        self._install_fake_core(monkeypatch, "legacy-ep", compute_manifest)

        original = svc_module.CONTRACT_GENERATED_DIR
        svc_module.CONTRACT_GENERATED_DIR = contract_dir
        try:
            client = _make_client()
            response = client.get(
                "/manifest",
                params={"entrypoint": "legacy-ep", "fe_inputs": json.dumps({"x": 1})},
            )
            assert response.status_code == 200
            assert response.json() == {"orig": True, "fe": {"x": 1}}
        finally:
            svc_module.CONTRACT_GENERATED_DIR = original

    def test_kebab_entrypoint_resolves_to_snake_module(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Kebab `csa-hello-a` → import `app.csa_hello_a.core`."""
        from application_sdk.handler import service as svc_module

        contract_dir = tmp_path / "generated"
        ep_dir = contract_dir / "csa-hello-a"  # kebab on disk
        ep_dir.mkdir(parents=True)
        (ep_dir / "manifest.json").write_text(json.dumps({"static": True}))

        async def compute_manifest(manifest: dict, fe_inputs: dict) -> dict:
            return {"computed": True}

        # Module is registered under app.csa_hello_a (snake) — the hook does the translation.
        self._install_fake_core(monkeypatch, "csa-hello-a", compute_manifest)

        original = svc_module.CONTRACT_GENERATED_DIR
        svc_module.CONTRACT_GENERATED_DIR = contract_dir
        try:
            client = _make_client()
            response = client.get("/workflows/v1/manifest?entrypoint=csa-hello-a")
            assert response.status_code == 200
            assert response.json() == {"computed": True}
        finally:
            svc_module.CONTRACT_GENERATED_DIR = original


class TestPerEntrypointHandlerHook:
    """Per-entrypoint handler discovery for /workflows/v1/{auth,check,metadata}.

    Multi-entrypoint apps drop ``app.<entrypoint_snake>.handler`` modules
    that expose plain async ``test_auth``, ``preflight_check``,
    ``fetch_metadata`` functions. The orchestrator sends the exact entry-point
    name in the ``entrypoint`` field (resolved from the marketplace catalog);
    the SDK dispatches to the per-entrypoint module **by exact name** when
    present, falling through to the app-level ``Handler`` instance otherwise.

    Tests inject fake modules into ``sys.modules`` to simulate the discovery
    convention (no filesystem / contract-dir glob is involved on the explicit
    path).
    """

    @staticmethod
    def _install_fake_handler(
        monkeypatch: pytest.MonkeyPatch, entrypoint: str, **fns: object
    ) -> None:
        """Register ``app.<snake>.handler`` exposing the given async fns."""
        _install_fake_app_module(monkeypatch, entrypoint, "handler", **fns)

    # ------------------------------------------------------------------
    # /workflows/v1/auth
    # ------------------------------------------------------------------

    def test_auth_dispatches_to_entrypoint_module(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Explicit ``entrypoint`` routes to the per-entrypoint test_auth by exact
        name — a direct lookup, no contract-dir glob or connector parsing."""
        captured: dict[str, object] = {}

        async def test_auth(input: AuthInput, ctx) -> AuthOutput:
            captured["entrypoint"] = input.entrypoint
            captured["app_name"] = ctx.app_name
            return AuthOutput(status=AuthStatus.FAILED, message="entrypoint says no")

        self._install_fake_handler(monkeypatch, "csa-hello-a", test_auth=test_auth)

        client = _make_client()
        response = client.post(
            "/workflows/v1/auth",
            json={"entrypoint": "csa-hello-a", "credentials": []},
        )
        assert response.status_code == 401
        body = response.json()
        assert body["data"]["status"] == "failed"
        assert body["data"]["message"] == "entrypoint says no"
        assert captured["entrypoint"] == "csa-hello-a"
        assert captured["app_name"] == "test-app"

    def test_auth_falls_back_when_no_entrypoint_module(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A valid ``entrypoint`` with no ``app.<segment>.handler`` module → the
        app-level Handler.test_auth runs (deterministic is/isn't fallback)."""
        client = _make_client()
        response = client.post(
            "/workflows/v1/auth",
            json={"entrypoint": "sdk-fallback-noep-auth", "credentials": []},
        )
        assert response.status_code == 200
        # _TestHandler returns SUCCESS with "auth ok"
        assert response.json()["data"]["message"] == "auth ok"

    def test_auth_sync_handler_fn_falls_back(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A *sync* ``def test_auth`` in the entrypoint module is rejected by
        discovery (it would TypeError on ``await``) → app-level Handler runs."""

        def test_auth(input: AuthInput, ctx) -> AuthOutput:  # not async — invalid
            raise AssertionError("sync handler must not be dispatched")

        self._install_fake_handler(monkeypatch, "csa-hello-a", test_auth=test_auth)

        client = _make_client()
        response = client.post(
            "/workflows/v1/auth",
            json={"entrypoint": "csa-hello-a", "credentials": []},
        )
        assert response.status_code == 200
        assert response.json()["data"]["message"] == "auth ok"

    def test_auth_non_callable_handler_attr_falls_back(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A non-callable ``test_auth`` attribute (e.g. ``test_auth = "x"``) is
        ignored by discovery → app-level Handler runs."""
        self._install_fake_handler(monkeypatch, "csa-hello-a", test_auth="not-a-fn")

        client = _make_client()
        response = client.post(
            "/workflows/v1/auth",
            json={"entrypoint": "csa-hello-a", "credentials": []},
        )
        assert response.status_code == 200
        assert response.json()["data"]["message"] == "auth ok"

    def test_auth_falls_back_when_entrypoint_empty(self) -> None:
        """No ``entrypoint`` → app-level Handler.test_auth runs (single-entrypoint compat)."""
        client = _make_client()
        response = client.post(
            "/workflows/v1/auth",
            json={"credentials": []},
        )
        assert response.status_code == 200
        assert response.json()["data"]["message"] == "auth ok"

    def test_auth_invalid_entrypoint_name_returns_400(self) -> None:
        """A non-empty but malformed ``entrypoint`` (e.g. a path-traversal
        attempt) is a client error → 400, consistent with the manifest and
        input-contract routes — NOT a silent fall-back to the app-level
        Handler / default entrypoint."""
        client = _make_client()
        for bad in ("../evil", "1leading-digit", "has space", "dot.sep"):
            response = client.post(
                "/workflows/v1/auth",
                json={"entrypoint": bad, "credentials": []},
            )
            assert response.status_code == 400, f"expected 400 for {bad!r}"
            assert response.json()["detail"] == "Invalid entrypoint name"

    def test_check_invalid_entrypoint_name_returns_400(self) -> None:
        """Malformed ``entrypoint`` on /check → 400 (consistency across routes)."""
        client = _make_client()
        response = client.post(
            "/workflows/v1/check",
            json={"entrypoint": "../evil", "credentials": []},
        )
        assert response.status_code == 400
        assert response.json()["detail"] == "Invalid entrypoint name"

    def test_metadata_invalid_entrypoint_name_returns_400(self) -> None:
        """Malformed ``entrypoint`` on /metadata → 400 (consistency across routes)."""
        client = _make_client()
        response = client.post(
            "/workflows/v1/metadata",
            json={"entrypoint": "../evil", "credentials": []},
        )
        assert response.status_code == 400
        assert response.json()["detail"] == "Invalid entrypoint name"

    def test_auth_connector_is_not_used_for_routing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The legacy ``connector`` field no longer drives dispatch. A connector
        naming a real entrypoint module is ignored when no ``entrypoint`` is sent
        — routing is explicit-only (the per-entrypoint fn must not run)."""

        async def test_auth(input: AuthInput, ctx) -> AuthOutput:
            raise AssertionError("connector must not trigger per-entrypoint dispatch")

        self._install_fake_handler(monkeypatch, "csa-hello-a", test_auth=test_auth)

        client = _make_client()
        response = client.post(
            "/workflows/v1/auth",
            json={"connector": "test-bundle-csa-hello-a", "credentials": []},
        )
        assert response.status_code == 200
        assert response.json()["data"]["message"] == "auth ok"

    # ------------------------------------------------------------------
    # /workflows/v1/check
    # ------------------------------------------------------------------

    def test_check_dispatches_to_entrypoint_module(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Per-entrypoint preflight_check is awaited; its checks render in response."""

        async def preflight_check(input: PreflightInput, ctx) -> PreflightOutput:
            assert input.entrypoint == "csa-hello-b"
            assert ctx.app_name == "test-app"
            return PreflightOutput(
                status=PreflightStatus.NOT_READY,
                checks=[
                    PreflightCheck(
                        name="recipientCheck",
                        passed=False,
                        message="recipient missing",
                    )
                ],
            )

        self._install_fake_handler(
            monkeypatch, "csa-hello-b", preflight_check=preflight_check
        )

        client = _make_client()
        response = client.post(
            "/workflows/v1/check",
            json={"entrypoint": "csa-hello-b", "credentials": []},
        )
        assert response.status_code == 200
        body = response.json()
        # Envelope success reports that preflight ran (a check was produced), not
        # that every check passed — per DBBI-665. The failing check surfaces in
        # data.recipientCheck.
        assert body["success"] is True
        assert body["data"]["recipientCheck"] == {
            "success": False,
            "message": "recipient missing",
            "successMessage": "",
            "failureMessage": "recipient missing",
        }

    def test_check_falls_back_when_no_entrypoint_module(self) -> None:
        """A valid ``entrypoint`` with no handler module → app-level
        Handler.preflight_check runs."""
        client = _make_client()
        response = client.post(
            "/workflows/v1/check",
            json={"entrypoint": "sdk-fallback-noep-check", "credentials": []},
        )
        assert response.status_code == 200
        # Fell back to the app-level Handler; the default test handler's preflight
        # returns status=ready, message "ready".
        assert response.json()["message"] == "ready"

    # ------------------------------------------------------------------
    # /workflows/v1/metadata
    # ------------------------------------------------------------------

    def test_metadata_dispatches_to_entrypoint_module(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Per-entrypoint fetch_metadata is awaited; its objects render in response."""

        async def fetch_metadata(input: MetadataInput, ctx) -> SqlMetadataOutput:
            assert input.entrypoint == "csa-hello-d"
            return SqlMetadataOutput(
                objects=[
                    SqlMetadataObject(TABLE_CATALOG="cat", TABLE_SCHEMA="sch1"),
                    SqlMetadataObject(TABLE_CATALOG="cat", TABLE_SCHEMA="sch2"),
                ]
            )

        self._install_fake_handler(
            monkeypatch, "csa-hello-d", fetch_metadata=fetch_metadata
        )

        client = _make_client()
        response = client.post(
            "/workflows/v1/metadata",
            json={"entrypoint": "csa-hello-d", "credentials": []},
        )
        assert response.status_code == 200
        body = response.json()
        assert len(body["data"]) == 2
        assert body["data"][0] == {"TABLE_CATALOG": "cat", "TABLE_SCHEMA": "sch1"}

    def test_metadata_falls_back_when_no_entrypoint_module(self) -> None:
        """A valid ``entrypoint`` with no handler module → app-level
        Handler.fetch_metadata runs."""
        client = _make_client()
        response = client.post(
            "/workflows/v1/metadata",
            json={"entrypoint": "sdk-fallback-noep-meta", "credentials": []},
        )
        assert response.status_code == 200
        # _TestHandler returns empty SqlMetadataOutput
        assert response.json()["data"] == []

    def test_metadata_input_object_filter_from_metadata_template_key(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Heracles forwards the widget filter key as ``metadataTemplateKey`` (and
        a ``type`` mirror) but the v3 SDK contract uses ``object_filter``. Verify
        the bridge: per-entrypoint hooks read the widget key from
        ``input.object_filter``."""
        captured: dict[str, str] = {}

        async def fetch_metadata(input: MetadataInput, ctx) -> ApiMetadataOutput:
            captured["object_filter"] = input.object_filter
            return ApiMetadataOutput(objects=[])

        self._install_fake_handler(
            monkeypatch, "csa-meta-bridge", fetch_metadata=fetch_metadata
        )

        client = _make_client()
        response = client.post(
            "/workflows/v1/metadata",
            json={
                "entrypoint": "csa-meta-bridge",
                "credentials": [],
                "metadataTemplateKey": "tags",
            },
        )
        assert response.status_code == 200
        assert captured["object_filter"] == "tags"

    def test_metadata_input_object_filter_explicit_wins_over_template_key(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If the body sets ``object_filter`` explicitly, the bridge does not
        overwrite it with ``metadataTemplateKey``."""
        captured: dict[str, str] = {}

        async def fetch_metadata(input: MetadataInput, ctx) -> ApiMetadataOutput:
            captured["object_filter"] = input.object_filter
            return ApiMetadataOutput(objects=[])

        self._install_fake_handler(
            monkeypatch, "csa-meta-bridge-2", fetch_metadata=fetch_metadata
        )

        client = _make_client()
        response = client.post(
            "/workflows/v1/metadata",
            json={
                "entrypoint": "csa-meta-bridge-2",
                "credentials": [],
                "object_filter": "public.*",
                "metadataTemplateKey": "tags",
            },
        )
        assert response.status_code == 200
        assert captured["object_filter"] == "public.*"

    # ------------------------------------------------------------------
    # Errors
    # ------------------------------------------------------------------

    def test_entrypoint_fn_raising_surfaces_as_500(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Per-entrypoint fn raising an unexpected exception → 500 (same as Handler.<fn>)."""

        async def test_auth(input: AuthInput, ctx) -> AuthOutput:
            raise RuntimeError("entrypoint blew up")

        self._install_fake_handler(monkeypatch, "csa-hello-a", test_auth=test_auth)

        client = _make_client()
        response = client.post(
            "/workflows/v1/auth",
            json={"entrypoint": "csa-hello-a", "credentials": []},
        )
        assert response.status_code == 500
