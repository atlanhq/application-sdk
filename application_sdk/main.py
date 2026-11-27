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

This module owns ``AppConfig``, the run modes, signal handling and the CLI.
What the run modes call lives beside its own layer (FND-4689):

- ``application_sdk.infrastructure._bootstrap`` — ``_create_infrastructure``
  and its Dapr component diagnostics.
- ``application_sdk.execution._temporal.supervisor`` — the worker restart
  supervisor, the fatal-teardown watchdog and the poll-state observer.
- ``application_sdk._shutdown_policy`` — the post-drain exit deadline and the
  event-loop stall bound.
- ``application_sdk.dev._runner`` — ``run_dev_combined``, re-exported here.

Patch those names on the module that defines them when the caller lives
there too; a patch on ``application_sdk.main`` only reaches calls made from
this module.
"""

from __future__ import annotations

from application_sdk.observability.observability import AtlanObservability
from application_sdk.storage.errors import ObjectStorePreflightError

__all__ = [
    "AppConfig",
    "run_dev_combined",
]

import argparse
import asyncio
import faulthandler
import importlib.util as _importlib_util
import os
import signal
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, NoReturn

# The handler surface (application_sdk.handler, .contracts, .errors, …) ships in
# atlan-application-sdk-api, which this SDK depends on at the same version. A pip
# upgrade from a release that predates the split deletes those files when it
# removes the old SDK. Say so plainly instead of failing on the first import.
if _importlib_util.find_spec("application_sdk.handler.base") is None:
    raise ImportError(
        "atlan-application-sdk-api's files are missing (application_sdk.handler "
        "and the rest of the handler surface). This happens after a pip upgrade "
        "from an SDK release that predates atlan-application-sdk-api. Fix: "
        "pip install --force-reinstall --no-deps atlan-application-sdk-api"
    )

from application_sdk._loop_stall_watchdog import (
    LoopStallWatchdog,
)
from application_sdk._shutdown_policy import (
    _install_loop_stall_watchdog,
    _log_exit_deadline_reached,
    _log_shutdown_budget,
    _loop_stall_bound,
    _PostDrainExitArm,
)
from application_sdk.app.base import _pascal_to_kebab
from application_sdk.app.registry import AppRegistry, TaskRegistry
from application_sdk.common import restart_marker
from application_sdk.common._env import env_bool as _env_bool
from application_sdk.common._env import env_int as _env_int
from application_sdk.common._env import env_str as _env_str
from application_sdk.common.task_queue import task_queue_from_env
from application_sdk.dev._runner import run_dev_combined
from application_sdk.discovery import (
    load_app_class,
    load_handler_class,
    validate_app_class,
)
from application_sdk.errors import (
    AppError,
    InvalidInputError,
)
from application_sdk.execution._temporal.supervisor import (
    _FatalTeardownWatchdog,
    _run_worker_with_restart,
)
from application_sdk.infrastructure._bootstrap import _create_infrastructure
from application_sdk.main_errors import (
    MissingAppModuleError,
    MultiAppModuleError,
    UnknownModeError,
)
from application_sdk.observability.logger_adaptor import get_logger
from application_sdk.storage._observability_sink import (
    install_observability_store_sink,
)

# Every process started through this module exports its logs, metrics and
# traces to the object store; observability gets the store from here (FND-4260).
install_observability_store_sink()

# Module-level reference to the running event loop (set in worker/combined mode).
# Used by the SIGUSR1 debug handler to snapshot asyncio tasks.
_worker_event_loop: asyncio.AbstractEventLoop | None = None


def _debug_dump_handler(signum: int, frame: object) -> None:
    """Dump thread stacks and asyncio tasks to /tmp/debug-dump-<pid>.txt on SIGUSR1."""
    dump_path = os.path.join("/tmp", f"debug-dump-{os.getpid()}.txt")
    # conformance: ignore[P050] single-consumer diagnostic dump — an operator reads it after the signal handler returns; no concurrent SDK reader exists for this path
    fd = os.open(dump_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
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
    # conformance: ignore[L005] signal handler must stay async-signal-safe; the logging framework takes locks and can deadlock when invoked from a signal context
    print(f"Debug dump written to {dump_path}", file=sys.stderr, flush=True)


def _install_debug_handlers() -> None:
    """Enable crash tracebacks and the SIGUSR1 debug dump for this process.

    Called from :func:`main` only, so importing this module (dev scripts that
    call :func:`run_dev_combined`, tests) leaves the process's fault and
    signal handlers alone.
    """
    # C-level crashes dump a traceback to stderr.
    faulthandler.enable()
    if hasattr(signal, "SIGUSR1"):
        signal.signal(signal.SIGUSR1, _debug_dump_handler)


def _log_process_memory_baseline() -> None:
    """Log current RSS vs container memory limit at process startup.

    Gives a baseline so that — after an OOM kill — the log of the replacement
    pod shows the limit, and the log of the killed pod shows memory climbing
    toward it via the heartbeat-loop pressure warnings.  The limit is the
    container's cgroup limit, falling back to K8S_POD_MEMORY_LIMIT; no-ops when
    neither is set (local dev / non-Kubernetes environments).
    """
    from application_sdk.observability import (  # noqa: PLC0415 — cold path: startup only
        cgroup as _cgroup,
    )
    from application_sdk.observability import (  # noqa: PLC0415 — cold path: startup only
        resource_sampler as _rs,
    )

    limit_bytes = _cgroup.memory_limit_bytes()
    if not limit_bytes:
        return
    sample = _rs.sample()
    if sample is None:
        return
    logger.info(
        "Process memory at start: %.2f GiB RSS / %.2f GiB limit (%.0f%% of limit)",
        sample.rss_bytes / (1024**3),
        limit_bytes / (1024**3),
        100.0 * sample.rss_bytes / limit_bytes,
    )


if TYPE_CHECKING:
    pass


logger = get_logger(__name__)


@dataclass
class AppConfig:
    """Runtime configuration for app execution.

    ``AppConfig`` is the **authoritative runtime config** passed through the call
    chain to workers, handlers, and the Temporal client. It is constructed after
    CLI argument parsing (``from_args_and_env``) or directly in dev scripts
    (``run_dev_combined``).

    **Relationship with constants.py:**
    Some values also exist as module-level constants (e.g. ``LOG_LEVEL``).
    Those constants serve code that runs at **import time** (observability
    init, logging.basicConfig) — before AppConfig exists. Both AppConfig and
    constants.py read the **same env vars with the same defaults** so they
    stay in sync.

    **Construction paths:**
    - Production: ``main()`` → ``AppConfig.from_args_and_env(args)``
    - Dev (CLI): ``atlan app run`` → same as production
    - Dev (script): ``run_dev_combined(MyApp)`` → ``AppConfig(...)`` directly
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
    frontend_assets_path: str = "app/generated/frontend/static"

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

    # Runtime flags (env-var defaults, overridable per execution mode)
    enable_temporal_core_metrics: bool = True
    """Enable Temporal Runtime's loopback Prometheus endpoint that exposes
    the Rust-core metric set (``temporal_workflow_*``, ``temporal_activity_*``
    etc.). Default ``True`` so combined-mode FastAPI ``/metrics`` can proxy
    these metrics, and so worker-mode's ``TemporalCoreCollector`` can read
    them locally to feed the Pushgateway push. Set to ``False`` in
    ``run_dev_combined()`` to avoid port collisions on hot reload."""

    prometheus_bind_address: str = "127.0.0.1:9464"
    """Loopback bind address for the Temporal Runtime Prometheus endpoint.
    Not externally reachable — only the combined-mode FastAPI ``/metrics``
    proxy and the worker's ``TemporalCoreCollector`` consume it."""

    workflow_max_timeout_hours: int | None = None
    """Maximum workflow execution timeout in hours. When set, passed as
    execution_timeout to Temporal on every /workflows/v1/start call.
    Reads env var ATLAN_WORKFLOW_MAX_TIMEOUT_HOURS. None means no SDK-level
    ceiling (Temporal namespace default applies)."""

    def __post_init__(self) -> None:
        """Derive task_queue from app_module when not explicitly set."""
        if not self.task_queue and self.app_module:
            self.task_queue = _derive_task_queue(self.app_module)

    @classmethod
    def from_args_and_env(cls, args: argparse.Namespace) -> AppConfig:
        """Create config from CLI args with env var fallbacks.

        CLI arguments take precedence over environment variables.
        """

        from application_sdk.constants import (  # noqa: PLC0415 — cold path: lazy access to env-var-derived constants
            _legacy_app_mode,
            _read_auth_enabled,
        )

        def _env(key: str, default: str = "") -> str:
            return _env_str(key, default)

        mode = args.mode or _env("ATLAN_APP_MODE")
        if not mode:
            # v2-compat: APPLICATION_MODE is the deprecated fallback.
            legacy_raw = _env("APPLICATION_MODE")
            legacy_mode = _legacy_app_mode(legacy_raw)
            if legacy_raw.strip() and legacy_mode is None:
                logger.warning(
                    "APPLICATION_MODE=%r is not a recognised mode; running in "
                    "combined mode. Set ATLAN_APP_MODE to worker, handler or "
                    "combined instead",
                    legacy_raw,
                )
            mode = legacy_mode or "combined"

        app_module_raw = args.app or _env("ATLAN_APP_MODULE")
        if not app_module_raw:
            raise MissingAppModuleError()

        app_module = app_module_raw.strip()

        if "," in app_module:
            raise MultiAppModuleError(app_module=app_module)

        service_name = (
            getattr(args, "service_name", None)
            or _env("ATLAN_SERVICE_NAME")
            or _env("OTEL_SERVICE_NAME")
            or _derive_service_name(app_module)
        )

        # v2-compat: remove when all deployments use ATLAN_TEMPORAL_HOST instead.
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
            handler_port=_handler_port_arg
            if (_handler_port_arg := getattr(args, "handler_port", None)) is not None
            else (
                _env_int("ATLAN_HANDLER_PORT", 0)
                or _env_int("ATLAN_APP_HTTP_PORT", 0)
                or 8000
            ),
            log_level=getattr(args, "log_level", None)
            or _env("ATLAN_LOG_LEVEL")
            or _env("LOG_LEVEL", "INFO"),
            health_port=_health_port_arg
            if (_health_port_arg := getattr(args, "health_port", None)) is not None
            else _env_int("ATLAN_HEALTH_PORT", 8081),
            service_name=service_name,
            # TLS
            # v2 compat: ATLAN_WORKFLOW_TLS_ENABLED is the legacy name; charts
            # predating the v3 rename still set it. Fall back to the v2 name
            # only when ATLAN_TEMPORAL_TLS_ENABLED is absent from the
            # environment — an explicit ATLAN_TEMPORAL_TLS_ENABLED=false must
            # still disable TLS even if a stale v2 value is left in place, so
            # use a presence check rather than truthiness for the precedence.
            tls_enabled=(
                _env_bool("ATLAN_TEMPORAL_TLS_ENABLED")
                if "ATLAN_TEMPORAL_TLS_ENABLED" in os.environ
                else _env_bool("ATLAN_WORKFLOW_TLS_ENABLED")
            ),
            tls_server_root_ca_cert_path=_env("ATLAN_TEMPORAL_TLS_CA_CERT_PATH"),
            tls_client_cert_path=_env("ATLAN_TEMPORAL_TLS_CLIENT_CERT_PATH"),
            tls_client_private_key_path=_env("ATLAN_TEMPORAL_TLS_CLIENT_KEY_PATH"),
            tls_domain=_env("ATLAN_TEMPORAL_TLS_DOMAIN"),
            # Auth
            # One reader for both this and constants.AUTH_ENABLED (event auth).
            auth_enabled=_read_auth_enabled(),
            auth_client_id=_env("ATLAN_AUTH_CLIENT_ID"),
            auth_client_secret=_env("ATLAN_AUTH_CLIENT_SECRET"),
            # v2 compat: ATLAN_AUTH_URL is the legacy env var set by older Helm charts.
            # v3 uses ATLAN_AUTH_TOKEN_URL / ATLAN_AUTH_BASE_URL separately.
            auth_token_url=_env("ATLAN_AUTH_TOKEN_URL") or _env("ATLAN_AUTH_URL"),
            auth_base_url=_env("ATLAN_AUTH_BASE_URL") or _env("ATLAN_AUTH_URL"),
            auth_scopes=_env("ATLAN_AUTH_SCOPES"),
            frontend_assets_path=_env(
                "ATLAN_FRONTEND_ASSETS_PATH", "app/generated/frontend/static"
            ),
            # Runtime flags
            enable_temporal_core_metrics=_env_bool(
                "ATLAN_ENABLE_TEMPORAL_CORE_METRICS", default=True
            ),
            prometheus_bind_address=_env(
                "ATLAN_TEMPORAL_PROMETHEUS_BIND_ADDRESS", "127.0.0.1:9464"
            ),
            workflow_max_timeout_hours=_parse_workflow_max_timeout_hours(),
        )


