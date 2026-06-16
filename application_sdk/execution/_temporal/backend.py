"""Temporal client creation and executor backend."""

from __future__ import annotations

import asyncio
import ipaddress
import random
import socket
import threading
import urllib.request
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from temporalio.client import Client
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.runtime import PrometheusConfig, Runtime, TelemetryConfig

from application_sdk.constants import (
    ENABLE_ATLAN_UPLOAD,
    TEMPORAL_PROMETHEUS_BIND_ADDRESS,
)
from application_sdk.execution.retry import RetryPolicy, _to_temporal_retry_policy
from application_sdk.observability.logger_adaptor import get_logger
from application_sdk.observability.utils import get_metric_enrichment_labels

logger = get_logger(__name__)

_prometheus_runtime: Runtime | None = None
_prometheus_lock = threading.Lock()


_default_runtime: Runtime | None = None


def _get_or_create_runtime(
    *, enable_prometheus: bool = True, prometheus_bind_address: str = ""
) -> Runtime:
    """Get or create the process-level Temporal Runtime.

    When *enable_prometheus* is True, the Runtime binds a Prometheus metrics
    endpoint. When False (e.g. local dev), it creates a default Runtime
    without the endpoint to avoid port-already-in-use errors on reload.

    Created at most once per process — subsequent calls return the same instance.
    """
    global _prometheus_runtime, _default_runtime

    if enable_prometheus:
        with _prometheus_lock:
            if _prometheus_runtime is None:
                bind_addr = prometheus_bind_address or TEMPORAL_PROMETHEUS_BIND_ADDRESS
                # Mirror the OTel Prometheus reader's resource enrichment onto
                # Temporal Rust core's emitted metrics so app_name/app_version/…
                # appear as labels on every temporal_* series, matching the
                # SDK-side metrics.
                _prometheus_runtime = Runtime(
                    telemetry=TelemetryConfig(
                        metrics=PrometheusConfig(bind_address=bind_addr),
                        global_tags=get_metric_enrichment_labels(),
                    )
                )
                logger.info("Temporal Prometheus metrics enabled on %s", bind_addr)
                _host = bind_addr.split(":", 1)[0]
                if _host in ("127.0.0.1", "::1", "localhost"):
                    logger.info(
                        "Temporal Prometheus endpoint bound to loopback (%s) — "
                        "external scrapes of this address will receive no data. "
                        "Scrape via the FastAPI /metrics route on the handler "
                        "port (default 8000); the SDK proxies the Temporal "
                        "Rust-core series through that endpoint.",
                        bind_addr,
                    )
        return _prometheus_runtime
    else:
        with _prometheus_lock:
            if _default_runtime is None:
                _default_runtime = Runtime.default()
                logger.info("Temporal Prometheus metrics disabled")
        return _default_runtime


if TYPE_CHECKING:
    from datetime import timedelta

    from temporalio.converter import DataConverter
    from temporalio.service import HttpConnectProxyConfig, TLSConfig

    from application_sdk.app.base import App
    from application_sdk.app.context import AppContext


