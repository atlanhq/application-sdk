"""Unified entry point for Application SDK containers.

Supports three execution modes:
- ``worker``: Temporal workflow execution only
- ``handler``: HTTP FastAPI handler service only
- ``combined``: Worker + handler in a single process (SDR / docker-compose)

CLI usage::

    python -m application_sdk.main --mode worker --app my_package.apps:MyApp
    python -m application_sdk.main --mode handler --app my_package.apps:MyApp
    python -m application_sdk.main --mode combined --app my_package.apps:MyApp

Environment variable equivalents::

    ATLAN_APP_MODE=combined
    ATLAN_APP_MODULE=my_package.apps:MyApp
"""

from __future__ import annotations

import argparse
import asyncio
import faulthandler
import os
import signal
import sys
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, NoReturn

# Enable faulthandler so C-level crashes dump a traceback to stderr.
faulthandler.enable()

# Module-level reference to the running event loop (set in worker/combined mode).
# Used by the SIGUSR1 debug handler to snapshot asyncio tasks.
_worker_event_loop: asyncio.AbstractEventLoop | None = None


def _debug_dump_handler(signum: int, frame: object) -> None:  # noqa: ARG001
    """Dump thread stacks and asyncio tasks to /tmp/debug-dump-<pid>.txt on SIGUSR1."""
    dump_path = os.path.join("/tmp", f"debug-dump-{os.getpid()}.txt")  # noqa: PTH118
    fd = os.open(dump_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    try:
        os.write(fd, b"\n===== DEBUG DUMP (SIGUSR1) =====\n")
        os.write(fd, f"PID: {os.getpid()}\n\n".encode())
        os.write(fd, b"--- Thread Stacks ---\n")
        faulthandler.dump_traceback(file=fd, all_threads=True)
        os.write(fd, b"\n--- Asyncio Tasks ---\n")
        loop = _worker_event_loop
        if loop is not None and loop.is_running():
            for task in asyncio.all_tasks(loop):
                os.write(fd, f"\nTask: {task.get_name()}\n".encode())
                for fr in task.get_stack():
                    os.write(
                        fd,
                        f'  File "{fr.f_code.co_filename}", line {fr.f_lineno}, in {fr.f_code.co_name}\n'.encode(),
                    )
        else:
            os.write(fd, b"  (event loop not running)\n")
        os.write(fd, b"\n===== END DEBUG DUMP =====\n")
    finally:
        os.close(fd)
    print(f"Debug dump written to {dump_path}", file=sys.stderr, flush=True)


if hasattr(signal, "SIGUSR1"):
    signal.signal(signal.SIGUSR1, _debug_dump_handler)

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from dapr.clients import DaprClient

    from application_sdk.app.base import App
    from application_sdk.infrastructure.context import InfrastructureContext
    from application_sdk.infrastructure.secrets import SecretStore

from application_sdk.observability.logger_adaptor import get_logger  # noqa: E402

logger = get_logger(__name__)

from application_sdk.discovery import (  # noqa: E402
    DiscoveryError,
    load_app_class,
    load_handler_class,
    validate_app_class,
)


@dataclass
class AppConfig:
    """Configuration for app execution.

    Loaded from CLI arguments with environment variable fallbacks.
    """

    mode: str
    """Execution mode: "worker", "handler", or "combined"."""

    app_module: str
    """App class module path, e.g. "my_package.apps:MyApp"."""

    handler_module: str | None = None
    """Optional explicit handler module path."""

    # Worker
    temporal_host: str = "localhost:7233"
    temporal_namespace: str = "default"
    task_queue: str = ""  # derived from app_module in __post_init__ if not set

    # Handler
    handler_host: str = "0.0.0.0"
    handler_port: int = 8000
    frontend_assets_path: str = "frontend/static"

    # Common
    log_level: str = "INFO"
    service_name: str = ""
    health_port: int = 8081

    # TLS
    tls_enabled: bool = False
    tls_server_root_ca_cert_path: str = ""
    tls_client_cert_path: str = ""
    tls_client_private_key_path: str = ""
    tls_domain: str = ""

    # Auth
    auth_enabled: bool = False
    auth_client_id: str = ""
    auth_client_secret: str = ""
    auth_token_url: str = ""
    auth_base_url: str = ""
    auth_scopes: str = ""

    def __post_init__(self) -> None:
        """Derive task_queue from app_module when not explicitly set."""
        if not self.task_queue and self.app_module:
            self.task_queue = _derive_task_queue(self.app_module)

    @classmethod
    def from_args_and_env(cls, args: argparse.Namespace) -> AppConfig:
        """Create config from CLI args with env var fallbacks.

        CLI arguments take precedence over environment variables.
        """

        def _env(key: str, default: str = "") -> str:
            return os.environ.get(key, default)

        def _env_int(key: str, default: int) -> int:
            val = os.environ.get(key)
            return int(val) if val else default

        def _env_bool(key: str, default: bool = False) -> bool:
            val = os.environ.get(key, "").lower()
            return val in ("true", "1", "yes") if val else default

        mode = args.mode or _env("ATLAN_APP_MODE")
        if not mode:
            # Fall back to APPLICATION_MODE with value mapping for backwards-compat.
            # WORKER→worker, SERVER→handler, anything else (LOCAL, unset)→combined
            _legacy_mode = _env("APPLICATION_MODE").upper()
            mode = {"WORKER": "worker", "SERVER": "handler"}.get(
                _legacy_mode, "combined"
            )

        app_module = args.app or _env("ATLAN_APP_MODULE")
        if not app_module:
            raise ValueError(
                "App module is required. Use --app or set ATLAN_APP_MODULE."
            )

        service_name = (
            getattr(args, "service_name", None)
            or _env("ATLAN_SERVICE_NAME")
            or _env("OTEL_SERVICE_NAME")
            or _derive_service_name(app_module)
        )

        # v2 fallback: combine ATLAN_WORKFLOW_HOST + ATLAN_WORKFLOW_PORT into host:port
        _v2_workflow_host = _env("ATLAN_WORKFLOW_HOST")
        _v2_temporal_host = (
            f"{_v2_workflow_host}:{_env('ATLAN_WORKFLOW_PORT', '7233')}"
            if _v2_workflow_host
            else ""
        )

        # Task queue: prefer ATLAN_APPLICATION_NAME+ATLAN_DEPLOYMENT_NAME (matches v2
        # TemporalWorkflowClient.get_worker_task_queue()), fall back to class-name derivation.
        _default_task_queue = _derive_task_queue(app_module)

        return cls(
            mode=mode,
            app_module=app_module,
            handler_module=getattr(args, "handler", None)
            or _env("ATLAN_HANDLER_MODULE")
            or None,
            temporal_host=getattr(args, "temporal_host", None)
            or _env("ATLAN_TEMPORAL_HOST")
            or _v2_temporal_host
            or "localhost:7233",
            temporal_namespace=getattr(args, "temporal_namespace", None)
            or _env("ATLAN_TEMPORAL_NAMESPACE")
            or _env("ATLAN_WORKFLOW_NAMESPACE", "default"),
            task_queue=getattr(args, "task_queue", None)
            or _env("ATLAN_TASK_QUEUE")
            or _default_task_queue,
            handler_host=getattr(args, "handler_host", None)
            or _env("ATLAN_HANDLER_HOST")
            or _env("ATLAN_APP_HTTP_HOST", "0.0.0.0"),
            handler_port=getattr(args, "handler_port", None)
            or _env_int("ATLAN_HANDLER_PORT", 0)
            or _env_int("ATLAN_APP_HTTP_PORT", 0)
            or 8000,
            log_level=getattr(args, "log_level", None)
            or _env("ATLAN_LOG_LEVEL")
            or _env("LOG_LEVEL", "INFO"),
            health_port=getattr(args, "health_port", None)
            or _env_int("ATLAN_HEALTH_PORT", 8081),
            service_name=service_name,
            # TLS
            tls_enabled=_env_bool("ATLAN_TEMPORAL_TLS_ENABLED"),
            tls_server_root_ca_cert_path=_env("ATLAN_TEMPORAL_TLS_CA_CERT_PATH"),
            tls_client_cert_path=_env("ATLAN_TEMPORAL_TLS_CLIENT_CERT_PATH"),
            tls_client_private_key_path=_env("ATLAN_TEMPORAL_TLS_CLIENT_KEY_PATH"),
            tls_domain=_env("ATLAN_TEMPORAL_TLS_DOMAIN"),
            # Auth
            auth_enabled=_env_bool("ATLAN_AUTH_ENABLED"),
            auth_client_id=_env("ATLAN_AUTH_CLIENT_ID"),
            auth_client_secret=_env("ATLAN_AUTH_CLIENT_SECRET"),
            auth_token_url=_env("ATLAN_AUTH_TOKEN_URL"),
            auth_base_url=_env("ATLAN_AUTH_BASE_URL"),
            auth_scopes=_env("ATLAN_AUTH_SCOPES"),
            frontend_assets_path=_env("ATLAN_FRONTEND_ASSETS_PATH", "frontend/static"),
        )


# Allowlist of Dapr component metadata keys that are safe to log.
# Keys NOT in this set (e.g. accessKey, secretKey, connectionString, url)
# are silently suppressed even if present in the component YAML.
_SAFE_METADATA_KEYS: frozenset[str] = frozenset(
    {
        "rootPath",
        "bucket",
        "region",
        "endpoint",
        "containerName",
        "accountName",
        "storageAccount",
        "forcePathStyle",
        "brokers",
        "consumerGroup",
        "clientId",
        "authType",
        "actorStateStore",
        "keyPrefix",
        "HotReload",
    }
)


def _parse_all_component_yamls(components_dir: "Path") -> dict[str, dict[str, str]]:
    """Parse all Dapr component YAML files and return safe metadata per component.

    Returns a mapping of component name → dict of allowlisted metadata values.
    Non-allowlisted keys (secrets, connection strings, credentials) are never included.
    Silently returns an empty dict on any parse error.
    """
    import yaml

    from application_sdk.storage.binding import _parse_dapr_metadata

    result: dict[str, dict[str, str]] = {}
    try:
        for yaml_file in sorted(components_dir.glob("*.yaml")):
            with yaml_file.open() as fh:
                doc = yaml.safe_load(fh)
            if not doc or doc.get("kind") != "Component":
                continue
            name = doc.get("metadata", {}).get("name")
            if not name:
                continue
            spec = doc.get("spec", {})
            all_meta = _parse_dapr_metadata(spec.get("metadata", []))
            safe = {k: v for k, v in all_meta.items() if k in _SAFE_METADATA_KEYS}
            result[name] = safe
    except Exception:
        logger.warning("Could not parse component YAMLs for diagnostics", exc_info=True)
    return result


def _log_dapr_components(
    dapr_client: "DaprClient",
    components_dir: "Path",
) -> None:
    """Log registered Dapr components and their safe configuration at startup.

    Queries the Dapr sidecar metadata API for all registered components and
    emits one INFO log line per component showing its type, version, and any
    non-sensitive metadata from the corresponding component YAML.

    Also warns about expected components (state store, secret store, object
    store, event binding) that are not registered in the sidecar.

    This is best-effort: any failure is logged as a WARNING and never blocks
    startup.
    """
    from application_sdk.constants import (
        DEPLOYMENT_OBJECT_STORE_NAME,
        EVENT_STORE_NAME,
        SECRET_STORE_NAME,
        STATE_STORE_NAME,
    )

    try:
        metadata = dapr_client.get_metadata()
    except Exception:
        logger.warning(
            "Could not query Dapr metadata — component diagnostics unavailable",
            exc_info=True,
        )
        return

    registered = {c.name: c for c in getattr(metadata, "registered_components", [])}
    yaml_details = _parse_all_component_yamls(components_dir)

    for name, comp in registered.items():
        safe_meta = yaml_details.get(name, {})
        if safe_meta:
            detail = ", ".join("%s=%s" % (k, v) for k, v in safe_meta.items())
            logger.info(
                "Dapr component: %s (type=%s, version=%s) — %s",
                name,
                comp.type,
                comp.version,
                detail,
            )
        else:
            logger.info(
                "Dapr component: %s (type=%s, version=%s)",
                name,
                comp.type,
                comp.version,
            )

    expected = {
        STATE_STORE_NAME: "state_store",
        SECRET_STORE_NAME: "secret_store",
        DEPLOYMENT_OBJECT_STORE_NAME: "object_store",
        EVENT_STORE_NAME: "event_binding",
    }
    for comp_name, role in expected.items():
        if comp_name not in registered:
            logger.warning(
                "Expected Dapr component %s (role=%s) not registered in sidecar",
                comp_name,
                role,
            )


def _create_infrastructure(
    credential_stores: "Mapping[str, SecretStore] | None" = None,
) -> "InfrastructureContext":
    """Create infrastructure services based on environment.

    If ``DAPR_HTTP_PORT`` is set (Dapr sidecar present), creates Dapr-backed
    implementations. Otherwise creates InMemory implementations suitable for
    local development and testing.

    Args:
        credential_stores: Optional mapping of store name → SecretStore. When
            provided (e.g., from ``run_dev_combined``), the first value is used
            as the secret store instead of a blank ``InMemorySecretStore``.

    Returns:
        Configured InfrastructureContext.
    """
    from application_sdk.infrastructure.context import InfrastructureContext
    from application_sdk.infrastructure.secrets import InMemorySecretStore
    from application_sdk.infrastructure.state import InMemoryStateStore

    if os.environ.get("DAPR_HTTP_PORT"):
        from pathlib import Path

        from application_sdk.constants import (
            DEPLOYMENT_OBJECT_STORE_NAME,
            EVENT_STORE_NAME,
            SECRET_STORE_NAME,
            STATE_STORE_NAME,
        )
        from application_sdk.infrastructure._dapr.client import (
            DaprBinding,
            DaprSecretStore,
            DaprStateStore,
            create_dapr_client,
        )
        from application_sdk.storage import create_store_from_binding

        dapr_client = create_dapr_client()
        components_dir = Path(os.environ.get("DAPR_COMPONENTS_PATH", "./components"))
        _log_dapr_components(dapr_client, components_dir)
        logger.info("Dapr sidecar detected — using Dapr infrastructure")
        return InfrastructureContext(
            state_store=DaprStateStore(dapr_client, store_name=STATE_STORE_NAME),
            secret_store=DaprSecretStore(dapr_client, store_name=SECRET_STORE_NAME),
            storage=create_store_from_binding(
                DEPLOYMENT_OBJECT_STORE_NAME,
                components_dir=components_dir,
            ),
            event_binding=DaprBinding(dapr_client, EVENT_STORE_NAME),
        )
    else:
        # No Dapr — use InMemory implementations
        secret_store: SecretStore
        if credential_stores:
            secret_store = next(iter(credential_stores.values()))
        else:
            secret_store = InMemorySecretStore()

        storage_root = os.environ.get("APP_STORAGE_ROOT", "./local/dapr/objectstore")
        from application_sdk.storage import create_local_store

        logger.info(
            "No Dapr sidecar — using InMemory + LocalStore infrastructure",
            storage_root=storage_root,
        )
        return InfrastructureContext(
            state_store=InMemoryStateStore(),
            secret_store=secret_store,
            storage=create_local_store(storage_root),
            event_binding=None,
        )


def _derive_service_name(app_module: str) -> str:
    """Convert "my_package.apps:MyApp" to "my-app" (kebab-case)."""
    if ":" in app_module:
        from application_sdk.app.base import _pascal_to_kebab

        return _pascal_to_kebab(app_module.split(":")[1])
    return "application-sdk"


def _derive_task_queue(app_module: str) -> str:
    """Derive the default task queue name.

    Mirrors v2 TemporalWorkflowClient.get_worker_task_queue():
    - If ATLAN_APPLICATION_NAME + ATLAN_DEPLOYMENT_NAME are set → atlan-{app}-{deployment}
    - If only ATLAN_APPLICATION_NAME is set → {app}
    - Otherwise fall back to class-name derivation → {ClassName}-queue
    """
    app_name = os.environ.get("ATLAN_APPLICATION_NAME", "")
    deployment_name = os.environ.get("ATLAN_DEPLOYMENT_NAME", "")
    if app_name and deployment_name:
        return f"atlan-{app_name}-{deployment_name}"
    if app_name:
        return app_name
    return f"{_derive_service_name(app_module)}-queue"


async def _flush_observability() -> None:
    """Flush all observability buffers before exit."""
    from application_sdk.observability.observability import AtlanObservability

    try:
        await AtlanObservability.flush_all()
    except Exception:
        logger.warning(
            "Failed to flush observability buffers on shutdown", exc_info=True
        )


def _loop_exception_handler(
    loop: asyncio.AbstractEventLoop, context: dict[str, Any]
) -> None:
    """Log unhandled asyncio task exceptions and schedule a flush.

    Registered via ``loop.set_exception_handler()`` in each async run mode.
    The loop is still alive when this fires, so ``create_task`` is safe.
    """
    exc = context.get("exception")
    msg = context.get("message", "Unhandled asyncio exception")
    if exc is not None:
        logger.error("Unhandled asyncio task exception: %s", msg, exc_info=exc)
    else:
        logger.error("Asyncio exception (no exception object): %s", msg)
    # Schedule a flush — non-blocking so the loop can continue
    loop.create_task(_flush_observability())
    # Preserve the default stderr output
    loop.default_exception_handler(context)


def _install_excepthook() -> None:
    """Install sys.excepthook to log + flush on uncaught main-thread exceptions.

    Covers crashes that escape main()'s try/except: module-level startup errors,
    BaseException subclasses not caught elsewhere, etc.  Called once at the top
    of main() so it is active for the full process lifetime.
    """
    _orig = sys.excepthook

    def _hook(exc_type, exc_value, exc_traceback):  # type: ignore[no-untyped-def]
        logger.error(
            "Unhandled exception — flushing observability before exit",
            exc_info=(exc_type, exc_value, exc_traceback),
        )
        try:
            asyncio.run(_flush_observability())
        except Exception:
            pass  # best-effort; never mask the original crash
        _orig(exc_type, exc_value, exc_traceback)

    sys.excepthook = _hook


async def run_worker_mode(config: AppConfig) -> None:
    """Run in worker mode (Temporal workflow execution).

    Loads the app class, connects to Temporal, creates a worker, and
    runs until a shutdown signal is received.
    """
    global _worker_event_loop
    _worker_event_loop = asyncio.get_running_loop()

    from application_sdk.app.registry import AppRegistry, TaskRegistry
    from application_sdk.execution._temporal.backend import create_temporal_client
    from application_sdk.execution._temporal.converter import (
        create_data_converter_for_app,
    )
    from application_sdk.execution._temporal.worker import create_worker
    from application_sdk.infrastructure.context import set_infrastructure

    logger.info(
        "Starting worker mode: app=%s temporal=%s queue=%s",
        config.app_module,
        config.temporal_host,
        config.task_queue,
    )

    infra = _create_infrastructure()
    set_infrastructure(infra)

    app_class = load_app_class(config.app_module)
    validate_app_class(app_class)
    app_name = app_class._app_name  # type: ignore[attr-defined]

    logger.info(
        "Loaded app %s version %s",
        app_name,
        app_class._app_version,  # type: ignore[attr-defined]
    )

    data_converter = create_data_converter_for_app(app_class)

    # Acquire auth token if enabled
    auth_manager: Any = None
    api_key: str | None = None
    if config.auth_enabled:
        from application_sdk.execution._temporal.auth import (
            TemporalAuthConfig,
            TemporalAuthManager,
        )

        auth_config = TemporalAuthConfig(
            client_id=config.auth_client_id,
            client_secret=config.auth_client_secret,
            token_url=config.auth_token_url,
            base_url=config.auth_base_url,
            scopes=config.auth_scopes,
        )
        auth_manager = TemporalAuthManager(auth_config)
        api_key = await auth_manager.acquire_initial_token()
        logger.info("Acquired initial auth token")

    logger.info("Connecting to Temporal %s", config.temporal_host)
    client = await create_temporal_client(
        config.temporal_host,
        config.temporal_namespace,
        data_converter=data_converter,
        api_key=api_key,
        tls_enabled=config.tls_enabled,
        tls_server_root_ca_cert_path=config.tls_server_root_ca_cert_path,
        tls_client_cert_path=config.tls_client_cert_path,
        tls_client_private_key_path=config.tls_client_private_key_path,
        tls_domain=config.tls_domain,
    )

    if auth_manager is not None:
        auth_manager.start_background_refresh(client)
        logger.info("Background token refresh started")

    worker = create_worker(client, task_queue=config.task_queue, app_names=[app_name])

    # Log registrations
    for registered_app in AppRegistry.get_instance().list_apps():
        app_meta = AppRegistry.get_instance().get(registered_app)
        logger.info("Registered app %s version %s", registered_app, app_meta.version)

    for registered_app, tasks in TaskRegistry.get_instance().get_all_tasks().items():
        for task_meta in tasks:
            logger.debug(
                "Registered task %s for app %s", task_meta.name, registered_app
            )

    # Graceful shutdown
    shutdown_event = asyncio.Event()

    def _signal_handler() -> None:
        logger.info("Shutdown signal received")
        shutdown_event.set()

    loop = asyncio.get_running_loop()
    loop.set_exception_handler(_loop_exception_handler)
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _signal_handler)

    from application_sdk.server.health import WorkerHealthServer

    health_server = WorkerHealthServer(port=config.health_port)
    health_server.set_temporal_client(client)

    logger.info("Worker started: app=%s queue=%s", app_name, config.task_queue)
    async with health_server:
        async with worker:
            await shutdown_event.wait()

    await _flush_observability()
    if auth_manager is not None:
        await auth_manager.shutdown()
        logger.info("Auth manager stopped")

    logger.info("Worker stopped")