def _derive_service_name(app_module: str) -> str:
    """Convert "my_package.apps:MyApp" to "my-app" (kebab-case)."""
    if ":" in app_module:
        return _pascal_to_kebab(app_module.split(":")[1])
    return "application-sdk"


def _derive_task_queue(app_module: str) -> str:
    """Derive the default task queue name.

    The env-var rule itself lives in
    :func:`application_sdk.common.task_queue.task_queue_from_env` — the same
    function the manifest-serving path consults, so the queue this worker polls
    and the queue AE submits to cannot drift apart (FND-195). Only the
    no-app-name fallback is local to the worker: ``{ClassName}-queue`` predates
    the env-var convention and is load-bearing for local dev, where nothing reads
    the manifest's queue anyway.
    """
    return task_queue_from_env() or f"{_derive_service_name(app_module)}-queue"


def _parse_workflow_max_timeout_hours() -> int | None:
    """Parse ATLAN_WORKFLOW_MAX_TIMEOUT_HOURS, returning None for unset or non-positive values."""
    hours = _env_int("ATLAN_WORKFLOW_MAX_TIMEOUT_HOURS", 0)
    if hours <= 0:
        if hours < 0:
            logger.warning(
                "ATLAN_WORKFLOW_MAX_TIMEOUT_HOURS=%d is non-positive; ignoring.",
                hours,
            )
        return None
    return hours


