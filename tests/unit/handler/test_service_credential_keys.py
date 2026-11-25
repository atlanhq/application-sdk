"""How an app extends request normalization, on the worker and the API host.

The public seams are two ``Handler`` attributes, read on every surface that
serves the handler:

* ``extra_credential_keys`` — v2 flat top-level keys, beyond the SDK's own,
  that count as credential material (e.g. ``kerberos``);
* ``prepare_request_body`` — rewrites the raw body before its credentials are
  normalized (e.g. lifting a legacy key into the field the input reads).

Before them, connector apps patched ``application_sdk.handler.service`` at
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

import sys
import textwrap
import warnings
from collections.abc import Iterator
from pathlib import Path
from typing import Any, ClassVar
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

import application_sdk.handler.contracts as contracts
import application_sdk.handler.service as service
from application_sdk.app.base import App
from application_sdk.contracts.base import Input, Output
from application_sdk.handler.asgi import build_asgi_app
from application_sdk.handler.base import DefaultHandler, Handler
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

_KERBEROS = {"principal": "svc@EXAMPLE.COM", "keytab": "a2V5dGFi"}
_FLAT_BODY: dict[str, Any] = {
    "host": "db.example.com",
    "port": 10000,
    "username": "svc",
    "kerberos": _KERBEROS,
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


class _KerberosHandler(_CapturingHandler):
    extra_credential_keys: ClassVar[frozenset[str]] = frozenset({"kerberos"})


class _LiftingHandler(_CapturingHandler):
    """Lifts a legacy top-level key into ``connection_config``, as databricks does."""

    def __init__(self) -> None:
        super().__init__()
        self.prepared: list[dict[str, Any]] = []

    def prepare_request_body(self, body: dict[str, Any]) -> dict[str, Any]:
        self.prepared.append(dict(body))
        cfg = dict(body.get("connection_config") or {})
        cfg["legacy-key"] = _LIFTED
        return {**body, "connection_config": cfg}


# Module-level so get_type_hints() resolves it under postponed annotations.
class _KerberosStartInput(Input, allow_unbounded_fields=True):  # type: ignore[call-arg]
    name: str = ""
    kerberos: dict[str, str] | None = None


class _KerberosStartOutput(Output, allow_unbounded_fields=True):  # type: ignore[call-arg]
    pass


def _worker(handler: Handler, **kwargs: Any) -> TestClient:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        app = service.create_app_handler_service(handler, app_name="kv-test", **kwargs)
    return TestClient(app, raise_server_exceptions=False)


def _host(handler: Handler) -> TestClient:
    return TestClient(
        build_asgi_app(handler, app_name="kv-test", object_store=None),
        raise_server_exceptions=False,
    )


_SURFACES = {"worker": _worker, "host": _host}


def _post_routes(client: TestClient) -> None:
    for route in _ROUTES:
        response = client.post(f"/workflows/v1/{route}", json=_FLAT_BODY)
        assert response.status_code == 200, (route, response.text)


def _start_input(
    handler: Handler, body: dict[str, Any]
) -> tuple[_KerberosStartInput, dict[str, Any]]:
    """POST /start; return the input handed to Temporal and the response body."""

    class _KerberosApp(App):
        async def run(self, input: _KerberosStartInput) -> _KerberosStartOutput:
            return _KerberosStartOutput()

    mock_client = MagicMock()
    mock_client.start_workflow = AsyncMock(
        return_value=MagicMock(id="wf-1", result_run_id="run-1")
    )
    with patch(
        "application_sdk.handler.service._get_temporal_client",
        new=AsyncMock(return_value=mock_client),
    ):
        client = _worker(handler, app_class=_KerberosApp, temporal_host="temporal:7233")
        response = client.post("/workflows/v1/start", json=body)
    assert response.status_code == 200, response.text
    (input_data,) = mock_client.start_workflow.await_args.kwargs["args"]
    return input_data, response.json()


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
def kerberos_rebound(unbound_legacy_globals: None) -> None:
    """Rebind the key set exactly as a connector app does."""
    service._CREDENTIAL_KEYS = _read_legacy("_CREDENTIAL_KEYS") | frozenset(  # type: ignore[attr-defined]
        {"kerberos"}
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
# Handler.extra_credential_keys
# ---------------------------------------------------------------------------


class TestExtraCredentialKeys:
    @pytest.mark.parametrize("surface", sorted(_SURFACES))
    def test_routes_see_the_extra_key_as_a_credential(self, surface: str) -> None:
        handler = _KerberosHandler()
        _post_routes(_SURFACES[surface](handler))
        for route in _ROUTES:
            assert "kerberos" in handler.seen[route], route
            assert "host" in handler.seen[route], route

    @pytest.mark.parametrize("surface", sorted(_SURFACES))
    def test_default_handler_keeps_the_sdk_set(self, surface: str) -> None:
        handler = _CapturingHandler()
        _post_routes(_SURFACES[surface](handler))
        for route in _ROUTES:
            assert "kerberos" not in handler.seen[route], route
            assert "host" in handler.seen[route], route

    def test_sdk_set_is_not_mutated(self) -> None:
        _post_routes(_worker(_KerberosHandler()))
        assert "kerberos" not in contracts._CREDENTIAL_KEYS

    def test_a_plain_set_is_accepted(self) -> None:
        class _SetHandler(_CapturingHandler):
            extra_credential_keys = {"kerberos"}  # type: ignore[assignment]

        handler = _SetHandler()
        _post_routes(_host(handler))
        assert "kerberos" in handler.seen["auth"]

    def test_start_strips_only_the_credentials_key(self) -> None:
        input_data, _ = _start_input(_KerberosHandler(), {**_FLAT_BODY, "name": "x"})
        # /start deletes ``credentials`` and nothing else, so a flat key the
        # input declares still validates into it.
        assert input_data.kerberos == _KERBEROS
        assert "credentials" not in input_data.model_dump()


# ---------------------------------------------------------------------------
# Handler.prepare_request_body
# ---------------------------------------------------------------------------


class TestPrepareRequestBody:
    @pytest.mark.parametrize("surface", sorted(_SURFACES))
    def test_metadata_route_sees_the_prepared_body(self, surface: str) -> None:
        handler = _LiftingHandler()
        response = _SURFACES[surface](handler).post(
            "/workflows/v1/metadata", json=_FLAT_BODY
        )
        assert response.status_code == 200, response.text
        assert handler.metadata_input is not None
        assert handler.metadata_input.connection_config["legacy-key"] == _LIFTED

    @pytest.mark.parametrize("surface", sorted(_SURFACES))
    def test_runs_once_per_request(self, surface: str) -> None:
        handler = _LiftingHandler()
        _post_routes(_SURFACES[surface](handler))
        assert len(handler.prepared) == len(_ROUTES)

    def test_sees_a_v2_credentials_object_before_normalization(self) -> None:
        handler = _LiftingHandler()
        body = {"credentials": {"host": "h", "extra": {"warehouse": "w"}}}
        response = _host(handler).post("/workflows/v1/auth", json=body)
        assert response.status_code == 200, response.text
        assert handler.prepared == [body]
        # ...and the handler still gets the normalized pairs.
        assert set(handler.seen["auth"]) == {"host", "extra.warehouse"}

    def test_start_runs_it(self) -> None:
        handler = _LiftingHandler()
        _start_input(handler, {**_FLAT_BODY, "name": "x"})
        assert len(handler.prepared) == 1

    def test_default_is_identity(self) -> None:
        body = {"a": 1}
        assert DefaultHandler().prepare_request_body(body) is body


_VALIDATE_PACKAGE = "fnd4688_validate_app"


@pytest.fixture
def validate_host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    generated = tmp_path / _VALIDATE_PACKAGE / "generated"
    generated.mkdir(parents=True)
    (tmp_path / _VALIDATE_PACKAGE / "__init__.py").write_text("")
    (generated / "__init__.py").write_text("")
    (generated / "_input.py").write_text(
        textwrap.dedent(
            """
            from application_sdk.templates.contracts import ExtractionInput


            class AppInputContract(ExtractionInput):
                oauth_client_id: str
            """
        )
    )
    (generated / "manifest.json").write_text("{}")
    monkeypatch.syspath_prepend(str(tmp_path))
    try:
        yield generated
    finally:
        for name in [m for m in sys.modules if m.startswith(_VALIDATE_PACKAGE)]:
            del sys.modules[name]


def test_validate_on_the_host_runs_prepare_request_body(validate_host: Path) -> None:
    class _RenamingHandler(DefaultHandler):
        def prepare_request_body(self, body: dict[str, Any]) -> dict[str, Any]:
            if "legacyClientId" in body:
                body = {**body, "oauth_client_id": body["legacyClientId"]}
            return body

    client = TestClient(
        build_asgi_app(
            _RenamingHandler(),
            app_name="validate-host",
            app_package=_VALIDATE_PACKAGE,
            generated_dir=validate_host,
            object_store=None,
        ),
        raise_server_exceptions=False,
    )
    response = client.post("/workflows/v1/validate", json={"legacyClientId": "c-1"})
    assert response.status_code == 200, response.text


# ---------------------------------------------------------------------------
# Deprecated: rebinding service._CREDENTIAL_KEYS / service._normalize_credentials
# ---------------------------------------------------------------------------


class TestDeprecatedServiceGlobals:
    @pytest.mark.parametrize(
        ("name", "replacement"),
        [
            ("_CREDENTIAL_KEYS", "Handler.extra_credential_keys"),
            ("_normalize_credentials", "Handler.prepare_request_body"),
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
        self, kerberos_rebound: None
    ) -> None:
        handler = _CapturingHandler()
        _post_routes(_worker(handler))
        for route in _ROUTES:
            assert "kerberos" in handler.seen[route], route

    def test_key_rebind_after_the_service_is_built_still_applies(
        self, unbound_legacy_globals: None
    ) -> None:
        handler = _CapturingHandler()
        client = _worker(handler)
        service._CREDENTIAL_KEYS = _read_legacy("_CREDENTIAL_KEYS") | frozenset(  # type: ignore[attr-defined]
            {"kerberos"}
        )
        _post_routes(client)
        assert "kerberos" in handler.seen["auth"]

    def test_key_rebind_still_reaches_start(self, kerberos_rebound: None) -> None:
        input_data, _ = _start_input(_CapturingHandler(), {**_FLAT_BODY, "name": "x"})
        assert input_data.kerberos == _KERBEROS

    def test_key_rebind_logs_the_replacement_once(self, kerberos_rebound: None) -> None:
        service._warned_legacy_rebinds.discard("_CREDENTIAL_KEYS")
        with patch.object(service, "logger") as log:
            _post_routes(_worker(_CapturingHandler()))
        warned = [
            call.args
            for call in log.warning.call_args_list
            if "_CREDENTIAL_KEYS" in call.args
        ]
        assert len(warned) == 1
        assert "Handler.extra_credential_keys" in warned[0][-1]

    def test_binding_back_the_value_read_is_not_a_rebind(
        self, unbound_legacy_globals: None
    ) -> None:
        service._CREDENTIAL_KEYS = _read_legacy("_CREDENTIAL_KEYS")  # type: ignore[attr-defined]
        assert service._legacy_rebinds() == {}

    def test_wrapper_still_reaches_every_worker_route_and_start(
        self, normalize_wrapped: list[dict[str, Any]]
    ) -> None:
        handler = _CapturingHandler()
        _post_routes(_worker(handler))
        assert len(normalize_wrapped) == len(_ROUTES)
        assert handler.metadata_input is not None
        assert handler.metadata_input.connection_config["legacy-key"] == _LIFTED
        _start_input(_CapturingHandler(), {**_FLAT_BODY, "name": "x"})
        assert len(normalize_wrapped) == len(_ROUTES) + 1

    def test_wrapper_composes_with_key_rebinding(
        self, kerberos_rebound: None, normalize_wrapped: list[dict[str, Any]]
    ) -> None:
        handler = _CapturingHandler()
        _post_routes(_worker(handler))
        assert "kerberos" in handler.seen["metadata"]
        assert handler.metadata_input is not None
        assert handler.metadata_input.connection_config["legacy-key"] == _LIFTED

    def test_handler_keys_compose_with_key_rebinding(
        self, unbound_legacy_globals: None
    ) -> None:
        service._CREDENTIAL_KEYS = _read_legacy("_CREDENTIAL_KEYS") | frozenset(  # type: ignore[attr-defined]
            {"realm"}
        )
        handler = _KerberosHandler()
        client = _worker(handler)
        response = client.post(
            "/workflows/v1/auth", json={**_FLAT_BODY, "realm": "EXAMPLE.COM"}
        )
        assert response.status_code == 200, response.text
        assert {"kerberos", "realm"} <= set(handler.seen["auth"])

    def test_host_ignores_rebinds(
        self, kerberos_rebound: None, normalize_wrapped: list[dict[str, Any]]
    ) -> None:
        handler = _CapturingHandler()
        _post_routes(_host(handler))
        assert normalize_wrapped == []
        for route in _ROUTES:
            assert "kerberos" not in handler.seen[route], route