def run_handler_mode(config: AppConfig) -> None:
    """Run in handler mode (HTTP FastAPI server).

    Loads the handler class (or DefaultHandler) and runs the FastAPI
    server via uvicorn. This is synchronous — uvicorn manages its own loop.
    """
    from application_sdk.execution._temporal.converter import (
        create_data_converter_for_app,
    )
    from application_sdk.handler import DefaultHandler, run_app_handler_service
    from application_sdk.infrastructure.context import set_infrastructure

    infra = _create_infrastructure()
    set_infrastructure(infra)

    logger.info(
        "Starting handler mode: app=%s host=%s port=%d",
        config.app_module,
        config.handler_host,
        config.handler_port,
    )

    app_class = load_app_class(config.app_module)
    app_name = app_class._app_name  # type: ignore[attr-defined]

    handler_class = load_handler_class(
        config.app_module,
        handler_module_path=config.handler_module,
    )
    if handler_class is None:
        handler_class = DefaultHandler
        logger.info("Using DefaultHandler for %s", app_name)
    else:
        logger.info(
            "Loaded custom handler %s for %s",
            handler_class.__name__,
            app_name,
        )

    handler = handler_class()
    data_converter = create_data_converter_for_app(app_class)

    run_app_handler_service(
        handler,
        host=config.handler_host,
        port=config.handler_port,
        log_level=config.log_level.lower(),
        app_name=app_name,
        app_class=app_class,
        temporal_host=config.temporal_host,
        temporal_namespace=config.temporal_namespace,
        task_queue=config.task_queue,
        data_converter=data_converter,
        tls_enabled=config.tls_enabled,
        tls_server_root_ca_cert_path=config.tls_server_root_ca_cert_path,
        tls_client_cert_path=config.tls_client_cert_path,
        tls_client_private_key_path=config.tls_client_private_key_path,
        tls_domain=config.tls_domain,
        auth_enabled=config.auth_enabled,
        auth_client_id=config.auth_client_id,
        auth_client_secret=config.auth_client_secret,
        auth_token_url=config.auth_token_url,
        auth_base_url=config.auth_base_url,
        auth_scopes=config.auth_scopes,
        state_store=infra.state_store,
        storage=infra.storage,
        frontend_assets_path=config.frontend_assets_path,
    )
    asyncio.run(_flush_observability())


