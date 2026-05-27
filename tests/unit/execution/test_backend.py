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

import socket as _socket
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
    async def test_tls_with_no_paths_and_no_custom_ca_enables_tls(self) -> None:
        """tls=True is the historical no-config TLS shape; the v4-preferring
        pre-resolver may upgrade it to TLSConfig(domain=<original>) to
        preserve SNI when it substitutes an IP for the hostname, so the
        assertion is permissive about the exact representation."""
        from temporalio.service import TLSConfig

        with (
            mock.patch.object(backend_module, "_get_or_create_runtime"),
            mock.patch(
                "application_sdk.clients.ssl_utils.get_custom_ca_cert_bytes",
                return_value=None,
            ),
            # Force the no-op resolver path so the assertion isn't sensitive
            # to whether the test host actually has working IPv6.
            mock.patch.object(
                backend_module,
                "_prefer_v4_target",
                side_effect=lambda h: (h, None),
            ),
            mock.patch.object(
                backend_module.Client,
                "connect",
                new=mock.AsyncMock(return_value=mock.MagicMock()),
            ) as connect,
        ):
            await create_temporal_client(tls_enabled=True, connect_max_attempts=1)
        tls_arg = connect.await_args.kwargs["tls"]
        assert tls_arg is True or isinstance(tls_arg, TLSConfig)

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


# ---------------------------------------------------------------------------
# _prefer_v4_target / _apply_sni_domain (IPv6-fallback safety net)
#
# These guard against a v3-vs-v2 regression seen on two SDR customer VMs:
# both hosts had half-broken IPv6 (link-local only, no global v6 route).
# v2 SDK's pure-Python client iterated every getaddrinfo result and
# silently fell back to v4; the v3 Rust bridge picks one address from
# the resolver and fails the whole connect if that address is v6 on
# such a host. Pre-resolving in Python lets us prefer v4 when both
# families are returned, while preserving TLS SNI via TLSConfig.domain.
# ---------------------------------------------------------------------------


def _fake_addrinfo(*families: int) -> list[tuple[int, int, int, str, tuple]]:
    """Build a getaddrinfo-shaped result with the given AF_INET / AF_INET6 entries."""
    out: list[tuple[int, int, int, str, tuple]] = []
    for fam in families:
        if fam == _socket.AF_INET:
            out.append((fam, _socket.SOCK_STREAM, 6, "", ("203.0.113.10", 443)))
        else:
            out.append(
                (
                    fam,
                    _socket.SOCK_STREAM,
                    6,
                    "",
                    ("2001:db8::abcd", 443, 0, 0),
                )
            )
    return out