async def _flush_observability() -> None:
    """Flush all observability buffers before exit."""
    try:
        await AtlanObservability.flush_all()
    except Exception:
        logger.warning(
            "Failed to flush observability buffers on shutdown", exc_info=True
        )

    # CNCT-107: drain the OTLP batch here — while the loop is alive, the network
    # path is still up, and there is grace-period budget left. ``flush_all`` only
    # drains the object-store buffer; the OTLP BatchLogRecordProcessor queue is
    # separate. ``atexit.register(shutdown_otlp_logs)`` remains the last-resort
    # backstop (interpreter exit), not the primary channel. ``flush_otlp_logs``
    # blocks (up to its own timeout bound), so offload it to a thread:
    # this coroutine is also reached via ``loop.create_task`` from
    # ``_loop_exception_handler`` with the loop still live, where a bare blocking
    # call would stall it. Do NOT call ``shutdown_otlp_logs`` here — its one-shot
    # guard would permanently disable every later graceful flush.
    from application_sdk.observability.logger_adaptor import (  # noqa: PLC0415 — cold path: shutdown-only OTLP drain
        flush_otlp_logs,
    )

    try:
        await asyncio.to_thread(flush_otlp_logs)
    except Exception:
        logger.warning("Failed to drain OTLP logs on shutdown", exc_info=True)


# Minimum gap between flushes scheduled by ``_loop_exception_handler``. Each
# flush takes a ``to_thread`` slot and, against an unreachable collector, an
# OTLP flush that only returns at its timeout; an exception storm scheduling one
# per exception would queue them without bound.
_LOOP_HANDLER_FLUSH_MIN_INTERVAL_SECONDS = 5.0
# The handler's flush task, and when it was last scheduled (``time.monotonic``).
_loop_handler_flush_task: asyncio.Task[None] | None = None
_loop_handler_flush_last: float | None = None


def _schedule_loop_handler_flush(loop: asyncio.AbstractEventLoop) -> None:
    """Schedule ``_flush_observability`` unless one is still running or one
    was scheduled within ``_LOOP_HANDLER_FLUSH_MIN_INTERVAL_SECONDS``."""
    global _loop_handler_flush_task, _loop_handler_flush_last
    if _loop_handler_flush_task is not None and not _loop_handler_flush_task.done():
        return
    now = time.monotonic()
    if (
        _loop_handler_flush_last is not None
        and now - _loop_handler_flush_last < _LOOP_HANDLER_FLUSH_MIN_INTERVAL_SECONDS
    ):
        return
    _loop_handler_flush_last = now
    _loop_handler_flush_task = loop.create_task(_flush_observability())