class TemporalExecutorBackend:
    """Temporal-based executor backend for running Apps as workflows."""

    def __init__(
        self,
        client: Client,
        task_queue: str = "application-sdk",
    ) -> None:
        self._client = client
        self._task_queue = task_queue

    async def execute(
        self,
        app_cls: type[App],
        input_data: Any,
        *,
        context: AppContext,
        retry_policy: RetryPolicy,
        execution_timeout: timedelta | None = None,
        entry_point: str | None = None,
    ) -> Any:
        """Execute an App as a Temporal workflow.

        Args:
            app_cls: The App class to execute.
            input_data: Input data for the workflow.
            context: App execution context.
            retry_policy: Retry policy for the workflow.
            execution_timeout: Optional timeout for the workflow execution.
            entry_point: Entry point name for multi-entry-point apps.
                When provided, the workflow name is ``"{app_name}:{entry_point}"``.
                When omitted, defaults to the app name (single-entry-point apps).
        """
        from uuid import uuid4  # noqa: PLC0415 — stdlib uuid; lazy use

        input_data._correlation_id = context.correlation_id

        prefix = context.app_name
        config_hash = (
            input_data.config_hash() if hasattr(input_data, "config_hash") else ""
        )
        short_id = uuid4().hex[:8]
        workflow_id = (
            f"{prefix}-{config_hash}-{short_id}"
            if config_hash
            else f"{prefix}-{short_id}"
        )

        workflow_name = (
            f"{app_cls._app_name}:{entry_point}" if entry_point else app_cls._app_name
        )
        ep_meta = (
            app_cls._app_metadata.entry_points.get(entry_point) if entry_point else None
        )
        if entry_point is not None and ep_meta is None:
            from application_sdk.execution._temporal._backend_errors import (  # noqa: PLC0415
                UnknownEntryPointError,
            )

            raise UnknownEntryPointError(resource_identifier=entry_point)
        output_type = (
            ep_meta.output_type
            if ep_meta is not None
            else getattr(app_cls, "_output_type", None)
        )
        result = await self._client.execute_workflow(
            workflow_name,
            args=[input_data],
            id=workflow_id,
            task_queue=self._task_queue,
            retry_policy=_to_temporal_retry_policy(retry_policy),
            result_type=output_type,
            execution_timeout=execution_timeout,
        )
        return result

    async def start(
        self,
        app_cls: type[App],
        input_data: Any,
        *,
        context: AppContext,
        retry_policy: RetryPolicy,
        entry_point: str | None = None,
    ) -> str:
        """Start an App workflow without waiting. Returns the workflow ID.

        Args:
            app_cls: The App class to execute.
            input_data: Input data for the workflow.
            context: App execution context.
            retry_policy: Retry policy for the workflow.
            entry_point: Entry point name for multi-entry-point apps.
                When provided, the workflow name is ``"{app_name}:{entry_point}"``.
                When omitted, defaults to the app name (single-entry-point apps).
        """
        from uuid import uuid4  # noqa: PLC0415 — stdlib uuid; lazy use

        input_data._correlation_id = context.correlation_id

        prefix = context.app_name
        config_hash = (
            input_data.config_hash() if hasattr(input_data, "config_hash") else ""
        )
        short_id = uuid4().hex[:8]
        workflow_id = (
            f"{prefix}-{config_hash}-{short_id}"
            if config_hash
            else f"{prefix}-{short_id}"
        )

        workflow_name = (
            f"{app_cls._app_name}:{entry_point}" if entry_point else app_cls._app_name
        )
        handle = await self._client.start_workflow(
            workflow_name,
            args=[input_data],
            id=workflow_id,
            task_queue=self._task_queue,
            retry_policy=_to_temporal_retry_policy(retry_policy),
        )
        return handle.id

    async def get_result(self, workflow_id: str) -> Any:
        """Get the result of a workflow by ID."""
        handle = self._client.get_workflow_handle(workflow_id)
        return await handle.result()

    async def cancel(self, workflow_id: str) -> None:
        """Cancel a running workflow."""
        handle = self._client.get_workflow_handle(workflow_id)
        await handle.cancel()


def _build_tls_config(
    *,
    server_root_ca_cert_path: str = "",
    client_cert_path: str = "",
    client_private_key_path: str = "",
    domain: str = "",
) -> TLSConfig:
    """Build a Temporal TLSConfig from file paths.

    Raises:
        FileNotFoundError: If a specified cert file does not exist.
        ValueError: If client cert is provided without key or vice versa.
    """
    from temporalio.service import (  # noqa: PLC0415 — cold path: TLS reload only when cert paths configured
        TLSConfig,
    )

    server_root_ca_cert: bytes | None = None
    client_cert: bytes | None = None
    client_private_key: bytes | None = None

    if server_root_ca_cert_path:
        path = Path(server_root_ca_cert_path)
        if not path.exists():
            from application_sdk.execution._temporal._backend_errors import (  # noqa: PLC0415
                TlsCertFileNotFoundError,
            )

            raise TlsCertFileNotFoundError(field="server_root_ca_cert")
        server_root_ca_cert = path.read_bytes()
        logger.info("Loaded TLS root CA cert: %s", server_root_ca_cert_path)

    has_cert = bool(client_cert_path)
    has_key = bool(client_private_key_path)
    if has_cert != has_key:
        from application_sdk.execution._temporal._backend_errors import (  # noqa: PLC0415
            MtlsConfigError,
        )

        raise MtlsConfigError()

    if client_cert_path:
        path = Path(client_cert_path)
        if not path.exists():
            from application_sdk.execution._temporal._backend_errors import (  # noqa: PLC0415
                TlsCertFileNotFoundError,
            )

            raise TlsCertFileNotFoundError(field="client_cert")
        client_cert = path.read_bytes()

    if client_private_key_path:
        path = Path(client_private_key_path)
        if not path.exists():
            from application_sdk.execution._temporal._backend_errors import (  # noqa: PLC0415
                TlsCertFileNotFoundError,
            )

            raise TlsCertFileNotFoundError(field="client_private_key")
        client_private_key = path.read_bytes()

    return TLSConfig(
        server_root_ca_cert=server_root_ca_cert,
        client_cert=client_cert,
        client_private_key=client_private_key,
        domain=domain or None,
    )