async def run_combined_mode(config: AppConfig) -> None:
    """Run worker + handler in a single process (SDR / docker-compose mode).

    Combines Temporal worker execution with the HTTP handler service,
    enabling single-container deployment. Both components share the same
    event loop and shut down together on SIGINT/SIGTERM.
    """
    global _worker_event_loop
    _worker_event_loop = asyncio.get_running_loop()

    import uvicorn

    from application_sdk.app.registry import AppRegistry, TaskRegistry
    from application_sdk.execution._temporal.backend import create_temporal_client
    from application_sdk.execution._temporal.converter import (
        create_data_converter_for_app,
    )
    from application_sdk.execution._temporal.worker import create_worker
    from application_sdk.handler import DefaultHandler, create_app_handler_service
    from application_sdk.infrastructure.context import (
        get_infrastructure,
        set_infrastructure,
    )

    # Only create infrastructure if not already set (e.g., by run_dev_combined)
    _existing_infra = get_infrastructure()
    if _existing_infra is None:
        infra = _create_infrastructure()
        set_infrastructure(infra)
    else:
        infra = _existing_infra

    logger.info(
        "Starting combined mode: app=%s temporal=%s queue=%s port=%d",
        config.app_module,
        config.temporal_host,
        config.task_queue,
        config.handler_port,
    )

    app_class = load_app_class(config.app_module)
    validate_app_class(app_class)
    app_name = app_class._app_name  # type: ignore[attr-defined]

    logger.info(
        "Loaded app %s version %s",
        app_name,
        app_class._app_version,  # type: ignore[attr-defined]
    )

    data_converter = create_data_converter_for_app(app_class)

    auth_manager: Any = None
    api_key: str | None = None
    if config.auth_enabled:
        from application_sdk.execution._temporal.auth import (
            TemporalAuthConfig,
            TemporalAuthManager,
        )

        auth_config = TemporalAuthConfig(
            client_id=config.auth_client_id,
            client_secret=config.auth_client_secret,
            token_url=config.auth_token_url,
            base_url=config.auth_base_url,
            scopes=config.auth_scopes,
        )
        auth_manager = TemporalAuthManager(auth_config)
        api_key = await auth_manager.acquire_initial_token()
        logger.info("Acquired initial auth token")

    logger.info("Connecting to Temporal %s", config.temporal_host)
    client = await create_temporal_client(
        config.temporal_host,
        config.temporal_namespace,
        data_converter=data_converter,
        api_key=api_key,
        tls_enabled=config.tls_enabled,
        tls_server_root_ca_cert_path=config.tls_server_root_ca_cert_path,
        tls_client_cert_path=config.tls_client_cert_path,
        tls_client_private_key_path=config.tls_client_private_key_path,
        tls_domain=config.tls_domain,
    )

    if auth_manager is not None:
        auth_manager.start_background_refresh(client)
        logger.info("Background token refresh started")

    worker = create_worker(client, task_queue=config.task_queue, app_names=[app_name])

    for registered_app in AppRegistry.get_instance().list_apps():
        app_meta = AppRegistry.get_instance().get(registered_app)
        logger.info("Registered app %s version %s", registered_app, app_meta.version)

    for registered_app, tasks in TaskRegistry.get_instance().get_all_tasks().items():
        for task_meta in tasks:
            logger.debug(
                "Registered task %s for app %s", task_meta.name, registered_app
            )

    handler_class = load_handler_class(
        config.app_module,
        handler_module_path=config.handler_module,
    )
    if handler_class is None:
        handler_class = DefaultHandler
        logger.info("Using DefaultHandler for %s", app_name)
    else:
        logger.info(
            "Loaded custom handler %s for %s",
            handler_class.__name__,
            app_name,
        )

    handler = handler_class()
    fastapi_app = create_app_handler_service(
        handler,
        app_name=app_name,
        app_class=app_class,
        temporal_host=config.temporal_host,
        temporal_namespace=config.temporal_namespace,
        task_queue=config.task_queue,
        data_converter=data_converter,
        tls_enabled=config.tls_enabled,
        tls_server_root_ca_cert_path=config.tls_server_root_ca_cert_path,
        tls_client_cert_path=config.tls_client_cert_path,
        tls_client_private_key_path=config.tls_client_private_key_path,
        tls_domain=config.tls_domain,
        auth_enabled=config.auth_enabled,
        auth_client_id=config.auth_client_id,
        auth_client_secret=config.auth_client_secret,
        auth_token_url=config.auth_token_url,
        auth_base_url=config.auth_base_url,
        auth_scopes=config.auth_scopes,
        state_store=infra.state_store,
        storage=infra.storage,
        frontend_assets_path=config.frontend_assets_path,
    )

    uvicorn_server = uvicorn.Server(
        uvicorn.Config(
            fastapi_app,
            host=config.handler_host,
            port=config.handler_port,
            log_level=config.log_level.lower(),
        )
    )

    shutdown_event = asyncio.Event()

    def _signal_handler() -> None:
        logger.info("Shutdown signal received")
        shutdown_event.set()
        uvicorn_server.should_exit = True

    loop = asyncio.get_running_loop()
    loop.set_exception_handler(_loop_exception_handler)
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _signal_handler)

    from application_sdk.server.health import WorkerHealthServer

    health_server = WorkerHealthServer(port=config.health_port)
    health_server.set_temporal_client(client)

    logger.info(
        "Combined mode started: app=%s queue=%s port=%d",
        app_name,
        config.task_queue,
        config.handler_port,
    )
    async with health_server:
        async with worker:
            await asyncio.gather(
                uvicorn_server.serve(),
                shutdown_event.wait(),
            )

    await _flush_observability()
    if auth_manager is not None:
        await auth_manager.shutdown()
        logger.info("Auth manager stopped")

    logger.info("Combined mode stopped")