def _loop_exception_handler(
    loop: asyncio.AbstractEventLoop, context: dict[str, Any]
) -> None:
    """Log unhandled asyncio task exceptions and schedule a flush.

    Registered via ``loop.set_exception_handler()`` in each async run mode.
    The loop is still alive when this fires, so ``create_task`` is safe. At
    most one handler flush runs at a time, and a new one starts no sooner than
    ``_LOOP_HANDLER_FLUSH_MIN_INTERVAL_SECONDS`` after the last; the shutdown
    paths flush unconditionally, so nothing skipped here is lost at exit.
    """
    exc = context.get("exception")
    msg = context.get("message", "Unhandled asyncio exception")
    if exc is not None:
        logger.error("Unhandled asyncio task exception: %s", msg, exc_info=exc)
    else:
        logger.error("Asyncio exception (no exception object): %s", msg)
    # Schedule a flush — non-blocking so the loop can continue
    _schedule_loop_handler_flush(loop)
    # Preserve the default stderr output
    loop.default_exception_handler(context)


def _install_excepthook() -> None:
    """Install sys.excepthook to log + flush on uncaught main-thread exceptions.

    Covers crashes that escape main()'s try/except: module-level startup errors,
    BaseException subclasses not caught elsewhere, etc.  Called once at the top
    of main() so it is active for the full process lifetime.
    """
    _orig = sys.excepthook

    def _hook(exc_type, exc_value, exc_traceback):
        logger.error(
            "Unhandled exception — flushing observability before exit",
            exc_info=(exc_type, exc_value, exc_traceback),
        )
        try:
            asyncio.run(_flush_observability())
        # conformance: ignore[E002,E004] best-effort flush in unhandled-exception hook; must never mask the original crash
        except Exception:  # noqa: S110, BLE001 — best-effort flush; must never mask the original crash
            pass
        _orig(exc_type, exc_value, exc_traceback)

    sys.excepthook = _hook


def _install_graceful_signal_handlers(
    loop: asyncio.AbstractEventLoop,
    handler: Callable[[], None],
) -> None:
    """Register SIGINT/SIGTERM handlers, with a fallback for platforms that
    don't support loop.add_signal_handler() (e.g. Windows).

    Wraps the caller's handler so the process-wide worker-shutdown flag is
    set before any caller-specific shutdown logic runs. The activity wrapper
    reads that flag to attribute mid-activity ``asyncio.CancelledError`` to
    pod termination instead of ordinary cancellation.
    """
    from application_sdk.execution.shutdown import (  # noqa: PLC0415 — keep main.py import surface narrow
        mark_worker_shutting_down,
    )

    def _wrapped_handler() -> None:
        mark_worker_shutting_down()
        handler()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _wrapped_handler)
        except (NotImplementedError, OSError):
            # Platforms that don't support ``loop.add_signal_handler`` (e.g.
            # Windows) still need the worker-shutdown flag set so the
            # eviction-retry path attributes mid-activity ``CancelledError``
            # correctly. Drop in a plain ``signal.signal`` fallback that, at
            # minimum, flips the flag — graceful-shutdown event integration
            # is still unavailable in this branch but eviction detection
            # continues to work.
            try:
                signal.signal(sig, lambda *_: mark_worker_shutting_down())
            # conformance: ignore[E002,E014] no add_signal_handler / not main thread; WARNING already logged below
            except (ValueError, OSError):
                pass
            logger.warning(
                "loop.add_signal_handler() not supported on this platform "
                "(signal=%s); graceful shutdown via signals is unavailable",
                sig.name,
                exc_info=True,
            )