class TestPreferV4Target:
    # The helper now uses ``loop.getaddrinfo`` (which delegates to a thread
    # executor) rather than the blocking ``socket.getaddrinfo``, so the
    # tests are async. ``mock.patch.object(backend_module.socket,
    # "getaddrinfo", ...)`` still works because asyncio's
    # ``loop.getaddrinfo`` resolves ``socket.getaddrinfo`` from the global
    # ``socket`` module attribute at call time — the patch is on that same
    # module attribute so the thread executor picks it up.

    @pytest.mark.asyncio
    async def test_returns_v4_ip_when_both_families_present(self) -> None:
        with mock.patch.object(
            backend_module.socket,
            "getaddrinfo",
            return_value=_fake_addrinfo(_socket.AF_INET6, _socket.AF_INET),
        ):
            target, sni = await backend_module._prefer_v4_target(
                "temporal.example.com:443"
            )
        assert target == "203.0.113.10:443"
        assert sni == "temporal.example.com"

    @pytest.mark.asyncio
    async def test_returns_v6_bracketed_when_no_v4_available(self) -> None:
        # Hosts genuinely v6-only (some k8s clusters) — pass v6 through
        # so the connect can still succeed via the only family available.
        # NB: this is also the no-swap-no-SNI path being unreachable here
        # because has_v6 is True and v4 is absent — we explicitly fall
        # through to "pick v6" because there's no v4 to prefer.
        with mock.patch.object(
            backend_module.socket,
            "getaddrinfo",
            return_value=_fake_addrinfo(_socket.AF_INET6),
        ):
            target, sni = await backend_module._prefer_v4_target(
                "temporal.example.com:443"
            )
        assert target == "[2001:db8::abcd]:443"
        assert sni == "temporal.example.com"

    @pytest.mark.asyncio
    async def test_swaps_when_only_v4_returned(self) -> None:
        # Even when Python's AI_ADDRCONFIG-filtered result has no AAAA
        # (e.g. inside a Docker bridge netns with no v6 stack), the
        # Rust connector — which doesn't use AI_ADDRCONFIG — can still
        # get AAAA back from DNS and try it, failing with
        # EADDRNOTAVAIL. So we always substitute the literal v4 IP
        # when one is available; that's the only way to keep the Rust
        # client off v6 in that case.
        with mock.patch.object(
            backend_module.socket,
            "getaddrinfo",
            return_value=_fake_addrinfo(_socket.AF_INET),
        ):
            target, sni = await backend_module._prefer_v4_target(
                "temporal.example.com:443"
            )
        assert target == "203.0.113.10:443"
        assert sni == "temporal.example.com"

    @pytest.mark.asyncio
    async def test_noop_on_literal_ipv4(self) -> None:
        target, sni = await backend_module._prefer_v4_target("203.0.113.10:443")
        assert target == "203.0.113.10:443"
        assert sni is None

    @pytest.mark.asyncio
    async def test_noop_on_literal_ipv6_bracketed(self) -> None:
        target, sni = await backend_module._prefer_v4_target("[::1]:7233")
        assert target == "[::1]:7233"
        assert sni is None

    @pytest.mark.asyncio
    async def test_noop_on_gaierror(self) -> None:
        with mock.patch.object(
            backend_module.socket,
            "getaddrinfo",
            side_effect=_socket.gaierror("name or service not known"),
        ):
            target, sni = await backend_module._prefer_v4_target("nx.example.com:443")
        assert target == "nx.example.com:443"
        assert sni is None

    @pytest.mark.asyncio
    async def test_noop_on_malformed_target(self) -> None:
        # No port, port is non-numeric, or empty host — caller's input
        # contract violation, let the real connect raise rather than
        # masking it with a parse error here.
        for bad in ("just-a-host", ":443", "host:notaport", ""):
            target, sni = await backend_module._prefer_v4_target(bad)
            assert target == bad
            assert sni is None


class TestApplySniDomain:
    def test_returns_false_unchanged(self) -> None:
        # Plaintext connections don't have a TLS handshake to validate,
        # so SNI is irrelevant — propagate False through unchanged.
        assert backend_module._apply_sni_domain(False, "temporal.example.com") is False

    def test_upgrades_true_to_tlsconfig_with_domain(self) -> None:
        from temporalio.service import TLSConfig

        result = backend_module._apply_sni_domain(True, "temporal.example.com")
        assert isinstance(result, TLSConfig)
        assert result.domain == "temporal.example.com"

    def test_preserves_existing_tlsconfig_fields_and_sets_domain(self) -> None:
        from temporalio.service import TLSConfig

        existing = TLSConfig(server_root_ca_cert=b"--CA--")
        result = backend_module._apply_sni_domain(existing, "temporal.example.com")
        assert isinstance(result, TLSConfig)
        assert result.server_root_ca_cert == b"--CA--"
        assert result.domain == "temporal.example.com"

    def test_respects_caller_supplied_domain(self) -> None:
        # When the caller passed --tls-domain (or otherwise built a
        # TLSConfig with an explicit SNI), we must not silently
        # overwrite their choice with the auto-derived hostname.
        from temporalio.service import TLSConfig

        existing = TLSConfig(domain="explicit.override.example")
        result = backend_module._apply_sni_domain(existing, "temporal.example.com")
        assert result is existing