async def run_dev_combined(
    app_class: type[App],
    *,
    credential_stores: Mapping[str, SecretStore] | None = None,
    example_input: dict[str, Any] | None = None,
    host: str = "127.0.0.1",
    port: int = 8000,
    temporal_host: str = "localhost:7233",
    temporal_namespace: str = "default",
    task_queue: str = "",
) -> None:
    """Run worker + handler in a single process for local development.

    Dev-friendly wrapper around ``run_combined_mode()`` that accepts a
    Python class and keyword arguments directly. Use it in ``run_dev.py``
    scripts; production containers use ``run_combined_mode()`` via CLI flags.

    Args:
        app_class: The App class to serve (must already be imported).
        credential_stores: Optional mapping of store name → SecretStore.
        example_input: Optional dict shown as curl example in startup banner.
        host: Bind host (default: "127.0.0.1").
        port: Handler HTTP port.
        temporal_host: Temporal server address.
        temporal_namespace: Temporal namespace.
        task_queue: Task queue name (default: "{app_name}-queue").

    Example::

        import asyncio
        from my_app import MyApp
        asyncio.run(run_dev_combined(MyApp, example_input={"key": "value"}))
    """
    import json as _json

    app_name = getattr(app_class, "_app_name", "") or app_class.__name__.lower()
    effective_task_queue = task_queue or f"{app_name}-queue"
    app_module = f"{app_class.__module__}:{app_class.__name__}"

    config = AppConfig(
        mode="combined",
        app_module=app_module,
        temporal_host=temporal_host,
        temporal_namespace=temporal_namespace,
        task_queue=effective_task_queue,
        handler_host=host,
        handler_port=port,
        log_level="DEBUG",
        service_name=_derive_service_name(app_module),
    )

    # Create infrastructure early so run_combined_mode uses it directly
    from application_sdk.infrastructure.context import set_infrastructure

    infra = _create_infrastructure(credential_stores=credential_stores)
    set_infrastructure(infra)

    print(f"\nDev server running at http://{host}:{port}")
    print("  POST /workflows/v1/start                         - Start workflow")
    print("  POST /workflows/v1/stop/{workflow_id}/{run_id}   - Stop workflow")
    print("  GET  /workflows/v1/result/{workflow_id}          - Get result")
    print("  GET  /workflows/v1/status/{workflow_id}/{run_id} - Get status")
    print("  GET  /health                                      - Health check")
    print("\nExample:")
    if example_input is not None:
        example_json = _json.dumps(example_input, indent=2)
        print(f"  curl -X POST http://{host}:{port}/workflows/v1/start \\")
        print('    -H "Content-Type: application/json" \\')
        print(f"    -d '{example_json}'")
    else:
        print(
            f"  curl -X POST http://{host}:{port}/workflows/v1/start"
            f' -H "Content-Type: application/json" -d \'{{"name": "World"}}\''
        )
    print(f"\n  curl http://{host}:{port}/workflows/v1/result/{{workflow_id}}\n")

    await run_combined_mode(config)