async def run_worker_mode(
    config: AppConfig, *, bound_post_drain_exit: bool = False
) -> None:
    """Run in worker mode (Temporal workflow execution).

    Loads the app class, connects to Temporal, creates a worker, and
    runs until a shutdown signal is received.
    ``bound_post_drain_exit`` starts the post-drain exit deadline, which ends
    the whole process if teardown hangs, and the event-loop stall watchdog,
    which ends it if the loop stops running during shutdown. Only the
    process-owning entry point sets it; leave it off when awaiting this from
    code that keeps the process.
    """
    global _worker_event_loop
    _worker_event_loop = asyncio.get_running_loop()

    from application_sdk.execution._temporal.backend import (  # noqa: PLC0415 — cold path: only loaded in worker mode (execution backend)
        create_temporal_client,
    )
    from application_sdk.execution._temporal.converter import (  # noqa: PLC0415 — cold path: only loaded in worker mode (execution backend)
        create_data_converter_for_app,
    )
    from application_sdk.execution._temporal.worker import (  # noqa: PLC0415 — cold path: only loaded in worker mode (execution backend)
        create_worker,
    )
    from application_sdk.infrastructure.context import (  # noqa: PLC0415 — cold path: only when infrastructure init is needed
        set_infrastructure,
    )

    logger.info(
        "Starting worker mode: app=%s temporal=%s queue=%s",
        config.app_module,
        config.temporal_host,
        config.task_queue,
    )

    infra = await _create_infrastructure()
    from application_sdk.storage.preflight import (  # noqa: PLC0415 — cold path: SDR-gated; only runs when ENABLE_ATLAN_UPLOAD=true
        verify_object_store_access,
    )

    await verify_object_store_access(infra)
    set_infrastructure(infra)

    app_class = load_app_class(config.app_module)
    validate_app_class(app_class)
    app_name = app_class._app_name

    logger.info(
        "Loaded app %s version %s",
        app_name,
        app_class._app_version,
    )

    data_converter = create_data_converter_for_app(app_class)

    # Acquire auth token if enabled
    auth_manager: Any = None
    api_key: str | None = None
    if config.auth_enabled:
        from application_sdk.execution._temporal.auth import (  # noqa: PLC0415 — cold path: only loaded in worker mode (execution backend)
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

    async def _connect_temporal(key: str | None) -> Any:
        return await create_temporal_client(
            config.temporal_host,
            config.temporal_namespace,
            data_converter=data_converter,
            api_key=key,
            tls_enabled=config.tls_enabled,
            tls_server_root_ca_cert_path=config.tls_server_root_ca_cert_path,
            tls_client_cert_path=config.tls_client_cert_path,
            tls_client_private_key_path=config.tls_client_private_key_path,
            tls_domain=config.tls_domain,
            enable_prometheus=config.enable_temporal_core_metrics,
            prometheus_bind_address=config.prometheus_bind_address,
        )

    client = await _connect_temporal(api_key)

    if auth_manager is not None:
        auth_manager.start_background_refresh(client)
        logger.info("Background token refresh started")

    # Discover the app's Handler so SDR workflows can be registered on the
    # worker.  When no Handler is found, create_worker binds DefaultHandler so
    # the SDR/gate activities are still registered (the preflight gate is
    # mandatory; DefaultHandler's no-op keeps it non-blocking).
    handler_class_for_sdr = load_handler_class(
        config.app_module,
        handler_module_path=config.handler_module,
    )
    handler_for_sdr = (
        handler_class_for_sdr() if handler_class_for_sdr is not None else None
    )
    if handler_for_sdr is not None:
        logger.info(
            "Loaded handler %s for SDR workflow registration",
            type(handler_for_sdr).__name__,
        )

    from application_sdk.server.health import (  # noqa: PLC0415 — cold path: health server only in worker mode
        build_worker_health_server,
    )

    health_server = build_worker_health_server(port=config.health_port, client=client)

    async def _reconnect() -> None:
        """Rebuild the Temporal client, mirroring what a pod restart does.

        ``_build_worker`` closes over ``client``, so rebinding it here is what
        makes the next rebuild poll on a fresh channel instead of the poisoned
        one. Re-points the auth refresh loop and health probes at the new
        client too. Connect first so a failed reconnect leaves the existing
        refresh loop running.
        """
        from application_sdk.execution._temporal.auth import (  # noqa: PLC0415 — cold path: only on supervised restart
            reconnect_temporal_client,
        )

        nonlocal client, api_key
        client, api_key = await reconnect_temporal_client(
            connect=_connect_temporal,
            api_key=api_key,
            auth_manager=auth_manager,
            health_server=health_server,
        )
        logger.info("Reconnected to Temporal before worker restart")

    # Worker-only mode pushes metrics to a Pushgateway since the process has
    # no /metrics endpoint to scrape. Combined mode (run_combined_mode below)
    # leaves enable_pushgateway=False so the FastAPI /metrics endpoint
    # exposes everything via in-process proxy.
    teardown_watchdog = _FatalTeardownWatchdog.from_settings()

    def _on_worker_fatal(exc: BaseException) -> None:
        health_server.record_worker_fatal(exc)
        teardown_watchdog.arm()

    def _build_worker() -> Any:
        # Rebuilt on each supervisor restart — Worker instances are single-use.
        # on_activity feeds the health server's liveness window (BLDX-1552): the
        # /live probe can then observe a silently stalled poll loop that the
        # restart supervisor cannot (it only fires when run() returns/raises).
        return create_worker(
            client,
            task_queue=config.task_queue,
            handler=handler_for_sdr,
            enable_pushgateway=True,
            on_activity=health_server.record_activity,
            on_fatal_error=_on_worker_fatal,
            on_drained=post_drain_arm.on_worker_drained,
        )

    # Log registrations
    for registered_app in AppRegistry.get_instance().list_apps():
        app_meta = AppRegistry.get_instance().get(registered_app)
        # conformance: ignore[L006] this worker registers a small, statically-configured set of apps (typically one); production logs are collected at INFO floor, so demoting this to DEBUG would delete it from observability entirely
        logger.info("Registered app %s version %s", registered_app, app_meta.version)

    for registered_app, tasks in TaskRegistry.get_instance().get_all_tasks().items():
        for task_meta in tasks:
            logger.debug(
                "Registered task %s for app %s", task_meta.name, registered_app
            )

    # Graceful shutdown
    shutdown_event = asyncio.Event()

    # Like the post-drain deadline it hands over to, the watchdog ends the
    # whole process, so only the process-owning entry point runs it.
    loop_stall = _loop_stall_bound()
    stall_watchdog = (
        LoopStallWatchdog(
            loop_stall.seconds,
            loop_stall.exit_by_seconds,
            on_exit_deadline=_log_exit_deadline_reached,
        )
        if bound_post_drain_exit and loop_stall.seconds > 0
        else None
    )

    def _signal_handler() -> None:
        logger.info("Shutdown signal received")
        if stall_watchdog is not None:
            stall_watchdog.start_rearming()
        shutdown_event.set()

    # Read by _build_worker's on_drained hook, which only runs after this point.
    post_drain_arm = _PostDrainExitArm(
        enabled=bound_post_drain_exit,
        shutdown_event=shutdown_event,
        stall_watchdog=stall_watchdog,
    )
    _log_shutdown_budget(loop_stall if bound_post_drain_exit else None)

    loop = asyncio.get_running_loop()
    loop.set_exception_handler(_loop_exception_handler)
    _install_graceful_signal_handlers(loop, _signal_handler)
    if stall_watchdog is not None:
        _install_loop_stall_watchdog(stall_watchdog)

    logger.info("Worker started: app=%s queue=%s", app_name, config.task_queue)
    _log_process_memory_baseline()
    # health_server stays up across worker restarts so the runtime keeps
    # answering health checks while the supervisor rebuilds a crashed worker.
    async with health_server:
        # A container that restarted in place after running out of memory comes
        # back on the limit that killed it, so polling now would take the work
        # straight back onto a pod that cannot hold it. This has to run before
        # anything builds a worker, which is why it is here and not in the
        # supervisor.
        await restart_marker.wait_if_pod_restarted(shutdown_event)
        await _run_worker_with_restart(
            build_worker=_build_worker,
            shutdown_event=shutdown_event,
            auth_manager=auth_manager,
            client=client,
            health_server=health_server,
            reconnect=_reconnect,
            teardown_watchdog=teardown_watchdog,
        )
        # Reached only when the worker drained on request. Anything that escapes
        # leaves the marker in place, which is what makes the next start a restart.
        restart_marker.clear()

    # Normally already armed by the worker's on_drained hook; idempotent.
    post_drain_arm.start()

    from application_sdk.infrastructure.context import (  # noqa: PLC0415 — cold path: only when infrastructure init is needed
        close_infrastructure,
    )

    await close_infrastructure()
    await _flush_observability()
    if auth_manager is not None:
        await auth_manager.shutdown()
        logger.info("Auth manager stopped")

    logger.info("Worker stopped")


def run_handler_mode(config: AppConfig) -> None:
    """Run in handler mode (HTTP FastAPI server).

    Synchronous entry point: runs :func:`_run_handler_mode` in a single
    ``asyncio.run`` so the shared Dapr client is created, used by requests,
    and closed on the same event loop.
    """
    asyncio.run(_run_handler_mode(config))


async def _run_handler_mode(config: AppConfig) -> None:
    """Handler mode on one event loop: build, serve, close, flush.

    The shared ``AsyncDaprClient`` keeps pooled keep-alive connections bound
    to the loop that opened them. Creating infrastructure, serving requests
    and closing the client must therefore all happen on this one loop, as
    worker and combined mode already do. Splitting them across separate
    ``asyncio.run`` calls makes the first Dapr call after start and the
    shutdown ``close_infrastructure()`` fail with "Event loop is closed".
    """
    import uvicorn  # noqa: PLC0415 — cold path: uvicorn only loaded in worker/handler runtime modes

    from application_sdk.execution._temporal.converter import (  # noqa: PLC0415 — cold path: only loaded in worker mode (execution backend)
        create_data_converter_for_app,
    )
    from application_sdk.handler import (  # noqa: PLC0415 — cold path: only loaded in handler mode
        DefaultHandler,
        create_app_handler_service,
    )
    from application_sdk.infrastructure.context import (  # noqa: PLC0415 — cold path: only when infrastructure init is needed
        close_infrastructure,
        set_infrastructure,
    )
    from application_sdk.storage.preflight import (  # noqa: PLC0415 — cold path: SDR-gated; only runs when ENABLE_ATLAN_UPLOAD=true
        verify_object_store_access,
    )

    infra = await _create_infrastructure()
    await verify_object_store_access(infra)
    set_infrastructure(infra)
    try:
        logger.info(
            "Starting handler mode: app=%s host=%s port=%d",
            config.app_module,
            config.handler_host,
            config.handler_port,
        )
        _log_process_memory_baseline()

        app_class = load_app_class(config.app_module)
        validate_app_class(app_class)
        app_name = app_class._app_name

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
            enable_temporal_core_metrics=False,
            prometheus_bind_address=config.prometheus_bind_address,
            secret_store=infra.secret_store,
            storage=infra.storage,
            frontend_assets_path=config.frontend_assets_path,
            workflow_max_timeout_hours=config.workflow_max_timeout_hours,
        )

        uvicorn_server = uvicorn.Server(
            uvicorn.Config(
                fastapi_app,
                host=config.handler_host,
                port=config.handler_port,
                log_level=config.log_level.lower(),
                # Same as combined mode: skip uvicorn's dictConfig() so its
                # logs flow through the root logger to the SDK's sink.
                log_config=None,
            )
        )
        await uvicorn_server.serve()
    finally:
        try:
            await close_infrastructure()
        finally:
            await _flush_observability()


