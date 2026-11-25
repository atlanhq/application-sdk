"""Which request-body keys count as credential material, on the worker and the API host.

A v2 flat request sends a credential form's values as top-level keys. The SDK
detects one by its own shared keys (``host``, ``authType``, ...) and lifts them
into ``credentials``. It also lifts every top-level field of the app's generated
credential form (``kerberos``, an auth-type object such as ``gcp-wif``), read
from the generated tree on both surfaces. Those form fields never make a body a
credential on their own: some are as generic as ``name``.

Before this, connector apps patched ``application_sdk.handler.service`` at
import time, either extending the worker's key set::

    _svc._CREDENTIAL_KEYS = _svc._CREDENTIAL_KEYS | frozenset({"kerberos"})

or wrapping the normalization::

    _original = _svc._normalize_credentials
    _svc._normalize_credentials = lambda body: _lift(_original(body))

Both names are deprecated (removal v4.0.0). Reading one warns; a rebind is
still honoured on the worker, with a logged warning, so an app that has not
moved yet keeps working. The API host never honoured a rebind and still does
not.
"""

from __future__ import annotations

import shutil
import sys
import warnings
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

import application_sdk.handler.contracts as contracts
import application_sdk.handler.service as service
from application_sdk.app.base import App
from application_sdk.contracts.base import Input, Output
from application_sdk.handler.asgi import build_asgi_app
from application_sdk.handler.base import Handler
from application_sdk.handler.contracts import (
    AuthInput,
    AuthOutput,
    AuthStatus,
    MetadataInput,
    MetadataOutput,
    PreflightInput,
    PreflightOutput,
    PreflightStatus,
    SqlMetadataOutput,
)
from application_sdk.handler.metadata_routes import _credential_form_keys

#: BigQuery's generated tree, regenerated from tests/unit/handler/fixtures/
#: bigquery_contract by tests/regen_fixtures.py and used unchanged. Its
#: credential form declares the auth-type object ``gcp-wif`` at the top level.
_BIGQUERY_GENERATED = (
    Path(__file__).parent / "fixtures" / "generated_bigquery_credentials"
)
_FLAT_BODY: dict[str, Any] = {
    "authType": "gcp-wif",
    "connectorType": "rest",
    "gcp-wif": {"extra": {"project_id": "example-project"}},
}
_ROUTES = ("auth", "check", "metadata")
_LIFTED = "lifted-by-app"


class _CapturingHandler(Handler):
    """Records the credential keys (and metadata input) each route received."""

    def __init__(self) -> None:
        super().__init__()
        self.seen: dict[str, list[str]] = {}
        self.metadata_input: MetadataInput | None = None

    async def test_auth(self, input: AuthInput) -> AuthOutput:
        self.seen["auth"] = [c.key for c in input.credentials]
        return AuthOutput(status=AuthStatus.SUCCESS, message="ok")

    async def preflight_check(self, input: PreflightInput) -> PreflightOutput:
        self.seen["check"] = [c.key for c in input.credentials]
        return PreflightOutput(status=PreflightStatus.READY, message="ready")

    async def fetch_metadata(self, input: MetadataInput) -> MetadataOutput:
        self.seen["metadata"] = [c.key for c in input.credentials]
        self.metadata_input = input
        return SqlMetadataOutput(objects=[])


# Module-level so get_type_hints() resolves it under postponed annotations.
class _FlatStartInput(Input, allow_unbounded_fields=True):  # type: ignore[call-arg]
    name: str = ""


class _FlatStartOutput(Output, allow_unbounded_fields=True):  # type: ignore[call-arg]
    pass


@pytest.fixture
def generated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A copy of the bigquery tree, and the worker pointed at it."""
    target = tmp_path / "generated"
    shutil.copytree(_BIGQUERY_GENERATED, target)
    monkeypatch.setattr(service, "CONTRACT_GENERATED_DIR", target)
    return target


@pytest.fixture
def no_generated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An app that ships no generated tree."""
    target = tmp_path / "missing"
    monkeypatch.setattr(service, "CONTRACT_GENERATED_DIR", target)
    return target