def run_main(config: AppConfig) -> None:
    """Route to worker, handler, or combined mode based on config."""
    if config.mode == "worker":
        asyncio.run(run_worker_mode(config))
    elif config.mode == "handler":
        run_handler_mode(config)
    elif config.mode == "combined":
        asyncio.run(run_combined_mode(config))
    else:
        raise ValueError(
            f"Unknown mode: {config.mode!r}. Must be 'worker', 'handler', or 'combined'."
        )


def parse_args() -> argparse.Namespace:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description="Application SDK — run apps in worker, handler, or combined mode",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Environment Variables:
  ATLAN_APP_MODE           Execution mode (worker, handler, or combined)
  ATLAN_APP_MODULE         App class path (e.g., my_package.apps:MyApp)
  ATLAN_HANDLER_MODULE     Optional custom handler module path
  ATLAN_TEMPORAL_HOST      Temporal server address (default: localhost:7233)
                           Falls back to ATLAN_WORKFLOW_HOST + ATLAN_WORKFLOW_PORT (v2)
  ATLAN_TEMPORAL_NAMESPACE Temporal namespace (default: default)
                           Falls back to ATLAN_WORKFLOW_NAMESPACE (v2)
  ATLAN_TASK_QUEUE         Task queue name (default: {service-name}-queue)
  ATLAN_HANDLER_HOST       Handler bind host (default: 0.0.0.0)
                           Falls back to ATLAN_APP_HTTP_HOST (v2)
  ATLAN_HANDLER_PORT       Handler bind port (default: 8000)
                           Falls back to ATLAN_APP_HTTP_PORT (v2)
  ATLAN_HEALTH_PORT        Worker health check port (default: 8081)
  ATLAN_LOG_LEVEL          Log level (default: INFO)
                           Falls back to LOG_LEVEL (v2)