async def run_combined_mode(
    config: AppConfig, *, bound_post_drain_exit: bool = False
) -> None:
    """Run worker + handler in a single process (SDR / docker-compose mode).

    Combines Temporal worker execution with the HTTP handler service,
    enabling single-container deployment. Both components share the same
    event loop and shut down together on SIGINT/SIGTERM.
    ``bound_post_drain_exit`` starts the post-drain exit deadline, which ends
    the whole process if teardown hangs, and the event-loop stall watchdog,
    which ends it if the loop stops running during shutdown. Only the
    process-owning entry point sets it; leave it off when awaiting this from
    code that keeps the process.
    """
    global _worker_event_loop
    _worker_event_loop = asyncio.get_running_loop()

    import uvicorn  # noqa: PLC0415 — cold path: uvicorn only loaded in worker/handler runtime modes

    from application_sdk.execution._temporal.backend import (  # noqa: PLC0415 — cold path: only loaded in worker mode (execution backend)
        create_temporal_client,
    )
    from application_sdk.execution._temporal.converter import (  # noqa: PLC0415 — cold path: only loaded in worker mode (execution backend)
        create_data_converter_for_app,
    )
    from application_sdk.execution._temporal.worker import (  # noqa: PLC0415 — cold path: only loaded in worker mode (execution backend)
        create_worker,
    )
    from application_sdk.handler import (  # noqa: PLC0415 — cold path: only loaded in handler mode
        DefaultHandler,
        create_app_handler_service,
    )
    from application_sdk.infrastructure.context import (  # noqa: PLC0415 — cold path: only when infrastructure init is needed
        get_infrastructure,
        set_infrastructure,
    )

    # Only create infrastructure if not already set (e.g., by run_dev_combined)
    _existing_infra = get_infrastructure()
    if _existing_infra is None:
        infra = await _create_infrastructure()
        from application_sdk.storage.preflight import (  # noqa: PLC0415 — cold path: SDR-gated; only runs when ENABLE_ATLAN_UPLOAD=true
            verify_object_store_access,
        )

        await verify_object_store_access(infra)
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
    app_name = app_class._app_name

    logger.info(
        "Loaded app %s version %s",
        app_name,
        app_class._app_version,
    )

    data_converter = create_data_converter_for_app(app_class)

    auth_manager: Any = None
    api_key: str | None = None
    if config.auth_enabled:
        from application_sdk.execution._temporal.auth import (  # noqa: PLC0415 — cold path: only loaded in worker mode (execution backend)
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

    async def _connect_temporal(key: str | None) -> Any:
        return await create_temporal_client(
            config.temporal_host,
            config.temporal_namespace,
            data_converter=data_converter,
            api_key=key,
            tls_enabled=config.tls_enabled,
            tls_server_root_ca_cert_path=config.tls_server_root_ca_cert_path,
            tls_client_cert_path=config.tls_client_cert_path,
            tls_client_private_key_path=config.tls_client_private_key_path,
            tls_domain=config.tls_domain,
            enable_prometheus=config.enable_temporal_core_metrics,
            prometheus_bind_address=config.prometheus_bind_address,
        )

    client = await _connect_temporal(api_key)

    if auth_manager is not None:
        auth_manager.start_background_refresh(client)
        logger.info("Background token refresh started")

    # Discover the handler before building the worker so the same instance
    # serves both the HTTP service and the SDR Temporal workflows.
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

    from application_sdk.server.health import (  # noqa: PLC0415 — cold path: health server only in combined mode
        build_worker_health_server,
    )

    health_server = build_worker_health_server(port=config.health_port, client=client)

    async def _reconnect() -> None:
        """Rebuild the Temporal client, mirroring what a pod restart does.

        ``_build_worker`` closes over ``client``, so rebinding it here is what
        makes the next rebuild poll on a fresh channel instead of the poisoned
        one. Re-points the auth refresh loop and health probes at the new
        client too. Connect first so a failed reconnect leaves the existing
        refresh loop running.
        """
        from application_sdk.execution._temporal.auth import (  # noqa: PLC0415 — cold path: only on supervised restart
            reconnect_temporal_client,
        )

        nonlocal client, api_key
        client, api_key = await reconnect_temporal_client(
            connect=_connect_temporal,
            api_key=api_key,
            auth_manager=auth_manager,
            health_server=health_server,
        )
        logger.info("Reconnected to Temporal before worker restart")

    teardown_watchdog = _FatalTeardownWatchdog.from_settings()

    def _on_worker_fatal(exc: BaseException) -> None:
        health_server.record_worker_fatal(exc)
        teardown_watchdog.arm()

    def _build_worker() -> Any:
        # Rebuilt on each supervisor restart — Worker instances are single-use.
        # on_activity feeds the /live liveness window (BLDX-1552).
        return create_worker(
            client,
            task_queue=config.task_queue,
            handler=handler,
            on_activity=health_server.record_activity,
            on_fatal_error=_on_worker_fatal,
            on_drained=post_drain_arm.on_worker_drained,
        )

    for registered_app in AppRegistry.get_instance().list_apps():
        app_meta = AppRegistry.get_instance().get(registered_app)
        # conformance: ignore[L006] this worker registers a small, statically-configured set of apps (typically one); production logs are collected at INFO floor, so demoting this to DEBUG would delete it from observability entirely
        logger.info("Registered app %s version %s", registered_app, app_meta.version)

    for registered_app, tasks in TaskRegistry.get_instance().get_all_tasks().items():
        for task_meta in tasks:
            logger.debug(
                "Registered task %s for app %s", task_meta.name, registered_app
            )

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
        enable_temporal_core_metrics=config.enable_temporal_core_metrics,
        prometheus_bind_address=config.prometheus_bind_address,
        workflow_max_timeout_hours=config.workflow_max_timeout_hours,
        secret_store=infra.secret_store,
        storage=infra.storage,
        frontend_assets_path=config.frontend_assets_path,
        worker_health=health_server,
    )

    uvicorn_server = uvicorn.Server(
        uvicorn.Config(
            fastapi_app,
            host=config.handler_host,
            port=config.handler_port,
            log_level=config.log_level.lower(),
            # Skip uvicorn's logging.config.dictConfig() call — it can deadlock
            # with background gRPC threads from the Temporal SDK on Windows.
            # Uvicorn logs still flow through Python's root logger to our
            # structlog/loguru setup via the installed InterceptHandler.
            log_config=None,
        )
    )

    shutdown_event = asyncio.Event()

    # Like the post-drain deadline it hands over to, the watchdog ends the
    # whole process, so only the process-owning entry point runs it.
    loop_stall = _loop_stall_bound()
    stall_watchdog = (
        LoopStallWatchdog(
            loop_stall.seconds,
            loop_stall.exit_by_seconds,
            on_exit_deadline=_log_exit_deadline_reached,
        )
        if bound_post_drain_exit and loop_stall.seconds > 0
        else None
    )

    def _signal_handler() -> None:
        logger.info("Shutdown signal received")
        if stall_watchdog is not None:
            stall_watchdog.start_rearming()
        shutdown_event.set()
        uvicorn_server.should_exit = True

    # Read by _build_worker's on_drained hook, which only runs after this point.
    post_drain_arm = _PostDrainExitArm(
        enabled=bound_post_drain_exit,
        shutdown_event=shutdown_event,
        stall_watchdog=stall_watchdog,
    )
    _log_shutdown_budget(loop_stall if bound_post_drain_exit else None)

    loop = asyncio.get_running_loop()
    loop.set_exception_handler(_loop_exception_handler)
    _install_graceful_signal_handlers(loop, _signal_handler)
    if stall_watchdog is not None:
        _install_loop_stall_watchdog(stall_watchdog)

    logger.info(
        "Combined mode started: app=%s queue=%s port=%d",
        app_name,
        config.task_queue,
        config.handler_port,
    )
    _log_process_memory_baseline()
    if stall_watchdog is not None:
        # While serving, uvicorn replaces the process-level SIGTERM and SIGINT
        # handlers with its own handle_exit, so the arming has to go there too.
        uvicorn_handle_exit = uvicorn_server.handle_exit

        def _handle_exit(sig: int, frame: Any) -> None:
            stall_watchdog.on_shutdown_signal()
            uvicorn_handle_exit(sig, frame)

        uvicorn_server.handle_exit = _handle_exit
    # uvicorn keeps serving while the worker is supervised and restarted
    # independently; a shutdown signal (which also sets should_exit) stops both.
    async with health_server:
        await asyncio.gather(
            uvicorn_server.serve(),
            _run_worker_with_restart(
                build_worker=_build_worker,
                shutdown_event=shutdown_event,
                auth_manager=auth_manager,
                client=client,
                health_server=health_server,
                reconnect=_reconnect,
                teardown_watchdog=teardown_watchdog,
            ),
        )

    # Normally already armed by the worker's on_drained hook; idempotent.
    post_drain_arm.start()

    from application_sdk.infrastructure.context import (  # noqa: PLC0415 — cold path: only when infrastructure init is needed
        close_infrastructure,
    )

    await close_infrastructure()
    await _flush_observability()
    if auth_manager is not None:
        await auth_manager.shutdown()
        logger.info("Auth manager stopped")

    logger.info("Combined mode stopped")


