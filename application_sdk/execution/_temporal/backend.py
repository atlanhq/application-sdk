"""Temporal client creation and executor backend."""

from __future__ import annotations

import asyncio
import random
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any

from temporalio.client import Client
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.runtime import PrometheusConfig, Runtime, TelemetryConfig

from application_sdk.constants import TEMPORAL_PROMETHEUS_BIND_ADDRESS
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
    from temporalio.service import TLSConfig

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
            available = list(app_cls._app_metadata.entry_points)
            raise ValueError(
                f"Unknown entry point '{entry_point}' for app '{app_cls._app_name}'. "
                f"Available: {available}"
            )
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
            raise FileNotFoundError(
                f"TLS root CA cert file not found: {server_root_ca_cert_path}"
            )
        server_root_ca_cert = path.read_bytes()
        logger.info("Loaded TLS root CA cert: %s", server_root_ca_cert_path)

    has_cert = bool(client_cert_path)
    has_key = bool(client_private_key_path)
    if has_cert != has_key:
        raise ValueError(
            "mTLS requires both client cert and client private key. "
            f"Got cert={client_cert_path!r}, key={client_private_key_path!r}"
        )

    if client_cert_path:
        path = Path(client_cert_path)
        if not path.exists():
            raise FileNotFoundError(
                f"TLS client cert file not found: {client_cert_path}"
            )
        client_cert = path.read_bytes()

    if client_private_key_path:
        path = Path(client_private_key_path)
        if not path.exists():
            raise FileNotFoundError(
                f"TLS client private key file not found: {client_private_key_path}"
            )
        client_private_key = path.read_bytes()

    return TLSConfig(
        server_root_ca_cert=server_root_ca_cert,
        client_cert=client_cert,
        client_private_key=client_private_key,
        domain=domain or None,
    )


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

    kwargs: dict[str, Any] = {
        "target_host": host,
        "namespace": namespace,
        "tls": tls_config,
        "data_converter": data_converter
        if data_converter is not None
        else pydantic_data_converter,
    }
    if api_key:
        kwargs["api_key"] = api_key

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

    raise RuntimeError(
        f"Failed to connect to Temporal at {host!r} after {connect_max_attempts} attempts"
    ) from last_exc