Examples:
  python -m application_sdk.main --mode worker --app my_package.apps:MyApp
  python -m application_sdk.main --mode handler --app my_package.apps:MyApp --port 9000
  python -m application_sdk.main --mode combined --app my_package.apps:MyApp
        """,
    )

    parser.add_argument(
        "--mode",
        "-m",
        choices=["worker", "handler", "combined"],
        help="Execution mode",
    )
    parser.add_argument(
        "--app",
        "-a",
        help="App module path (e.g., my_package.apps:MyApp)",
    )
    parser.add_argument(
        "--handler",
        help="Optional custom handler module path",
    )

    worker_group = parser.add_argument_group("Worker options")
    worker_group.add_argument("--temporal-host", help="Temporal server address")
    worker_group.add_argument("--temporal-namespace", help="Temporal namespace")
    worker_group.add_argument("--task-queue", help="Task queue name")

    handler_group = parser.add_argument_group("Handler options")
    handler_group.add_argument(
        "--handler-host", "--host", help="Handler bind host (default: 0.0.0.0)"
    )
    handler_group.add_argument(
        "--handler-port", "--port", type=int, help="Handler bind port (default: 8000)"
    )
    handler_group.add_argument(
        "--health-port", type=int, help="Worker health check port (default: 8081)"
    )

    common_group = parser.add_argument_group("Common options")
    common_group.add_argument(
        "--log-level",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Log level",
    )
    common_group.add_argument("--service-name", help="Service name for observability")

    return parser.parse_args()


def main() -> NoReturn:
    """CLI entry point."""
    _install_excepthook()

    args = parse_args()

    try:
        config = AppConfig.from_args_and_env(args)
    except ValueError:
        logger.error("Configuration error", exc_info=True)
        sys.exit(1)

    logger.info(
        "Application SDK starting: mode=%s app=%s service=%s",
        config.mode,
        config.app_module,
        config.service_name,
    )

    try:
        run_main(config)
    except DiscoveryError:
        logger.error("Discovery error", exc_info=True)
        sys.exit(1)
    except KeyboardInterrupt:
        logger.info("Interrupted by user")
    except Exception:
        logger.exception("Fatal error")
        try:
            asyncio.run(_flush_observability())
        except Exception:
            pass
        sys.exit(1)

    sys.exit(0)


if __name__ == "__main__":
    main()