def run_main(config: AppConfig) -> None:
    """Route to worker, handler, or combined mode based on config."""
    from application_sdk.common.env_warnings import (  # noqa: PLC0415 — cold path: startup-only check
        warn_removed_env_vars,
    )

    warn_removed_env_vars()

    # Bootstrap the global MeterProvider once per process so any meter
    # consumer (instrumentors, interceptors, decorators, …) resolves to the
    # configured provider. Without this, handler-mode /metrics serves only
    # the prometheus_client defaults.
    from application_sdk.observability.metrics_adaptor import (  # noqa: PLC0415 — cold path: meter provider bootstrap at process start
        get_metrics,
    )

    get_metrics()

    # Same for the global TracerProvider: Temporal's TracingInterceptor emits
    # through it, so without this ATLAN_ENABLE_OTLP_TRACES=true exports no
    # worker spans. Gated here so the OTLP/gRPC exporter is not imported when
    # tracing is off.
    from application_sdk.constants import (  # noqa: PLC0415 — cold path: lazy access to env-var-derived constants
        ENABLE_OTLP_TRACES,
    )

    if ENABLE_OTLP_TRACES:
        from application_sdk.observability.traces_adaptor import (  # noqa: PLC0415 — cold path: tracer provider bootstrap at process start
            setup_tracer_provider,
        )

        setup_tracer_provider()

    if config.mode == "worker":
        asyncio.run(run_worker_mode(config, bound_post_drain_exit=True))
    elif config.mode == "handler":
        run_handler_mode(config)
    elif config.mode == "combined":
        asyncio.run(run_combined_mode(config, bound_post_drain_exit=True))
    else:
        raise UnknownModeError(received_mode=config.mode)


