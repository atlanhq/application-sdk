"""Unit tests for application_sdk.execution._temporal.backend.

Targets:
- TemporalExecutorBackend (execute / start / get_result / cancel)
- _build_tls_config (file IO + validation)
- create_temporal_client (TLS branches, connect retry, runtime caching)
- _get_or_create_runtime (singleton behaviour)

These tests exercise the lazy-import paths in this module through real call
paths so a renamed symbol fails the test.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any
from unittest import mock

import pytest

# These tests intentionally import a private Temporal backend module because
# they verify internal client/runtime wiring that is not exposed publicly.
from application_sdk.execution._temporal import backend as backend_module
from application_sdk.execution._temporal._backend_errors import (
    MtlsConfigError,
    TemporalConnectError,
    TlsCertFileNotFoundError,
    UnknownEntryPointError,
)
from application_sdk.execution._temporal.backend import (
    TemporalExecutorBackend,
    _build_tls_config,
    _get_or_create_runtime,
    create_temporal_client,
)
from application_sdk.execution.retry import RetryPolicy as SdkRetryPolicy


@pytest.fixture(autouse=True)
def _reset_module_globals() -> Any:
    """Clear the runtime singletons between tests."""
    backend_module._prometheus_runtime = None
    backend_module._default_runtime = None
    yield
    backend_module._prometheus_runtime = None
    backend_module._default_runtime = None


# ---------------------------------------------------------------------------
# TemporalExecutorBackend.execute / start / get_result / cancel
# ---------------------------------------------------------------------------


def _make_app_cls(
    name: str = "test-app",
    *,
    entry_points: dict[str, Any] | None = None,
    output_type: type | None = None,
) -> Any:
    """Build a stand-in app class with the attributes the backend reads."""

    class _MockApp:
        _app_name = name
        _output_type = output_type
        _app_metadata = mock.MagicMock(entry_points=entry_points or {})

    return _MockApp


def _make_input_data(*, with_config_hash: bool = False) -> Any:
    inp = mock.MagicMock()
    if with_config_hash:
        inp.config_hash = mock.MagicMock(return_value="cfghash")
    else:
        # Remove config_hash so hasattr() returns False.
        if hasattr(inp, "config_hash"):
            del inp.config_hash
    return inp


class TestTemporalExecutorBackendExecute:
    @pytest.mark.asyncio
    async def test_execute_calls_client_execute_workflow(self) -> None:
        client = mock.MagicMock()
        client.execute_workflow = mock.AsyncMock(return_value="OK")
        backend = TemporalExecutorBackend(client=client, task_queue="q")
        app_cls = _make_app_cls()
        ctx = mock.MagicMock(app_name="my-app", correlation_id="corr-1")
        result = await backend.execute(
            app_cls,
            _make_input_data(with_config_hash=False),
            context=ctx,
            retry_policy=SdkRetryPolicy(),
        )
        assert result == "OK"
        client.execute_workflow.assert_awaited_once()
        kwargs = client.execute_workflow.await_args.kwargs
        assert kwargs["task_queue"] == "q"
        assert kwargs["id"].startswith("my-app-")

    @pytest.mark.asyncio
    async def test_execute_uses_config_hash_in_workflow_id_when_available(self) -> None:
        client = mock.MagicMock()
        client.execute_workflow = mock.AsyncMock(return_value=None)
        backend = TemporalExecutorBackend(client=client)
        app_cls = _make_app_cls(name="hashy")
        ctx = mock.MagicMock(app_name="hashy", correlation_id="c1")
        await backend.execute(
            app_cls,
            _make_input_data(with_config_hash=True),
            context=ctx,
            retry_policy=SdkRetryPolicy(),
        )
        wf_id = client.execute_workflow.await_args.kwargs["id"]
        assert wf_id.startswith("hashy-cfghash-")

    @pytest.mark.asyncio
    async def test_execute_with_entry_point_uses_qualified_workflow_name(self) -> None:
        ep_meta = mock.MagicMock(output_type=dict)
        client = mock.MagicMock()
        client.execute_workflow = mock.AsyncMock(return_value=None)
        backend = TemporalExecutorBackend(client=client)
        app_cls = _make_app_cls(name="mep", entry_points={"extract": ep_meta})
        ctx = mock.MagicMock(app_name="mep", correlation_id="x")
        await backend.execute(
            app_cls,
            _make_input_data(),
            context=ctx,
            retry_policy=SdkRetryPolicy(),
            entry_point="extract",
        )
        # First positional arg is the workflow name.
        wf_name = client.execute_workflow.await_args.args[0]
        assert wf_name == "mep:extract"

    @pytest.mark.asyncio
    async def test_execute_with_unknown_entry_point_raises_value_error(self) -> None:
        client = mock.MagicMock()
        client.execute_workflow = mock.AsyncMock()
        backend = TemporalExecutorBackend(client=client)
        app_cls = _make_app_cls(name="noeps", entry_points={"a": mock.MagicMock()})
        ctx = mock.MagicMock(app_name="noeps", correlation_id="c")
        with pytest.raises(UnknownEntryPointError):
            await backend.execute(
                app_cls,
                _make_input_data(),
                context=ctx,
                retry_policy=SdkRetryPolicy(),
                entry_point="missing",
            )

    @pytest.mark.asyncio
    async def test_execute_passes_execution_timeout(self) -> None:
        client = mock.MagicMock()
        client.execute_workflow = mock.AsyncMock(return_value=None)
        backend = TemporalExecutorBackend(client=client)
        app_cls = _make_app_cls()
        ctx = mock.MagicMock(app_name="my-app", correlation_id="x")
        await backend.execute(
            app_cls,
            _make_input_data(),
            context=ctx,
            retry_policy=SdkRetryPolicy(),
            execution_timeout=timedelta(minutes=5),
        )
        assert client.execute_workflow.await_args.kwargs[
            "execution_timeout"
        ] == timedelta(minutes=5)


class TestTemporalExecutorBackendStart:
    @pytest.mark.asyncio
    async def test_start_returns_workflow_id(self) -> None:
        handle = mock.MagicMock()
        handle.id = "wf-id-xyz"
        client = mock.MagicMock()
        client.start_workflow = mock.AsyncMock(return_value=handle)
        backend = TemporalExecutorBackend(client=client)
        app_cls = _make_app_cls(name="starter")
        ctx = mock.MagicMock(app_name="starter", correlation_id="c")
        wf_id = await backend.start(
            app_cls,
            _make_input_data(),
            context=ctx,
            retry_policy=SdkRetryPolicy(),
        )
        assert wf_id == "wf-id-xyz"

    @pytest.mark.asyncio
    async def test_start_uses_entry_point_when_provided(self) -> None:
        handle = mock.MagicMock(id="abc")
        client = mock.MagicMock()
        client.start_workflow = mock.AsyncMock(return_value=handle)
        backend = TemporalExecutorBackend(client=client)
        app_cls = _make_app_cls(name="ep", entry_points={"do": mock.MagicMock()})
        ctx = mock.MagicMock(app_name="ep", correlation_id="c")
        await backend.start(
            app_cls,
            _make_input_data(),
            context=ctx,
            retry_policy=SdkRetryPolicy(),
            entry_point="do",
        )
        assert client.start_workflow.await_args.args[0] == "ep:do"


class TestTemporalExecutorBackendHandleOps:
    @pytest.mark.asyncio
    async def test_get_result_awaits_handle_result(self) -> None:
        handle = mock.MagicMock()
        handle.result = mock.AsyncMock(return_value=42)
        client = mock.MagicMock()
        client.get_workflow_handle = mock.MagicMock(return_value=handle)
        backend = TemporalExecutorBackend(client=client)
        result = await backend.get_result("wf-1")
        assert result == 42
        client.get_workflow_handle.assert_called_once_with("wf-1")

    @pytest.mark.asyncio
    async def test_cancel_awaits_handle_cancel(self) -> None:
        handle = mock.MagicMock()
        handle.cancel = mock.AsyncMock(return_value=None)
        client = mock.MagicMock()
        client.get_workflow_handle = mock.MagicMock(return_value=handle)
        backend = TemporalExecutorBackend(client=client)
        await backend.cancel("wf-1")
        handle.cancel.assert_awaited_once()


# ---------------------------------------------------------------------------
# _build_tls_config
# ---------------------------------------------------------------------------


class TestBuildTlsConfig:
    """Exercises the inline import of temporalio.service.TLSConfig."""

    def test_returns_default_when_no_paths(self) -> None:
        cfg = _build_tls_config()
        # TLSConfig is a temporal-side dataclass; we just sanity-check shape.
        assert cfg.server_root_ca_cert is None
        assert cfg.client_cert is None
        assert cfg.client_private_key is None

    def test_loads_root_ca_cert(self, tmp_path: Any) -> None:
        cert_path = tmp_path / "ca.pem"
        cert_path.write_bytes(b"-----CA-----")
        cfg = _build_tls_config(server_root_ca_cert_path=str(cert_path))
        assert cfg.server_root_ca_cert == b"-----CA-----"

    def test_raises_when_root_ca_missing(self, tmp_path: Any) -> None:
        with pytest.raises(TlsCertFileNotFoundError):
            _build_tls_config(server_root_ca_cert_path=str(tmp_path / "missing.pem"))

    def test_raises_when_client_cert_without_key(self, tmp_path: Any) -> None:
        with pytest.raises(MtlsConfigError):
            _build_tls_config(client_cert_path=str(tmp_path / "client.pem"))

    def test_raises_when_client_key_without_cert(self, tmp_path: Any) -> None:
        with pytest.raises(MtlsConfigError):
            _build_tls_config(client_private_key_path=str(tmp_path / "key.pem"))

    def test_raises_when_client_cert_missing(self, tmp_path: Any) -> None:
        key = tmp_path / "k.pem"
        key.write_bytes(b"key")
        with pytest.raises(TlsCertFileNotFoundError):
            _build_tls_config(
                client_cert_path=str(tmp_path / "missing-cert.pem"),
                client_private_key_path=str(key),
            )

    def test_raises_when_client_key_missing(self, tmp_path: Any) -> None:
        cert = tmp_path / "c.pem"
        cert.write_bytes(b"cert")
        with pytest.raises(TlsCertFileNotFoundError):
            _build_tls_config(
                client_cert_path=str(cert),
                client_private_key_path=str(tmp_path / "missing-key.pem"),
            )

    def test_loads_mtls_pair_with_domain(self, tmp_path: Any) -> None:
        cert = tmp_path / "c.pem"
        key = tmp_path / "k.pem"
        cert.write_bytes(b"cert-bytes")
        key.write_bytes(b"key-bytes")
        cfg = _build_tls_config(
            client_cert_path=str(cert),
            client_private_key_path=str(key),
            domain="example.com",
        )
        assert cfg.client_cert == b"cert-bytes"
        assert cfg.client_private_key == b"key-bytes"
        assert cfg.domain == "example.com"


# ---------------------------------------------------------------------------
# _get_or_create_runtime
# ---------------------------------------------------------------------------


class TestGetOrCreateRuntime:
    def test_with_prometheus_caches_singleton(self) -> None:
        with mock.patch.object(backend_module, "Runtime") as Runtime:
            Runtime.side_effect = lambda **kwargs: mock.MagicMock(name="rt")
            r1 = _get_or_create_runtime(
                enable_prometheus=True, prometheus_bind_address="0.0.0.0:9999"
            )
            r2 = _get_or_create_runtime(
                enable_prometheus=True, prometheus_bind_address="0.0.0.0:9999"
            )
        assert r1 is r2
        # Constructed exactly once.
        assert Runtime.call_count == 1

    def test_without_prometheus_uses_default_runtime(self) -> None:
        sentinel = mock.MagicMock(name="default-runtime")
        with mock.patch.object(backend_module, "Runtime") as Runtime:
            Runtime.default = mock.MagicMock(return_value=sentinel)
            r1 = _get_or_create_runtime(enable_prometheus=False)
            r2 = _get_or_create_runtime(enable_prometheus=False)
        assert r1 is sentinel
        assert r2 is sentinel
        assert Runtime.default.call_count == 1


# ---------------------------------------------------------------------------
# create_temporal_client
# ---------------------------------------------------------------------------


class TestCreateTemporalClient:
    @pytest.mark.asyncio
    async def test_plaintext_connect_happy_path(self) -> None:
        fake_client = mock.MagicMock(name="client")
        with (
            mock.patch.object(backend_module, "_get_or_create_runtime") as runtime,
            mock.patch.object(
                backend_module.Client,
                "connect",
                new=mock.AsyncMock(return_value=fake_client),
            ) as connect,
        ):
            runtime.return_value = mock.MagicMock(name="rt")
            client = await create_temporal_client(
                host="h:1", namespace="ns", connect_max_attempts=1
            )
        assert client is fake_client
        kwargs = connect.await_args.kwargs
        assert kwargs["target_host"] == "h:1"
        assert kwargs["namespace"] == "ns"
        assert kwargs["tls"] is False
        assert "api_key" not in kwargs

    @pytest.mark.asyncio
    async def test_api_key_passed_through(self) -> None:
        with (
            mock.patch.object(backend_module, "_get_or_create_runtime"),
            mock.patch.object(
                backend_module.Client,
                "connect",
                new=mock.AsyncMock(return_value=mock.MagicMock()),
            ) as connect,
        ):
            await create_temporal_client(api_key="secret-token", connect_max_attempts=1)
        assert connect.await_args.kwargs["api_key"] == "secret-token"

    @pytest.mark.asyncio
    async def test_tls_with_no_paths_uses_custom_ca_when_available(self) -> None:
        """Exercises inline imports of clients.ssl_utils and temporalio.service."""
        with (
            mock.patch.object(backend_module, "_get_or_create_runtime"),
            mock.patch(
                "application_sdk.clients.ssl_utils.get_custom_ca_cert_bytes",
                return_value=b"-----CA-BYTES-----",
            ),
            mock.patch.object(
                backend_module.Client,
                "connect",
                new=mock.AsyncMock(return_value=mock.MagicMock()),
            ) as connect,
        ):
            await create_temporal_client(tls_enabled=True, connect_max_attempts=1)
        tls_arg = connect.await_args.kwargs["tls"]
        # When custom CA bytes are present, build a TLSConfig — not a bare True.
        assert tls_arg is not True
        assert tls_arg is not False

    @pytest.mark.asyncio
    async def test_tls_with_no_paths_and_no_custom_ca_uses_true(self) -> None:
        with (
            mock.patch.object(backend_module, "_get_or_create_runtime"),
            mock.patch(
                "application_sdk.clients.ssl_utils.get_custom_ca_cert_bytes",
                return_value=None,
            ),
            mock.patch.object(
                backend_module.Client,
                "connect",
                new=mock.AsyncMock(return_value=mock.MagicMock()),
            ) as connect,
        ):
            await create_temporal_client(tls_enabled=True, connect_max_attempts=1)
        assert connect.await_args.kwargs["tls"] is True

    @pytest.mark.asyncio
    async def test_tls_with_explicit_cert_path_calls_build_tls_config(
        self, tmp_path: Any
    ) -> None:
        ca = tmp_path / "ca.pem"
        ca.write_bytes(b"ca")
        with (
            mock.patch.object(backend_module, "_get_or_create_runtime"),
            mock.patch.object(
                backend_module.Client,
                "connect",
                new=mock.AsyncMock(return_value=mock.MagicMock()),
            ) as connect,
        ):
            await create_temporal_client(
                tls_enabled=True,
                tls_server_root_ca_cert_path=str(ca),
                connect_max_attempts=1,
            )
        tls_arg = connect.await_args.kwargs["tls"]
        # Should be a TLSConfig instance (not bool)
        assert tls_arg is not True
        assert tls_arg is not False

    @pytest.mark.asyncio
    async def test_retries_on_connect_failure(self) -> None:
        """create_temporal_client retries up to connect_max_attempts."""
        fake_client = mock.MagicMock(name="client")
        connect_mock = mock.AsyncMock(
            side_effect=[
                ConnectionError("fail1"),
                ConnectionError("fail2"),
                fake_client,
            ]
        )
        with (
            mock.patch.object(backend_module, "_get_or_create_runtime"),
            mock.patch.object(backend_module.Client, "connect", new=connect_mock),
            mock.patch.object(
                backend_module.asyncio, "sleep", new=mock.AsyncMock()
            ) as sleep_mock,
        ):
            client = await create_temporal_client(
                connect_max_attempts=3,
                connect_retry_delay_seconds=0.0,
            )
        assert client is fake_client
        assert connect_mock.await_count == 3
        # Two retries → two sleeps.
        assert sleep_mock.await_count == 2

    @pytest.mark.asyncio
    async def test_raises_after_max_attempts(self) -> None:
        connect_mock = mock.AsyncMock(side_effect=ConnectionError("nope"))
        with (
            mock.patch.object(backend_module, "_get_or_create_runtime"),
            mock.patch.object(backend_module.Client, "connect", new=connect_mock),
            mock.patch.object(backend_module.asyncio, "sleep", new=mock.AsyncMock()),
        ):
            with pytest.raises(TemporalConnectError):
                await create_temporal_client(
                    connect_max_attempts=2,
                    connect_retry_delay_seconds=0.0,
                )
        assert connect_mock.await_count == 2