async def _prefer_v4_target(target_host: str) -> tuple[str, str | None]:
    """Pre-resolve ``target_host`` and prefer an IPv4 address when AAAA is present.

    Caller-gated: ``create_temporal_client`` only invokes this when
    ``ENABLE_ATLAN_UPLOAD`` is set (SDR mode). Internal Atlan services that
    connect via in-cluster Service DNS keep the hostname intact so the
    Rust bridge re-resolves on reconnect and survives Service IP changes.

    Returns ``(resolved_target, sni_hostname)``:

    * ``resolved_target`` — the literal ``<ip>:<port>`` to hand to the Rust
      Temporal bridge (IPv6 results are wrapped in brackets).
    * ``sni_hostname`` — the original DNS name when we substituted an IP,
      else ``None``. Callers must propagate this into ``TLSConfig.domain``
      so the TLS handshake still validates against the right cert.

    Why this exists
    ---------------

    The Temporal Rust bridge resolves the target via Tokio's connector,
    which does not iterate every address returned by the OS resolver:
    on a host where DNS returns both A and AAAA, the bridge picks one
    address (often AAAA first), and if that connect fails the whole
    ``Client.connect`` raises. Two production incidents on customer
    SDR VMs hit this — both had partial IPv6 enablement (only
    link-local v6 configured, no global v6 route) so ``connect()`` to
    a global IPv6 destination fast-fails with ``EADDRNOTAVAIL`` before
    the bridge falls back to v4. The pure-Python Temporal client used
    by the earlier SDK line iterated the full address list and silently
    fell back to v4, which is why the same hosts worked on v2 and
    break on v3.

    Pre-resolving in Python with ``AI_ADDRCONFIG`` returns only the
    address families the host actually has configured. We then always
    pick a v4 IP when one is in the result and substitute it for the
    hostname, falling back to v6 only when no v4 is returned. The key
    invariant: as long as ``getaddrinfo`` returns at least one v4
    result, we hand the Rust bridge a literal v4 IP — which guarantees
    it can't re-resolve and pick a v6 address on its own.

    No-op cases (return ``(target_host, None)`` unchanged):

    * ``target_host`` is already a literal IP (``1.2.3.4:7233`` or
      ``[::1]:7233``).
    * Port is missing or non-numeric (malformed input; let the caller
      fail with a real connect error rather than a parse error here).
    * ``getaddrinfo`` raises (DNS down, unknown host, etc).
    * The resolver returns zero usable address-family entries.
    """
    host, _, port = target_host.rpartition(":")
    if not host or not port.isdigit():
        return target_host, None
    # IPv6 literal targets come bracketed (``[::1]:7233``); strip for the
    # ipaddress check, leave the original ``target_host`` to return.
    host_stripped = host[1:-1] if host.startswith("[") and host.endswith("]") else host
    try:
        ipaddress.ip_address(host_stripped)
    except ValueError:  # conformance: ignore[E002] host is not a literal IP; fall through to DNS resolution
        pass
    else:
        # Already a literal IP — no resolution to do, no SNI to preserve.
        return target_host, None

    # Use ``loop.getaddrinfo`` instead of the blocking ``socket.getaddrinfo``
    # so the resolver runs off the event loop's thread. ``create_temporal_client``
    # is on a hot startup path that overlaps with health-probe handlers,
    # lifespan startup hooks, and other ``create_temporal_client`` calls
    # (worker + executor backend) — a slow DNS server blocking the loop here
    # would stall readiness probes and any other request handlers waiting
    # for their turn. ``loop.getaddrinfo`` delegates to a thread executor
    # so the resolution is concurrent with the rest of startup.
    loop = asyncio.get_running_loop()
    try:
        results = await loop.getaddrinfo(
            host,
            int(port),
            family=socket.AF_UNSPEC,
            type=socket.SOCK_STREAM,
            flags=socket.AI_ADDRCONFIG,
        )
    except (socket.gaierror, ValueError):
        logger.warning(
            "DNS resolution failed for %s; returning unresolved host",
            host,
            exc_info=True,
        )
        return target_host, None
    if not results:
        return target_host, None

    # Prefer v4 whenever a v4 address is in the resolver result.
    #
    # Earlier versions of this helper only swapped when the result
    # contained BOTH A and AAAA — the assumption being that on a
    # v4-only host (where AI_ADDRCONFIG filters out AAAA), the Rust
    # bridge would also see only A and there's nothing to fix. That
    # assumption was wrong in production: the Rust connector does NOT
    # use AI_ADDRCONFIG, so it queries DNS directly and still gets
    # AAAA even when Python's filtered result drops it. On a Docker
    # bridge netns with no v6 stack, Python returns "A only", Rust
    # returns "A + AAAA from DNS", tries the AAAA, and fails with
    # EADDRNOTAVAIL. The only way to keep the Rust client off v6 in
    # that case is to feed it a literal v4 IP from the start.
    v4_results = [r for r in results if r[0] == socket.AF_INET]
    if v4_results:
        ip = v4_results[0][4][0]
        return f"{ip}:{port}", host

    # No v4 available — fall back to v6. This is the legitimate
    # v6-only host case (e.g. some k8s clusters); the Rust client
    # would have used v6 anyway and there's no v4 to prefer.
    v6_results = [r for r in results if r[0] == socket.AF_INET6]
    if v6_results:
        ip = v6_results[0][4][0]
        return f"[{ip}]:{port}", host

    return target_host, None


