"""The service module's credential-normalization globals stay extension points.

Connector apps patch them at import time, either extending the worker's v2
flat-credential key set::

    import application_sdk.handler.service as _sdk_handler_svc
    _sdk_handler_svc._CREDENTIAL_KEYS = _sdk_handler_svc._CREDENTIAL_KEYS | frozenset({"kerberos"})

or wrapping the normalization itself::

    _original = _sdk_handler_svc._normalize_credentials
    _sdk_handler_svc._normalize_credentials = lambda body: _lift(_original(body))

Normalization moved to ``handler.contracts`` and the auth / check / metadata
routes to ``handler.routes``; these tests pin that the rebinding still reaches
every worker-side normalization (``/start`` and the shared routes) while the
API host keeps the contracts default set.
"""

from __future__ import annotations

import warnings
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

import application_sdk.handler.contracts as contracts
import application_sdk.handler.service as service
from application_sdk.app.base import App
from application_sdk.app.registry import AppRegistry, TaskRegistry
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

_KERBEROS = {"principal": "svc@EXAMPLE.COM", "keytab": "a2V5dGFi"}
_FLAT_BODY: dict[str, Any] = {
    "host": "db.example.com",
    "port": 10000,
    "username": "svc",
    "kerberos": _KERBEROS,
}


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
class _KerberosStartInput(Input, allow_unbounded_fields=True):  # type: ignore[call-arg]
    name: str = ""
    kerberos: dict[str, str] | None = None


class _KerberosStartOutput(Output, allow_unbounded_fields=True):  # type: ignore[call-arg]
    pass


@pytest.fixture
def kerberos_rebound() -> Iterator[None]:
    """Rebind the service attribute exactly as a connector app does; restore after."""
    original = service._CREDENTIAL_KEYS
    service._CREDENTIAL_KEYS = service._CREDENTIAL_KEYS | frozenset({"kerberos"})
    try:
        yield
    finally:
        service._CREDENTIAL_KEYS = original


@pytest.fixture
def clean_registry() -> Iterator[None]:
    AppRegistry.reset()
    TaskRegistry.reset()
    try:
        yield
    finally:
        AppRegistry.reset()
        TaskRegistry.reset()


def _worker(handler: Handler, **kwargs: Any) -> TestClient:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        app = service.create_app_handler_service(handler, app_name="kv-test", **kwargs)
    return TestClient(app, raise_server_exceptions=False)


def _post_routes(client: TestClient) -> None:
    for route in ("auth", "check", "metadata"):
        response = client.post(f"/workflows/v1/{route}", json=_FLAT_BODY)
        assert response.status_code == 200, (route, response.text)


def _start_input(kerberos_body: dict[str, Any]) -> _KerberosStartInput:
    """POST /start and return the input object handed to Temporal."""

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
        client = _worker(
            _CapturingHandler(), app_class=_KerberosApp, temporal_host="temporal:7233"
        )
        response = client.post("/workflows/v1/start", json=kerberos_body)
    assert response.status_code == 200, response.text
    (input_data,) = mock_client.start_workflow.await_args.kwargs["args"]
    return input_data


class TestServiceAttribute:
    def test_exists_with_contracts_value(self) -> None:
        assert service._CREDENTIAL_KEYS == contracts._CREDENTIAL_KEYS

    def test_rebinding_line_runs(self, kerberos_rebound: None) -> None:
        assert "kerberos" in service._CREDENTIAL_KEYS
        # Contracts' own set is untouched -- only the worker's view changes.
        assert "kerberos" not in contracts._CREDENTIAL_KEYS


class TestRebindingReachesWorkerNormalization:
    def test_normalize_credentials_copies_kerberos_into_credentials(
        self, kerberos_rebound: None
    ) -> None:
        body = service._normalize_credentials({**_FLAT_BODY, "name": "x"})
        assert body["name"] == "x"
        assert "kerberos" in {p["key"] for p in body["credentials"]}
        # As on main: only ``credentials`` is added; the flat keys stay.
        assert body["kerberos"] == _KERBEROS

    def test_preflight_normalization_copies_kerberos(
        self, kerberos_rebound: None
    ) -> None:
        body = service._normalize_preflight_request(dict(_FLAT_BODY))
        assert "kerberos" in {p["key"] for p in body["credentials"]}

    def test_auth_check_metadata_routes_see_kerberos_as_credential(
        self, kerberos_rebound: None
    ) -> None:
        handler = _CapturingHandler()
        _post_routes(_worker(handler))
        for route in ("auth", "check", "metadata"):
            assert "kerberos" in handler.seen[route], route
            assert "host" in handler.seen[route], route

    def test_rebinding_after_app_built_still_applies(self) -> None:
        """Read per request, not captured at registration -- as on main."""
        handler = _CapturingHandler()
        client = _worker(handler)
        original = service._CREDENTIAL_KEYS
        service._CREDENTIAL_KEYS = service._CREDENTIAL_KEYS | frozenset({"kerberos"})
        try:
            _post_routes(client)
        finally:
            service._CREDENTIAL_KEYS = original
        assert "kerberos" in handler.seen["auth"]

    def test_start_strips_only_the_credentials_key(
        self, kerberos_rebound: None, clean_registry: None
    ) -> None:
        input_data = _start_input({**_FLAT_BODY, "name": "x"})
        # As on main: /start deletes ``credentials`` and nothing else, so a
        # flat key the input declares still validates into it.
        assert input_data.kerberos == _KERBEROS
        assert "credentials" not in input_data.model_dump()


