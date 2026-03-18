"""Temporal client creation and executor backend."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING, Any

from loguru import logger
from temporalio.client import Client
from temporalio.common import RetryPolicy as TemporalRetryPolicy

if TYPE_CHECKING:
    from datetime import timedelta

    from temporalio.converter import DataConverter
    from temporalio.service import TLSConfig

    from application_sdk.app.base import App
    from application_sdk.app.context import AppContext
    from application_sdk.execution.retry import RetryPolicy


def _to_temporal_retry_policy(policy: RetryPolicy) -> TemporalRetryPolicy:
    """Convert framework RetryPolicy to Temporal RetryPolicy."""
    return TemporalRetryPolicy(
        maximum_attempts=policy.max_attempts,
        initial_interval=policy.initial_interval,
        maximum_interval=policy.max_interval,
        backoff_coefficient=policy.backoff_coefficient,
        non_retryable_error_types=list(policy.non_retryable_errors)
        if policy.non_retryable_errors
        else None,
    )


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
    ) -> Any:
        """Execute an App as a Temporal workflow."""
        from uuid import uuid4

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

        output_type = getattr(app_cls, "_output_type", None)
        result = await self._client.execute_workflow(
            app_cls._app_name,
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
    ) -> str:
        """Start an App workflow without waiting. Returns the workflow ID."""
        from uuid import uuid4

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

        handle = await self._client.start_workflow(
            app_cls._app_name,
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
    from temporalio.service import TLSConfig

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
        logger.info(f"Loaded TLS root CA cert: {server_root_ca_cert_path}")

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
    connect_max_attempts: int = 5,
    connect_retry_delay_seconds: float = 2.0,
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
            tls_config = True
        logger.info(
            f"Connecting to Temporal with TLS: host={host} namespace={namespace} "
            f"mtls={bool(tls_client_cert_path)}"
        )
    else:
        logger.info(
            f"Connecting to Temporal (plaintext): host={host} namespace={namespace}"
        )

    kwargs: dict[str, Any] = {
        "target_host": host,
        "namespace": namespace,
        "tls": tls_config,
    }
    if data_converter is not None:
        kwargs["data_converter"] = data_converter
    if api_key:
        kwargs["api_key"] = api_key

    last_exc: Exception | None = None
    delay = connect_retry_delay_seconds
    for attempt in range(1, connect_max_attempts + 1):
        try:
            client = await Client.connect(**kwargs)
            logger.info(f"Connected to Temporal: host={host} namespace={namespace}")
            return client
        except Exception as exc:
            last_exc = exc
            if attempt < connect_max_attempts:
                logger.warning(
                    f"Temporal connection attempt {attempt}/{connect_max_attempts} failed, "
                    f"retrying in {delay}s: {exc}"
                )
                await asyncio.sleep(delay)
                delay *= 2
            else:
                logger.error(
                    f"Temporal connection failed after {connect_max_attempts} attempts: {exc}",
                    exc_info=True,
                )

    raise RuntimeError(
        f"Failed to connect to Temporal at {host!r} after {connect_max_attempts} attempts"
    ) from last_exc