def _worker(handler: Handler, _generated: Path, **kwargs: Any) -> TestClient:
    """The worker reads ``service.CONTRACT_GENERATED_DIR``, which the fixture set."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        app = service.create_app_handler_service(handler, app_name="kv-test", **kwargs)
    return TestClient(app, raise_server_exceptions=False)


def _host(handler: Handler, generated: Path) -> TestClient:
    return TestClient(
        build_asgi_app(
            handler,
            app_name="kv-test",
            generated_dir=generated,
            object_store=None,
        ),
        raise_server_exceptions=False,
    )


_SURFACES = {"worker": _worker, "host": _host}


def _post_routes(client: TestClient, body: dict[str, Any] = _FLAT_BODY) -> None:
    for route in _ROUTES:
        response = client.post(f"/workflows/v1/{route}", json=body)
        assert response.status_code == 200, (route, response.text)


def _start_input(body: dict[str, Any], generated: Path) -> _FlatStartInput:
    """POST /start; return the input handed to Temporal."""

    class _FlatApp(App):
        async def run(self, input: _FlatStartInput) -> _FlatStartOutput:
            return _FlatStartOutput()

    mock_client = MagicMock()
    mock_client.start_workflow = AsyncMock(
        return_value=MagicMock(id="wf-1", result_run_id="run-1")
    )
    with patch(
        "application_sdk.handler.service._get_temporal_client",
        new=AsyncMock(return_value=mock_client),
    ):
        client = _worker(
            _CapturingHandler(),
            generated,
            app_class=_FlatApp,
            temporal_host="temporal:7233",
        )
        response = client.post("/workflows/v1/start", json=body)
    assert response.status_code == 200, response.text
    (input_data,) = mock_client.start_workflow.await_args.kwargs["args"]
    return input_data


@pytest.fixture
def unbound_legacy_globals() -> Iterator[None]:
    """Leave no rebound deprecated global behind, whatever a test bound."""
    try:
        yield
    finally:
        for name in ("_CREDENTIAL_KEYS", "_normalize_credentials"):
            vars(service).pop(name, None)


def _read_legacy(name: str) -> Any:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        return getattr(service, name)


@pytest.fixture
def realm_rebound(unbound_legacy_globals: None) -> None:
    """Rebind the key set exactly as a connector app does."""
    service._CREDENTIAL_KEYS = _read_legacy("_CREDENTIAL_KEYS") | frozenset(  # type: ignore[attr-defined]
        {"realm"}
    )


@pytest.fixture
def normalize_wrapped(unbound_legacy_globals: None) -> list[dict[str, Any]]:
    """Wrap the normalization exactly as a connector app does; record each call."""
    calls: list[dict[str, Any]] = []
    original = _read_legacy("_normalize_credentials")

    def _wrapper(body: dict[str, Any]) -> dict[str, Any]:
        calls.append(dict(body))
        out = original(body)
        cfg = dict(out.get("connection_config") or {})
        cfg["legacy-key"] = _LIFTED
        return {**out, "connection_config": cfg}

    service._normalize_credentials = _wrapper  # type: ignore[attr-defined]
    return calls


# ---------------------------------------------------------------------------
# Credential keys from the generated credential form
# ---------------------------------------------------------------------------


class TestCredentialFormKeys:
    def test_reads_the_top_level_fields_of_the_credential_form(
        self, generated: Path
    ) -> None:
        assert _credential_form_keys(generated) == {
            "auth-type",
            "basic",
            "connector",
            "connectorType",
            "gcp-wif",
            "name",
        }

    def test_no_generated_tree_has_no_form_keys(self, no_generated: Path) -> None:
        assert _credential_form_keys(no_generated) == frozenset()

    @pytest.mark.parametrize("surface", sorted(_SURFACES))
    def test_routes_lift_the_form_field_into_credentials(
        self, surface: str, generated: Path
    ) -> None:
        handler = _CapturingHandler()
        _post_routes(_SURFACES[surface](handler, generated))
        for route in _ROUTES:
            assert "gcp-wif" in handler.seen[route], route
            assert "authType" in handler.seen[route], route

    @pytest.mark.parametrize("surface", sorted(_SURFACES))
    def test_without_a_form_only_the_sdk_keys_are_lifted(
        self, surface: str, no_generated: Path
    ) -> None:
        handler = _CapturingHandler()
        _post_routes(_SURFACES[surface](handler, no_generated))
        for route in _ROUTES:
            assert "gcp-wif" not in handler.seen[route], route
            assert "authType" in handler.seen[route], route

    @pytest.mark.parametrize("surface", sorted(_SURFACES))
    def test_a_form_field_alone_does_not_make_a_body_a_credential(
        self, surface: str, generated: Path
    ) -> None:
        """``name`` is a form field, but a workflow body can carry it too."""
        handler = _CapturingHandler()
        _post_routes(_SURFACES[surface](handler, generated), {"name": "x"})
        for route in _ROUTES:
            assert handler.seen[route] == [], route

    def test_sdk_set_is_not_mutated(self, generated: Path) -> None:
        _post_routes(_worker(_CapturingHandler(), generated))
        assert "gcp-wif" not in contracts._CREDENTIAL_KEYS

    def test_start_strips_only_the_credentials_key(self, generated: Path) -> None:
        input_data = _start_input({**_FLAT_BODY, "name": "x"}, generated)
        # /start deletes ``credentials`` and nothing else, so a flat key the
        # input declares still validates into it.
        assert input_data.name == "x"
        assert "credentials" not in input_data.model_dump()


_VALIDATE_PACKAGE = "fnd4688_validate_app"
_WIF_EXTRA = {
    "project_id": "example-project",
    "service_account_email": "sa@example.invalid",
    "wif_pool_provider_id": "example-pool",
    "atlan_oauth_id": "example-client-id",
    "atlan_oauth_secret": "example-client-secret",
}


@pytest.fixture
def validate_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[TestClient]:
    """The API host serving the bigquery tree as an importable app package."""
    package = tmp_path / _VALIDATE_PACKAGE
    generated = package / "generated"
    shutil.copytree(_BIGQUERY_GENERATED, generated)
    for init in (package, generated, generated / "crawler"):
        (init / "__init__.py").write_text("")
    monkeypatch.syspath_prepend(str(tmp_path))
    try:
        yield TestClient(
            build_asgi_app(
                _CapturingHandler(),
                app_name="validate-host",
                app_package=_VALIDATE_PACKAGE,
                generated_dir=generated,
                object_store=None,
            ),
            raise_server_exceptions=False,
        )
    finally:
        for name in [m for m in sys.modules if m.startswith(_VALIDATE_PACKAGE)]:
            del sys.modules[name]


class TestValidateWithAFormField:
    """A form field lifted as one pair is still checked field by field."""

    def _post(self, client: TestClient, extra: dict[str, str]) -> Any:
        return client.post(
            "/workflows/v1/validate?entrypoint=crawler",
            json={
                "credential_guid": "guid-1",
                "authType": "gcp-wif",
                "connectorType": "rest",
                "gcp-wif": {"extra": extra},
            },
        )

    def test_a_complete_auth_type_object_is_accepted(
        self, validate_host: TestClient
    ) -> None:
        response = self._post(validate_host, _WIF_EXTRA)
        assert response.status_code == 200, response.text

    def test_a_missing_field_inside_it_is_named(
        self, validate_host: TestClient
    ) -> None:
        extra = {k: v for k, v in _WIF_EXTRA.items() if k != "project_id"}
        response = self._post(validate_host, extra)
        assert response.status_code == 422, response.text
        assert [d["field"] for d in response.json()["detail"]] == [
            "credentials.extra.project_id"
        ]
        for value in extra.values():
            assert value not in response.text


# ---------------------------------------------------------------------------
# Deprecated: rebinding service._CREDENTIAL_KEYS / service._normalize_credentials
# ---------------------------------------------------------------------------


class TestDeprecatedServiceGlobals:
    @pytest.mark.parametrize(
        ("name", "replacement"),
        [
            ("_CREDENTIAL_KEYS", "generated credential form"),
            ("_normalize_credentials", "input contract"),
        ],
    )
    def test_reading_warns_and_names_the_replacement(
        self, name: str, replacement: str
    ) -> None:
        with pytest.warns(DeprecationWarning, match=replacement):
            getattr(service, name)

    def test_read_values_are_the_sdk_defaults(self) -> None:
        assert _read_legacy("_CREDENTIAL_KEYS") == contracts._CREDENTIAL_KEYS
        normalize = _read_legacy("_normalize_credentials")
        assert normalize(dict(_FLAT_BODY)) == contracts.normalize_credentials(
            dict(_FLAT_BODY)
        )

    def test_key_rebind_still_reaches_every_worker_route(
        self, realm_rebound: None, generated: Path
    ) -> None:
        handler = _CapturingHandler()
        _post_routes(_worker(handler, generated), {**_FLAT_BODY, "realm": "R"})
        for route in _ROUTES:
            assert "realm" in handler.seen[route], route
            # ...and the form's keys still apply alongside it.
            assert "gcp-wif" in handler.seen[route], route

    def test_key_rebind_after_the_service_is_built_still_applies(
        self, unbound_legacy_globals: None, generated: Path
    ) -> None:
        handler = _CapturingHandler()
        client = _worker(handler, generated)
        service._CREDENTIAL_KEYS = _read_legacy("_CREDENTIAL_KEYS") | frozenset(  # type: ignore[attr-defined]
            {"realm"}
        )
        _post_routes(client, {**_FLAT_BODY, "realm": "R"})
        assert "realm" in handler.seen["auth"]

    def test_key_rebind_logs_the_replacement_once(
        self, realm_rebound: None, generated: Path
    ) -> None:
        service._warned_legacy_rebinds.discard("_CREDENTIAL_KEYS")
        with patch.object(service, "logger") as log:
            _post_routes(_worker(_CapturingHandler(), generated))
        warned = [
            call.args
            for call in log.warning.call_args_list
            if "_CREDENTIAL_KEYS" in call.args
        ]
        assert len(warned) == 1
        assert "generated credential form" in warned[0][-1]

    def test_binding_back_the_value_read_is_not_a_rebind(
        self, unbound_legacy_globals: None
    ) -> None:
        service._CREDENTIAL_KEYS = _read_legacy("_CREDENTIAL_KEYS")  # type: ignore[attr-defined]
        assert service._legacy_rebinds() == {}

    def test_wrapper_still_reaches_every_worker_route_and_start(
        self, normalize_wrapped: list[dict[str, Any]], generated: Path
    ) -> None:
        handler = _CapturingHandler()
        _post_routes(_worker(handler, generated))
        assert len(normalize_wrapped) == len(_ROUTES)
        assert handler.metadata_input is not None
        assert handler.metadata_input.connection_config["legacy-key"] == _LIFTED
        _start_input({**_FLAT_BODY, "name": "x"}, generated)
        assert len(normalize_wrapped) == len(_ROUTES) + 1

    def test_wrapper_keeps_the_form_keys(
        self, normalize_wrapped: list[dict[str, Any]], generated: Path
    ) -> None:
        """A wrapper around the original still gets the app's form keys."""
        handler = _CapturingHandler()
        _post_routes(_worker(handler, generated))
        assert normalize_wrapped, "wrapper was not called"
        for route in _ROUTES:
            assert "gcp-wif" in handler.seen[route], route
        assert service._form_credential_keys.get() == frozenset()

    def test_wrapper_composes_with_key_rebinding(
        self,
        realm_rebound: None,
        normalize_wrapped: list[dict[str, Any]],
        generated: Path,
    ) -> None:
        handler = _CapturingHandler()
        _post_routes(_worker(handler, generated), {**_FLAT_BODY, "realm": "R"})
        assert "realm" in handler.seen["metadata"]
        assert handler.metadata_input is not None
        assert handler.metadata_input.connection_config["legacy-key"] == _LIFTED

    def test_host_ignores_rebinds(
        self,
        realm_rebound: None,
        normalize_wrapped: list[dict[str, Any]],
        generated: Path,
    ) -> None:
        handler = _CapturingHandler()
        _post_routes(_host(handler, generated), {**_FLAT_BODY, "realm": "R"})
        assert normalize_wrapped == []
        for route in _ROUTES:
            assert "realm" not in handler.seen[route], route