class TestDefaultSetWithoutRebinding:
    def test_normalize_credentials_leaves_kerberos_top_level(self) -> None:
        body = service._normalize_credentials(dict(_FLAT_BODY))
        assert body == contracts.normalize_credentials(dict(_FLAT_BODY))
        assert body["kerberos"] == _KERBEROS
        assert "kerberos" not in {p["key"] for p in body["credentials"]}
        assert body["host"] == _FLAT_BODY["host"]

    def test_routes_use_default_set(self) -> None:
        handler = _CapturingHandler()
        _post_routes(_worker(handler))
        for route in ("auth", "check", "metadata"):
            assert "kerberos" not in handler.seen[route], route
            assert "host" in handler.seen[route], route

    def test_start_keeps_kerberos_as_a_plain_field(self, clean_registry: None) -> None:
        input_data = _start_input({**_FLAT_BODY, "name": "x"})
        assert input_data.kerberos == _KERBEROS
        assert "host" not in input_data.model_dump()


_LIFTED = "lifted-by-app"


@pytest.fixture
def normalize_wrapped() -> Iterator[list[dict[str, Any]]]:
    """Wrap the service normalization as a connector app does; restore after.

    The wrapper adds a ``connection_config`` key to every normalized body and
    records each body it saw.
    """
    calls: list[dict[str, Any]] = []
    original = service._normalize_credentials

    def _wrapper(body: dict[str, Any]) -> dict[str, Any]:
        calls.append(dict(body))
        out = original(body)
        cfg = dict(out.get("connection_config") or {})
        cfg["legacy-key"] = _LIFTED
        return {**out, "connection_config": cfg}

    service._normalize_credentials = _wrapper  # type: ignore[assignment]
    try:
        yield calls
    finally:
        service._normalize_credentials = original  # type: ignore[assignment]


class TestWrappedNormalizationReachesWorker:
    def test_metadata_route_sees_wrapper_output(
        self, normalize_wrapped: list[dict[str, Any]]
    ) -> None:
        handler = _CapturingHandler()
        response = _worker(handler).post("/workflows/v1/metadata", json=_FLAT_BODY)
        assert response.status_code == 200, response.text
        assert handler.metadata_input is not None
        assert handler.metadata_input.connection_config["legacy-key"] == _LIFTED
        assert normalize_wrapped, "wrapper was not called"

    def test_every_route_and_start_call_the_wrapper(
        self, normalize_wrapped: list[dict[str, Any]], clean_registry: None
    ) -> None:
        _post_routes(_worker(_CapturingHandler()))
        assert len(normalize_wrapped) == 3  # auth, check, metadata
        _start_input({**_FLAT_BODY, "name": "x"})
        assert len(normalize_wrapped) == 4

    def test_wrapper_composes_with_key_rebinding(
        self, kerberos_rebound: None, normalize_wrapped: list[dict[str, Any]]
    ) -> None:
        handler = _CapturingHandler()
        _post_routes(_worker(handler))
        assert "kerberos" in handler.seen["metadata"]
        assert handler.metadata_input is not None
        assert handler.metadata_input.connection_config["legacy-key"] == _LIFTED

    def test_host_ignores_service_wrapper(
        self, normalize_wrapped: list[dict[str, Any]]
    ) -> None:
        handler = _CapturingHandler()
        client = TestClient(
            build_asgi_app(handler, app_name="kv-test"),
            raise_server_exceptions=False,
        )
        _post_routes(client)
        assert normalize_wrapped == []
        assert handler.metadata_input is not None
        assert "legacy-key" not in (handler.metadata_input.connection_config or {})


class TestApiHostKeepsContractsDefault:
    def test_host_ignores_service_rebinding(self, kerberos_rebound: None) -> None:
        handler = _CapturingHandler()
        client = TestClient(
            build_asgi_app(handler, app_name="kv-test"),
            raise_server_exceptions=False,
        )
        _post_routes(client)
        for route in ("auth", "check", "metadata"):
            assert "kerberos" not in handler.seen[route], route