def parse_args() -> argparse.Namespace:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description="Application SDK — run apps in worker, handler, or combined mode",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Environment Variables:
  ATLAN_APP_MODE           Execution mode (worker, handler, or combined)
  ATLAN_APP_MODULE         App class path in 'module:ClassName' form (e.g. app.main:MyApp)
                           Apps expose multiple workflows via @entrypoint methods
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
  ATLAN_WORKER_LIVENESS_MAX_IDLE_SECONDS
                           Optional /live idle window in seconds (default: 0 = disabled).
                           When >0, /live fails if no worker activity within the window.
                           Enable only for continuously-busy queues; a positive value
                           false-positives on legitimately idle queues.
  ATLAN_WORKER_LIVENESS_MAX_ZERO_POLLER_READINGS
                           Consecutive zero-poller readings before /live fails
                           (default: 5; 0 = disabled).
  ATLAN_WORKER_LIVENESS_FATAL_RECOVERY_SECONDS
                           Seconds a fatal poll error may go without the worker
                           polling again before /live fails (default: 600; 0 = disabled).
  ATLAN_WORKER_LOOP_STALL_TIMEOUT_SECONDS
                           After SIGTERM/SIGINT, seconds the event loop may go without
                           running before the process dumps every thread's stack and
                           exits with code 1 (default: derived from the registered
                           tasks' heartbeat timeouts and the shutdown budget;
                           0 = disabled).
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
        help="App class path in 'module:ClassName' form (e.g. app.main:MyApp). Use @entrypoint methods to expose multiple workflows from one App.",
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
    _install_debug_handlers()
    _install_excepthook()

    args = parse_args()

    try:
        config = AppConfig.from_args_and_env(args)
    except (ValueError, AppError):
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
    except InvalidInputError:
        logger.error("Discovery error", exc_info=True)
        sys.exit(1)
    except KeyboardInterrupt:
        logger.info("Interrupted by user")
    except Exception as exc:
        if isinstance(exc, ObjectStorePreflightError):
            logger.error(
                "SDR object-store preflight failed — cannot start:\n%s",
                exc,
                exc_info=True,
            )
            try:
                asyncio.run(_flush_observability())
            # conformance: ignore[E002,E004] best-effort flush on fatal exit; fatal error already logged above
            except Exception:  # noqa: S110, BLE001 — best-effort flush on fatal exit; error already logged above
                pass
            sys.exit(1)
        logger.error("Fatal error", exc_info=True)
        try:
            asyncio.run(_flush_observability())
        # conformance: ignore[E002,E004] best-effort flush on fatal exit; fatal error already logged above
        except Exception:  # noqa: S110, BLE001 — best-effort flush on fatal exit; error already logged above
            pass
        sys.exit(1)

    sys.exit(0)


if __name__ == "__main__":
    main()