def _apply_sni_domain(tls: TLSConfig | bool, sni: str) -> TLSConfig | bool:
    """Return a TLSConfig with ``domain`` set to ``sni`` for SNI validation.

    Needed when we've substituted a literal IP for the original
    hostname: without an explicit ``domain``, the Rust bridge defaults
    SNI to the literal IP and the TLS handshake fails because the
    server cert's SAN list doesn't include the IP. Preserves any other
    fields already configured on the caller's TLSConfig and respects a
    caller-supplied ``domain`` if one is already set.
    """
    from temporalio.service import TLSConfig  # noqa: PLC0415 — cold path

    if tls is False:
        return tls
    if tls is True:
        return TLSConfig(domain=sni)
    if isinstance(tls, TLSConfig):
        if tls.domain:
            # Caller explicitly set SNI; respect that.
            return tls
        return TLSConfig(
            server_root_ca_cert=tls.server_root_ca_cert,
            client_cert=tls.client_cert,
            client_private_key=tls.client_private_key,
            domain=sni,
        )
    return tls


def _build_temporal_proxy_config(
    host: str, *, tls_enabled: bool
) -> HttpConnectProxyConfig | None:
    """Build an HTTP CONNECT proxy config for the Temporal gRPC connection.

    Temporal's client does not read proxy environment variables on its own, so
    the SDK forwards them explicitly, resolving them through the stdlib
    (:func:`urllib.request.getproxies`) exactly like ``httpx`` does:

    * the proxy is selected by target scheme — ``HTTPS_PROXY`` for TLS-enabled
      connections, ``HTTP_PROXY`` for plaintext;
    * ``NO_PROXY`` is honored (so in-cluster Temporal reached via Service DNS is
      never tunneled);
    * upper/lowercase env variants and the httpoxy CGI mitigation come for free.

    Args:
        host: Temporal target ``host[:port]`` (used for NO_PROXY matching and to
            keep the proxy CONNECT keyed by hostname).
        tls_enabled: Whether the Temporal connection uses TLS.

    Returns:
        An ``HttpConnectProxyConfig`` when a proxy applies, else ``None`` (direct).
    """
    proxies = urllib.request.getproxies()
    proxy = proxies.get("https" if tls_enabled else "http")
    if not proxy:
        return None

    hostname = host.rsplit(":", 1)[0]  # drop :port before NO_PROXY matching
    if urllib.request.proxy_bypass_environment(hostname, proxies):
        logger.info("Temporal host %s matches NO_PROXY; connecting directly", host)
        return None

    parsed = urlsplit(proxy)  # RFC 3986 proxy URL, e.g. http://host:port
    if parsed.scheme == "https":
        logger.warning(
            "Proxy URL uses an https:// scheme, but Temporal's HTTP CONNECT "
            "tunnel reaches the proxy over plaintext (no TLS-to-proxy)."
        )

    # hostname[:port] only — keep any userinfo out of target_host and the logs.
    target_host = (
        f"{parsed.hostname}:{parsed.port}" if parsed.port else (parsed.hostname or "")
    )
    basic_auth = (
        (parsed.username, parsed.password)
        if parsed.username and parsed.password
        else None
    )

    from temporalio.service import (  # noqa: PLC0415 — cold path: only when a proxy is configured
        HttpConnectProxyConfig,
    )

    logger.info(
        "Routing Temporal gRPC via HTTP CONNECT proxy %s (tls=%s)",
        target_host,
        tls_enabled,
    )
    return HttpConnectProxyConfig(target_host=target_host, basic_auth=basic_auth)