class TestCreateTemporalClientPrefersV4:
    # All three end-to-end tests below explicitly patch
    # ``backend_module.ENABLE_ATLAN_UPLOAD`` to True because the
    # resolver substitution is now gated on SDR mode (the env-driven
    # flag). Internal-Atlan-service flows leave it False and keep the
    # hostname intact for re-resolution on reconnect — covered by the
    # ``TestCreateTemporalClientNonSdrMode`` class below.

    @pytest.mark.asyncio
    async def test_substitutes_v4_ip_and_preserves_sni_for_tls(self) -> None:
        """End-to-end: when the resolver returns both v4 and v6 and TLS
        is enabled, Client.connect receives the v4 IP as target_host and
        a TLSConfig whose domain matches the original hostname."""
        from temporalio.service import TLSConfig

        with (
            mock.patch.object(backend_module, "_get_or_create_runtime"),
            mock.patch.object(backend_module, "ENABLE_ATLAN_UPLOAD", True),
            mock.patch.object(
                backend_module.socket,
                "getaddrinfo",
                return_value=_fake_addrinfo(_socket.AF_INET6, _socket.AF_INET),
            ),
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
            await create_temporal_client(
                host="temporal.example.com:443",
                tls_enabled=True,
                connect_max_attempts=1,
            )
        kwargs = connect.await_args.kwargs
        assert kwargs["target_host"] == "203.0.113.10:443"
        assert isinstance(kwargs["tls"], TLSConfig)
        assert kwargs["tls"].domain == "temporal.example.com"

    @pytest.mark.asyncio
    async def test_swaps_even_when_resolver_returns_only_v4(self) -> None:
        # Plaintext path: Python's AI_ADDRCONFIG-filtered result has
        # only A, but the substitution still happens — the Rust
        # connector doesn't apply AI_ADDRCONFIG and may still query
        # DNS for AAAA on its own. Substituting a literal v4 IP from
        # the start is the only thing that consistently prevents v6
        # attempts on netns-with-no-v6 hosts.
        with (
            mock.patch.object(backend_module, "_get_or_create_runtime"),
            mock.patch.object(backend_module, "ENABLE_ATLAN_UPLOAD", True),
            mock.patch.object(
                backend_module.socket,
                "getaddrinfo",
                return_value=_fake_addrinfo(_socket.AF_INET),
            ),
            mock.patch.object(
                backend_module.Client,
                "connect",
                new=mock.AsyncMock(return_value=mock.MagicMock()),
            ) as connect,
        ):
            await create_temporal_client(
                host="temporal.example.com:443",
                connect_max_attempts=1,
            )
        kwargs = connect.await_args.kwargs
        assert kwargs["target_host"] == "203.0.113.10:443"
        # Plaintext — no TLS to upgrade for SNI; tls stays False.
        assert kwargs["tls"] is False

    @pytest.mark.asyncio
    async def test_plaintext_target_swap_does_not_touch_tls(self) -> None:
        # Plaintext connect with both v4 and v6 — target swaps, but tls
        # stays False because there's no handshake to preserve SNI for.
        with (
            mock.patch.object(backend_module, "_get_or_create_runtime"),
            mock.patch.object(backend_module, "ENABLE_ATLAN_UPLOAD", True),
            mock.patch.object(
                backend_module.socket,
                "getaddrinfo",
                return_value=_fake_addrinfo(_socket.AF_INET6, _socket.AF_INET),
            ),
            mock.patch.object(
                backend_module.Client,
                "connect",
                new=mock.AsyncMock(return_value=mock.MagicMock()),
            ) as connect,
        ):
            await create_temporal_client(
                host="temporal.example.com:443",
                tls_enabled=False,
                connect_max_attempts=1,
            )
        kwargs = connect.await_args.kwargs
        assert kwargs["target_host"] == "203.0.113.10:443"
        assert kwargs["tls"] is False


class TestCreateTemporalClientNonSdrMode:
    # Internal Atlan services connect to Temporal via in-cluster Service
    # DNS (e.g. ``temporal-frontend.temporal.svc.cluster.local``). The
    # ClusterIP can change if the Service object is recreated (Helm
    # reapply, namespace rebuild), and the Rust bridge holds a literal-IP
    # target across reconnects — so pinning the IP at connect time would
    # cause stale-IP connect failures later. These tests pin
    # ``ENABLE_ATLAN_UPLOAD`` to False and confirm the SDK keeps the
    # hostname intact so the bridge re-resolves DNS on each reconnect.

    @pytest.mark.asyncio
    async def test_hostname_preserved_when_not_sdr_dual_stack(self) -> None:
        with (
            mock.patch.object(backend_module, "_get_or_create_runtime"),
            mock.patch.object(backend_module, "ENABLE_ATLAN_UPLOAD", False),
            # Resolver still returns both families — this should NOT
            # trigger any swap because we're not in SDR mode.
            mock.patch.object(
                backend_module.socket,
                "getaddrinfo",
                return_value=_fake_addrinfo(_socket.AF_INET6, _socket.AF_INET),
            ),
            mock.patch.object(
                backend_module.Client,
                "connect",
                new=mock.AsyncMock(return_value=mock.MagicMock()),
            ) as connect,
        ):
            await create_temporal_client(
                host="temporal-frontend.temporal.svc.cluster.local:7233",
                connect_max_attempts=1,
            )
        kwargs = connect.await_args.kwargs
        # Hostname preserved end-to-end so the Rust bridge re-resolves
        # on every reconnect.
        assert (
            kwargs["target_host"] == "temporal-frontend.temporal.svc.cluster.local:7233"
        )
        assert kwargs["tls"] is False

    @pytest.mark.asyncio
    async def test_tls_unchanged_when_not_sdr(self) -> None:
        """When not in SDR mode, ``tls=True`` is NOT upgraded to a
        TLSConfig with a SNI domain — because we never substituted an
        IP, the original hostname continues to drive SNI on each
        Rust-bridge reconnect."""
        with (
            mock.patch.object(backend_module, "_get_or_create_runtime"),
            mock.patch.object(backend_module, "ENABLE_ATLAN_UPLOAD", False),
            mock.patch.object(
                backend_module.socket,
                "getaddrinfo",
                return_value=_fake_addrinfo(_socket.AF_INET6, _socket.AF_INET),
            ),
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
            await create_temporal_client(
                host="temporal-frontend.temporal.svc.cluster.local:7233",
                tls_enabled=True,
                connect_max_attempts=1,
            )
        kwargs = connect.await_args.kwargs
        assert (
            kwargs["target_host"] == "temporal-frontend.temporal.svc.cluster.local:7233"
        )
        # ``tls=True`` stays as-is — no TLSConfig upgrade because we
        # didn't substitute an IP, so SNI naturally tracks the hostname.
        assert kwargs["tls"] is True

    @pytest.mark.asyncio
    async def test_resolver_helper_is_not_called_when_not_sdr(self) -> None:
        # Direct evidence the resolver short-circuits before
        # ``getaddrinfo`` runs at all when we're not in SDR mode. Catches
        # accidental regressions where someone moves the gate the wrong
        # side of the call.
        getaddrinfo_mock = mock.MagicMock(
            return_value=_fake_addrinfo(_socket.AF_INET6, _socket.AF_INET)
        )
        with (
            mock.patch.object(backend_module, "_get_or_create_runtime"),
            mock.patch.object(backend_module, "ENABLE_ATLAN_UPLOAD", False),
            mock.patch.object(
                backend_module.socket, "getaddrinfo", new=getaddrinfo_mock
            ),
            mock.patch.object(
                backend_module.Client,
                "connect",
                new=mock.AsyncMock(return_value=mock.MagicMock()),
            ),
        ):
            await create_temporal_client(
                host="internal-temporal.svc.cluster.local:7233",
                connect_max_attempts=1,
            )
        assert getaddrinfo_mock.call_count == 0