async def create_temporal_client(
    host: str = "localhost:7233",
    namespace: str = "default",
    *,
    data_converter: DataConverter | None = None,
    api_key: str | None = None,
    tls_enabled: bool = False,
    tls_server_root_ca_cert_path: str = "",
    tls_client_cert_path: str = "",
    tls_client_private_key_path: str = "",
    tls_domain: str = "",
    connect_max_attempts: int = 10,
    connect_retry_delay_seconds: float = 0.5,
    enable_prometheus: bool = True,
    prometheus_bind_address: str = "",
) -> Client:
    """Create a Temporal client with optional TLS and auth.

    Supports plain TCP, TLS, mTLS, and API key auth. Retries the connection
    up to ``connect_max_attempts`` times with exponential backoff.

    When ``HTTPS_PROXY`` (TLS) or ``HTTP_PROXY`` (plaintext) is set, the gRPC
    connection is routed through that HTTP CONNECT proxy. Temporal does not read
    these env vars itself, so the SDK forwards them.

    Args:
        host: Temporal server address.
        namespace: Temporal namespace.
        data_converter: Optional custom DataConverter for serialization.
        api_key: Optional Bearer token for Temporal auth.
        tls_enabled: Enable TLS for the connection.
        tls_server_root_ca_cert_path: Path to root CA cert PEM file.
        tls_client_cert_path: Path to client cert PEM file (mTLS).
        tls_client_private_key_path: Path to client private key PEM file (mTLS).
        tls_domain: TLS server name override.
        connect_max_attempts: Maximum connection attempts (default 5).
        connect_retry_delay_seconds: Initial delay between retries (default 2.0s).
        enable_prometheus: Enable Temporal Prometheus metrics endpoint.
        prometheus_bind_address: Bind address for Prometheus metrics (e.g. "0.0.0.0:9464").

    Returns:
        Connected Temporal client.
    """
    tls_config: TLSConfig | bool = False

    if tls_enabled:
        has_any_cert_path = bool(
            tls_server_root_ca_cert_path
            or tls_client_cert_path
            or tls_client_private_key_path
        )
        if has_any_cert_path or tls_domain:
            tls_config = _build_tls_config(
                server_root_ca_cert_path=tls_server_root_ca_cert_path,
                client_cert_path=tls_client_cert_path,
                client_private_key_path=tls_client_private_key_path,
                domain=tls_domain,
            )
        else:
            from application_sdk.clients.ssl_utils import (  # deferred: only needed when no explicit TLS cert paths are provided  # noqa: PLC0415 — cold path: ssl_utils only when no explicit cert paths
                get_custom_ca_cert_bytes,
            )

            ca_cert_bytes = get_custom_ca_cert_bytes()
            if ca_cert_bytes:
                from temporalio.service import (  # noqa: PLC0415 — cold path: TLS only when reloaded
                    TLSConfig,
                )

                tls_config = TLSConfig(server_root_ca_cert=ca_cert_bytes)
            else:
                tls_config = True
        logger.info(
            "Connecting to Temporal with TLS: host=%s namespace=%s mtls=%s",
            host,
            namespace,
            bool(tls_client_cert_path),
        )
    else:
        logger.info(
            "Connecting to Temporal (plaintext): host=%s namespace=%s", host, namespace
        )

    # Temporal's client ignores proxy env vars, so forward an HTTP CONNECT proxy
    # explicitly when HTTPS_PROXY/HTTP_PROXY is set (selected by TLS state).
    proxy_config = _build_temporal_proxy_config(host, tls_enabled=tls_enabled)

    # SDR-only: pre-resolve the target hostname and prefer IPv4 when the OS
    # resolver returns both A and AAAA. Solves the half-broken-IPv6 case on
    # customer SDR hosts (link-local-only v6, or v6 enabled without a global
    # route) where the Rust Temporal bridge picks the AAAA address and fails
    # the whole connect with ``EADDRNOTAVAIL``. See ``_prefer_v4_target`` for
    # the full rationale.
    #
    # Gated on ``ENABLE_ATLAN_UPLOAD`` (the SDR-mode flag) because internal
    # Atlan services connect to Temporal via in-cluster Service DNS
    # (``temporal-frontend.temporal.svc.cluster.local``) where the ClusterIP
    # can change if the Service object is recreated (Helm reapply, namespace
    # rebuild, etc.). On reconnect the Rust bridge keeps using the literal
    # IP it was given at connect time, so a pinned-IP target goes stale and
    # connect fails. Hostname-based connects re-resolve each reconnect and
    # stay healthy through Service IP churn. Only SDR — where DNS goes
    # through Cloudflare anycast and there's no in-cluster Service to track
    # — benefits from the literal-IP substitution.
    #
    # Skipped when a proxy is in use: the proxy does DNS, so we keep the hostname
    # (CONNECT <hostname>, preserving allowlists) instead of sending a bare IP.
    resolved_host, sni_host = host, None
    if ENABLE_ATLAN_UPLOAD and proxy_config is None:
        resolved_host, sni_host = await _prefer_v4_target(host)
        if sni_host is not None:
            # We substituted an IP for the hostname — preserve TLS SNI so
            # the handshake still validates against the original DNS name's
            # cert, not the literal IP.
            tls_config = _apply_sni_domain(tls_config, sni_host)
            logger.info(
                "Pre-resolved Temporal target %s -> %s (SNI preserved as %s)",
                host,
                resolved_host,
                sni_host,
            )

    kwargs: dict[str, Any] = {
        "target_host": resolved_host,
        "namespace": namespace,
        "tls": tls_config,
        "data_converter": data_converter
        if data_converter is not None
        else pydantic_data_converter,
    }
    if api_key:
        kwargs["api_key"] = api_key
    if proxy_config is not None:
        kwargs["http_connect_proxy_config"] = proxy_config

    # Configure Temporal runtime (with or without Prometheus metrics)
    kwargs["runtime"] = _get_or_create_runtime(
        enable_prometheus=enable_prometheus,
        prometheus_bind_address=prometheus_bind_address,
    )

    # Full-jitter exponential backoff (per AWS Architecture blog) — keeps the
    # capped exponential growth but draws each sleep from U(0, cap_at_attempt)
    # to spread concurrent reconnects across the window and avoid thundering
    # herd on Temporal frontend recovery.
    last_exc: Exception | None = None
    backoff_cap = 5.0
    for attempt in range(1, connect_max_attempts + 1):
        try:
            client = await Client.connect(**kwargs)
            logger.info("Connected to Temporal: host=%s namespace=%s", host, namespace)
            return client
        except Exception as exc:
            last_exc = exc
            if attempt < connect_max_attempts:
                cap_at_attempt = min(
                    connect_retry_delay_seconds * (2 ** (attempt - 1)),
                    backoff_cap,
                )
                jittered = random.uniform(0, cap_at_attempt)
                logger.warning(
                    "Temporal connection attempt %d/%d failed, retrying in %.2fs "
                    "(jittered, cap=%.1fs)",
                    attempt,
                    connect_max_attempts,
                    jittered,
                    cap_at_attempt,
                    exc_info=True,
                )
                await asyncio.sleep(jittered)
            else:
                logger.error(
                    "Temporal connection failed after %d attempts",
                    connect_max_attempts,
                    exc_info=True,
                )

    from application_sdk.execution._temporal._backend_errors import (  # noqa: PLC0415
        TemporalConnectError,
    )

    raise TemporalConnectError(target=host, cause=last_exc) from last_exc
