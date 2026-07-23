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

__all__ = [
    "AppConfig",
    "run_dev_combined",
]

import argparse
import asyncio
import faulthandler
import os
import random
import signal
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, NoReturn

import orjson

from application_sdk.common._env import env_int as _env_int
from application_sdk.discovery import (
    load_app_class,
    load_handler_class,
    validate_app_class,
)
from application_sdk.errors import AppError, InvalidInputError
from application_sdk.main_errors import (
    DaprNotDetectedError,
    MissingAppModuleError,
    MultiAppModuleError,
    UnknownModeError,
)
from application_sdk.observability.logger_adaptor import get_logger

# Enable faulthandler so C-level crashes dump a traceback to stderr.
faulthandler.enable()

# Module-level reference to the running event loop (set in worker/combined mode).
# Used by the SIGUSR1 debug handler to snapshot asyncio tasks.
_worker_event_loop: asyncio.AbstractEventLoop | None = None


def _debug_dump_handler(signum: int, frame: object) -> None:
    """Dump thread stacks and asyncio tasks to /tmp/debug-dump-<pid>.txt on SIGUSR1."""
    dump_path = os.path.join("/tmp", f"debug-dump-{os.getpid()}.txt")
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
    # conformance: ignore[L005] signal handler must stay async-signal-safe; the logging framework takes locks and can deadlock when invoked from a signal context
    print(f"Debug dump written to {dump_path}", file=sys.stderr, flush=True)


if hasattr(signal, "SIGUSR1"):
    signal.signal(signal.SIGUSR1, _debug_dump_handler)


def _log_process_memory_baseline() -> None:
    """Log current RSS vs container memory limit at process startup.

    Gives a baseline so that — after an OOM kill — the log of the replacement
    pod shows the limit, and the log of the killed pod shows memory climbing
    toward it via the heartbeat-loop pressure warnings.  No-ops when
    K8S_POD_MEMORY_LIMIT is unset (local dev / non-Kubernetes environments).
    """
    from application_sdk.observability import (  # noqa: PLC0415 — cold path: startup only
        resource_sampler as _rs,
    )
    from application_sdk.observability.resource_sampler import (  # noqa: PLC0415
        parse_pod_memory_limit,
    )

    limit_bytes = parse_pod_memory_limit(os.environ.get("K8S_POD_MEMORY_LIMIT", ""))
    if limit_bytes <= 0:
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
    from collections.abc import Callable, Mapping
    from pathlib import Path

    from application_sdk.app.base import App
    from application_sdk.infrastructure._dapr.http import AsyncDaprClient
    from application_sdk.infrastructure.context import InfrastructureContext
    from application_sdk.infrastructure.secrets import SecretStore


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

    enable_mcp: bool = False
    """Enable Model Context Protocol (MCP) server.
    Reads same env var as constants.ENABLE_MCP (ENABLE_MCP)."""

    max_concurrent_storage_transfers: int = 4
    """Maximum concurrent object-store uploads/downloads.
    Reads same env var as constants.MAX_CONCURRENT_STORAGE_TRANSFERS."""

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

        def _env(key: str, default: str = "") -> str:
            return os.environ.get(key, default)

        def _env_bool(key: str, default: bool = False) -> bool:
            val = os.environ.get(key, "").lower()
            return val in ("true", "1", "yes") if val else default

        _legacy_mode_map = {
            "WORKER": "worker",
            "SERVER": "handler",
            "LOCAL": "combined",
        }
        mode = (
            args.mode
            or _env("ATLAN_APP_MODE")
            or _legacy_mode_map.get(_env("APPLICATION_MODE"), "")
            or "combined"
        )

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
            auth_enabled=_env_bool("ATLAN_AUTH_ENABLED"),
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
            enable_mcp=_env_bool("ENABLE_MCP"),
            max_concurrent_storage_transfers=_env_int(
                "ATLAN_MAX_CONCURRENT_STORAGE_TRANSFERS", 4
            ),
            workflow_max_timeout_hours=_parse_workflow_max_timeout_hours(),
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


def _parse_all_component_yamls(components_dir: Path) -> dict[str, dict[str, str]]:
    """Parse all Dapr component YAML files and return safe metadata per component.

    Returns a mapping of component name → dict of allowlisted metadata values.
    Non-allowlisted keys (secrets, connection strings, credentials) are never included.
    Silently returns an empty dict on any parse error.
    """
    import yaml  # noqa: PLC0415 — cold path: yaml only when reading dapr binding YAML

    from application_sdk.storage.binding import (  # noqa: PLC0415 — cold path: storage init only when binding YAML present
        _parse_dapr_metadata,
    )

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


async def _log_dapr_components(
    dapr_client: AsyncDaprClient,
    components_dir: Path,
) -> set[str]:
    """Log registered Dapr components and their safe configuration at startup.

    Queries the Dapr sidecar metadata API for all registered components and
    emits one INFO log line per component showing its type, version, and any
    non-sensitive metadata from the corresponding component YAML.

    Then emits one outcome line per *expected* binding (state store, secret
    store, deployment object store, event binding), classifying it against the
    ``ATLAN_ENABLE_*`` env vars the chart derives from ``atlan.yaml``'s
    ``deploy.dapr`` block:

    * registered + declared enabled / unset → INFO ("accepted")
    * registered + declared disabled        → INFO ("drift")
    * missing    + declared enabled         → WARNING ("declared but missing")
    * missing    + declared disabled        → INFO ("disabled by config")
    * missing    + declared unset           → INFO ("legacy/unknown intent")

    Every binding always produces a log line so operators can see, with a
    reason, how each one was handled. This is best-effort: any failure to
    query Dapr metadata is logged as a WARNING and never blocks startup.

    Returns:
        Set of registered component names. Empty set if metadata query fails.
    """
    from application_sdk.constants import (  # noqa: PLC0415 — cold path: lazy access to env-var-derived constants
        DEPLOYMENT_OBJECT_STORE_NAME,
        EVENT_STORE_NAME,
        SECRET_STORE_NAME,
        STATE_STORE_NAME,
        _read_enable_tri_state,
    )

    try:
        metadata = await dapr_client.get_metadata()
    except Exception:
        logger.warning(
            "Could not query Dapr metadata — component diagnostics unavailable; "
            "optional components (e.g. event binding) will be disabled",
            exc_info=True,
        )
        return set()

    # Dapr 1.13: "registeredComponents", Dapr 1.14+: "components"
    raw_components = metadata.get(
        "components", metadata.get("registeredComponents", [])
    )
    registered = {c["name"]: c for c in raw_components if "name" in c}
    yaml_details = _parse_all_component_yamls(components_dir)

    for name, comp in registered.items():
        safe_meta = yaml_details.get(name, {})
        if safe_meta:
            detail = ", ".join("%s=%s" % (k, v) for k, v in safe_meta.items())
            # conformance: ignore[L006] one-time startup enumeration over discovered dapr components (small, bounded set), not a hot loop; an existing test pins INFO
            logger.info(
                "Dapr component: %s (type=%s, version=%s) — %s",
                name,
                comp.get("type", "unknown"),
                comp.get("version", "unknown"),
                detail,
            )
        else:
            # conformance: ignore[L006] one-time startup enumeration over discovered dapr components (small, bounded set), not a hot loop
            logger.info(
                "Dapr component: %s (type=%s, version=%s)",
                name,
                comp.get("type", "unknown"),
                comp.get("version", "unknown"),
            )

    # Each expected binding is keyed off the ATLAN_ENABLE_* env var the chart
    # derives from atlan.yaml's deploy.dapr block. Tuple shape:
    # (component_name, role_label, enable_env_var, atlan_yaml_key)
    expected: tuple[tuple[str, str, str, str], ...] = (
        (STATE_STORE_NAME, "state_store", "ATLAN_ENABLE_STATESTORE", "statestore"),
        (SECRET_STORE_NAME, "secret_store", "ATLAN_ENABLE_SECRETSTORE", "secretstore"),
        (
            DEPLOYMENT_OBJECT_STORE_NAME,
            "object_store",
            "ATLAN_ENABLE_OBJECTSTORE",
            "objectstore",
        ),
        (EVENT_STORE_NAME, "event_binding", "ATLAN_ENABLE_EVENTSTORE", "eventstore"),
    )
    for comp_name, role, enable_var, yaml_key in expected:
        raw, declared = _read_enable_tri_state(enable_var)
        is_registered = comp_name in registered

        if is_registered and declared is not False:
            # conformance: ignore[L006] one-time startup validation over the fixed 4-element `expected` tuple, not a hot loop
            logger.info(
                "Dapr binding %s (role=%s) accepted: registered in sidecar (%s=%s)",
                comp_name,
                role,
                enable_var,
                raw if raw is not None else "<unset>",
            )
        elif is_registered and declared is False:
            # conformance: ignore[L006] one-time startup validation over the fixed 4-element `expected` tuple, not a hot loop
            logger.info(
                "Dapr binding %s (role=%s) registered in sidecar despite %s=false — "
                "possible drift between atlan.yaml deploy.dapr.%s and the deployed chart values",
                comp_name,
                role,
                enable_var,
                yaml_key,
            )
        elif not is_registered and declared is True:
            logger.warning(
                "Dapr binding %s (role=%s) declared enabled (%s=true) but not registered in sidecar — "
                "runtime calls using this component will fail; check chart values and Dapr component templates",
                comp_name,
                role,
                enable_var,
            )
        elif not is_registered and declared is False:
            # conformance: ignore[L006] one-time startup validation over the fixed 4-element `expected` tuple, not a hot loop
            logger.info(
                "Dapr binding %s (role=%s) disabled by config (%s=false); not registered in sidecar (expected)",
                comp_name,
                role,
                enable_var,
            )
        else:  # not registered, declared is None
            # conformance: ignore[L006] one-time startup validation over the fixed 4-element `expected` tuple, not a hot loop
            logger.info(
                "Dapr binding %s (role=%s) not registered in sidecar and %s is unset — "
                "the SDK cannot determine whether your app needs it; "
                "set deploy.dapr.%s in atlan.yaml (true or false) to silence this",
                comp_name,
                role,
                enable_var,
                yaml_key,
            )

    return set(registered)


async def _create_infrastructure(
    credential_stores: Mapping[str, SecretStore] | None = None,
) -> InfrastructureContext:
    """Create infrastructure services based on environment.

    If ``DAPR_HTTP_PORT`` is set (Dapr sidecar present), creates Dapr-backed
    implementations. Otherwise raises ``RuntimeError`` — the Dapr sidecar is
    required for all runtime modes.

    Args:
        credential_stores: Optional mapping of store name → SecretStore.
            Reserved for future use; currently unused.

    Returns:
        Configured InfrastructureContext.

    Raises:
        RuntimeError: If DAPR_HTTP_PORT is not set (no Dapr sidecar).
    """
    from application_sdk.infrastructure.context import (  # noqa: PLC0415 — cold path: only when infrastructure init is needed
        InfrastructureContext,
    )

    if os.environ.get("DAPR_HTTP_PORT"):
        from pathlib import (  # noqa: PLC0415 — cold path: lazy load for entry-point function
            Path,
        )

        from application_sdk.constants import (  # noqa: PLC0415 — cold path: lazy access to env-var-derived constants
            DEPLOYMENT_OBJECT_STORE_NAME,
            ENABLE_ATLAN_UPLOAD,
            EVENT_STORE_NAME,
            SECRET_STORE_NAME,
            STATE_STORE_NAME,
            UPSTREAM_OBJECT_STORE_NAME,
        )
        from application_sdk.infrastructure._dapr.client import (  # noqa: PLC0415 — cold path: only when infrastructure init is needed
            DaprBinding,
            DaprSecretStore,
            DaprStateStore,
        )
        from application_sdk.infrastructure._dapr.http import (  # noqa: PLC0415 — cold path: only when infrastructure init is needed
            AsyncDaprClient,
            wait_for_dapr_sidecar,
        )
        from application_sdk.storage import (  # noqa: PLC0415 — cold path: storage init only when binding YAML present
            create_store_from_binding_with_put_attrs,
        )
        from application_sdk.storage.binding import (  # noqa: PLC0415 — cold path: private helper used only inside _create_infrastructure
            _create_store_from_binding_optional_with_put_attrs,
        )

        await wait_for_dapr_sidecar()
        dapr_client = AsyncDaprClient()
        components_dir = Path(os.environ.get("DAPR_COMPONENTS_PATH", "./components"))
        registered_components = await _log_dapr_components(dapr_client, components_dir)
        logger.info("Dapr sidecar detected — using Dapr infrastructure")

        upstream_storage, upstream_put_attrs = (
            _create_store_from_binding_optional_with_put_attrs(
                UPSTREAM_OBJECT_STORE_NAME,
                components_dir=components_dir,
                required=ENABLE_ATLAN_UPLOAD,
            )
        )

        deployment_store, deployment_put_attrs = (
            create_store_from_binding_with_put_attrs(
                DEPLOYMENT_OBJECT_STORE_NAME,
                components_dir=components_dir,
            )
        )
        return InfrastructureContext(
            state_store=DaprStateStore(dapr_client, store_name=STATE_STORE_NAME),
            secret_store=DaprSecretStore(dapr_client, store_name=SECRET_STORE_NAME),
            storage=deployment_store,
            storage_put_attributes=deployment_put_attrs,
            upstream_storage=upstream_storage,
            upstream_storage_put_attributes=upstream_put_attrs,
            event_binding=(
                DaprBinding(dapr_client, EVENT_STORE_NAME)
                if EVENT_STORE_NAME in registered_components
                else None
            ),
            _dapr_client=dapr_client,
        )
    else:
        raise DaprNotDetectedError()


def _derive_service_name(app_module: str) -> str:
    """Convert "my_package.apps:MyApp" to "my-app" (kebab-case)."""
    if ":" in app_module:
        from application_sdk.app.base import (  # noqa: PLC0415 — circular: app.* imports from main.py via _pascal_to_kebab
            _pascal_to_kebab,
        )

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


def _build_dev_config(
    app_module: str,
    *,
    host: str | None = None,
    port: int | None = None,
    temporal_host: str | None = None,
    temporal_namespace: str | None = None,
    task_queue: str | None = None,
) -> AppConfig:
    """Build an :class:`AppConfig` for the dev-combined path.

    Routes through :meth:`AppConfig.from_args_and_env` so env-var reading is
    not duplicated. Precedence per connection field:
    ``explicit kwarg → env var(s) → AppConfig default``.

    Two fields are fixed in the synthetic namespace before construction:

    * ``log_level`` is always ``"DEBUG"``.
    * ``health_port`` is always ``0`` (OS-assigned ephemeral port).

    Two further overrides are applied to the config after construction,
    because their dev defaults differ from the production defaults in
    :meth:`AppConfig.from_args_and_env`:

    * ``handler_host`` defaults to ``"127.0.0.1"`` when neither kwarg nor
      env var is set — production defaults to ``"0.0.0.0"``.
    * ``enable_temporal_core_metrics`` defaults to ``False`` to avoid the
      port-9464 collision on hot reload; honoured when
      ``ATLAN_ENABLE_TEMPORAL_CORE_METRICS`` is explicitly set.
    """
    args = argparse.Namespace(
        mode="combined",
        app=app_module,
        handler=os.environ.get("ATLAN_HANDLER_MODULE"),
        handler_host=host,
        handler_port=port,
        temporal_host=temporal_host,
        temporal_namespace=temporal_namespace,
        task_queue=task_queue,
        log_level="DEBUG",
        health_port=0,
        service_name=None,
    )
    config = AppConfig.from_args_and_env(args)
    # Dev default: loopback only. Production defaults to 0.0.0.0 for external
    # access; dev prefers loopback unless the caller or env var says otherwise.
    if not (
        host
        or os.environ.get("ATLAN_HANDLER_HOST")
        or os.environ.get("ATLAN_APP_HTTP_HOST")
    ):
        config.handler_host = "127.0.0.1"
    # Dev default: disable Temporal Rust-core Prometheus binding to avoid port
    # 9464 collision on hot reload. Honour explicit env override.
    if not os.environ.get("ATLAN_ENABLE_TEMPORAL_CORE_METRICS"):
        config.enable_temporal_core_metrics = False
    return config


async def _flush_observability() -> None:
    """Flush all observability buffers before exit."""
    from application_sdk.observability.observability import (  # noqa: PLC0415 — cold path: observability components only at startup
        AtlanObservability,
    )

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
        # conformance: ignore[E002,E004] best-effort flush in unhandled-exception hook; must never mask the original crash
        except Exception:  # noqa: S110 — best-effort flush; must never mask the original crash
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


# --- Worker restart supervisor ----------------------------------------------
# A transient poll auth failure — e.g. a JWKS signing-key cache skew on the
# Temporal frontend that rejects an otherwise-valid token for a few seconds —
# makes temporalio treat the poll as fatal and the worker process exits. Without
# a supervisor the whole runtime goes inactive until a manual restart. These
# bounds drive an automatic rebuild-and-restart of the worker, while still
# failing loud when the failure is persistent (e.g. genuinely bad credentials).
_WORKER_MAX_CONSECUTIVE_RESTARTS = _env_int("ATLAN_WORKER_MAX_CONSECUTIVE_RESTARTS", 10)
_WORKER_RESTART_BACKOFF_CAP_SECONDS = _env_int(
    "ATLAN_WORKER_RESTART_BACKOFF_CAP_SECONDS", 30
)
_WORKER_HEALTHY_RUN_SECONDS = _env_int("ATLAN_WORKER_HEALTHY_RUN_SECONDS", 300)


async def _run_worker_with_restart(
    *,
    build_worker: Callable[[], Any],
    shutdown_event: asyncio.Event,
    auth_manager: Any = None,
    client: Any = None,
) -> None:
    """Run a Temporal worker under a bounded restart supervisor.

    temporalio treats a poll ``PermissionDenied`` (and other non-retryable gRPC
    errors) as fatal: the worker shuts down and re-raises out of the
    ``async with worker`` block. A ``Worker`` is single-use, so each restart
    rebuilds one via ``build_worker``.

    Restart-on-fatal with a cap: any worker-fatal error triggers a rebuild after
    a full-jitter backoff, unless a shutdown was requested. The consecutive
    counter resets once a worker runs healthily for ``_WORKER_HEALTHY_RUN_SECONDS``;
    if the worker keeps failing without ever staying up, the supervisor gives up
    after ``_WORKER_MAX_CONSECUTIVE_RESTARTS`` and re-raises so a persistent
    misconfiguration still fails loud rather than hot-looping forever.

    Args:
        build_worker: Builds a fresh worker (an ``AppWorker``) to run.
        shutdown_event: Set on SIGINT/SIGTERM; a clean shutdown stops the loop.
        auth_manager: Optional; when present its token is force-refreshed before
            each restart to recover from stale/expired tokens.
        client: The Temporal client passed to ``auth_manager.force_refresh``.
    """
    consecutive_failures = 0

    while not shutdown_event.is_set():
        worker = build_worker()
        started_at = time.monotonic()
        try:
            async with worker:
                await shutdown_event.wait()
            # Body returned normally — a shutdown signal, not a failure. Stop.
            return
        except Exception:
            # temporalio surfaces a worker-fatal error by cancelling this task
            # and re-raising the error from Worker.__aexit__. Clear the residual
            # cancellation so the backoff below (asyncio.wait_for) isn't torn
            # down by the leftover cancel count on Python 3.11+.
            current = asyncio.current_task()
            if current is not None and hasattr(current, "uncancel"):
                current.uncancel()

            if shutdown_event.is_set():
                # Failure raced with an in-flight shutdown — treat as clean.
                logger.info("Worker exited during shutdown; not restarting")
                return

            ran_seconds = time.monotonic() - started_at
            if ran_seconds >= _WORKER_HEALTHY_RUN_SECONDS:
                # Ran healthily for a while before failing — a fresh incident,
                # not a restart storm, so reset the streak.
                consecutive_failures = 0
            consecutive_failures += 1

            if consecutive_failures > _WORKER_MAX_CONSECUTIVE_RESTARTS:
                logger.error(
                    "Worker failed %d times without staying healthy for %ds; "
                    "giving up and exiting",
                    consecutive_failures,
                    _WORKER_HEALTHY_RUN_SECONDS,
                    exc_info=True,
                )
                raise

            # Force a fresh token before restarting — recovers stale/expired
            # token cases; harmless for a transient frontend key-cache skew
            # (the backoff itself gives the frontend time to refresh its JWKS).
            if auth_manager is not None:
                try:
                    await auth_manager.force_refresh(client)
                except Exception:
                    logger.warning(
                        "Token refresh before worker restart failed; "
                        "restarting anyway",
                        exc_info=True,
                    )

            # Full-jitter exponential backoff (matches create_temporal_client),
            # raced against shutdown so SIGTERM stays responsive during backoff.
            cap_at_attempt = min(
                2 ** (consecutive_failures - 1),
                _WORKER_RESTART_BACKOFF_CAP_SECONDS,
            )
            delay = random.uniform(0, cap_at_attempt)
            logger.warning(
                "Worker exited with a fatal error (attempt %d/%d, ran %.0fs); "
                "restarting in %.1fs",
                consecutive_failures,
                _WORKER_MAX_CONSECUTIVE_RESTARTS,
                ran_seconds,
                delay,
                exc_info=True,
            )
            try:
                await asyncio.wait_for(shutdown_event.wait(), timeout=delay)
                # Shutdown requested during backoff — stop.
                return
            except TimeoutError:
                # Backoff elapsed without a shutdown request — the expected
                # path; fall through to rebuild and restart the worker.
                logger.debug("Restart backoff elapsed; rebuilding worker")


async def run_worker_mode(config: AppConfig) -> None:
    """Run in worker mode (Temporal workflow execution).

    Loads the app class, connects to Temporal, creates a worker, and
    runs until a shutdown signal is received.
    """
    global _worker_event_loop
    _worker_event_loop = asyncio.get_running_loop()

    from application_sdk.app.registry import (  # noqa: PLC0415 — circular: app.* imports from main.py via _pascal_to_kebab
        AppRegistry,
        TaskRegistry,
    )
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
        enable_prometheus=config.enable_temporal_core_metrics,
        prometheus_bind_address=config.prometheus_bind_address,
    )

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

    # Worker-only mode pushes metrics to a Pushgateway since the process has
    # no /metrics endpoint to scrape. Combined mode (run_combined_mode below)
    # leaves enable_pushgateway=False so the FastAPI /metrics endpoint
    # exposes everything via in-process proxy.
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

    def _signal_handler() -> None:
        logger.info("Shutdown signal received")
        shutdown_event.set()

    loop = asyncio.get_running_loop()
    loop.set_exception_handler(_loop_exception_handler)
    _install_graceful_signal_handlers(loop, _signal_handler)

    logger.info("Worker started: app=%s queue=%s", app_name, config.task_queue)
    _log_process_memory_baseline()
    # health_server stays up across worker restarts so the runtime keeps
    # answering health checks while the supervisor rebuilds a crashed worker.
    async with health_server:
        await _run_worker_with_restart(
            build_worker=_build_worker,
            shutdown_event=shutdown_event,
            auth_manager=auth_manager,
            client=client,
        )

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

    Loads the handler class (or DefaultHandler) and runs the FastAPI
    server via uvicorn. This is synchronous — uvicorn manages its own loop.
    """
    # BOOT-TIME: data converter creation moved into the handler's lazy
    # Temporal-client factory — importing it here pulls temporalio at boot.
    from application_sdk.handler import (  # noqa: PLC0415 — cold path: only loaded in handler mode
        DefaultHandler,
        run_app_handler_service,
    )
    from application_sdk.infrastructure.context import (  # noqa: PLC0415 — cold path: only when infrastructure init is needed
        set_infrastructure,
    )

    infra = asyncio.run(_create_infrastructure())
    from application_sdk.storage.preflight import (  # noqa: PLC0415 — cold path: SDR-gated; only runs when ENABLE_ATLAN_UPLOAD=true
        verify_object_store_access,
    )

    asyncio.run(verify_object_store_access(infra))
    set_infrastructure(infra)

    logger.info(
        "Starting handler mode: app=%s host=%s port=%d",
        config.app_module,
        config.handler_host,
        config.handler_port,
    )
    _log_process_memory_baseline()

    app_class = load_app_class(config.app_module)
    validate_app_class(app_class)
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
    data_converter = None  # BOOT-TIME: built lazily at first workflow call

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
        enable_temporal_core_metrics=False,
        prometheus_bind_address=config.prometheus_bind_address,
        secret_store=infra.secret_store,
        storage=infra.storage,
        frontend_assets_path=config.frontend_assets_path,
        workflow_max_timeout_hours=config.workflow_max_timeout_hours,
    )

    from application_sdk.infrastructure.context import (  # noqa: PLC0415 — cold path: only when infrastructure init is needed
        close_infrastructure,
    )

    asyncio.run(close_infrastructure())
    asyncio.run(_flush_observability())


async def run_combined_mode(config: AppConfig) -> None:
    """Run worker + handler in a single process (SDR / docker-compose mode).

    Combines Temporal worker execution with the HTTP handler service,
    enabling single-container deployment. Both components share the same
    event loop and shut down together on SIGINT/SIGTERM.
    """
    global _worker_event_loop
    _worker_event_loop = asyncio.get_running_loop()

    import uvicorn  # noqa: PLC0415 — cold path: uvicorn only loaded in worker/handler runtime modes

    from application_sdk.app.registry import (  # noqa: PLC0415 — circular: app.* imports from main.py via _pascal_to_kebab
        AppRegistry,
        TaskRegistry,
    )
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
        enable_prometheus=config.enable_temporal_core_metrics,
        prometheus_bind_address=config.prometheus_bind_address,
    )

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

    def _build_worker() -> Any:
        # Rebuilt on each supervisor restart — Worker instances are single-use.
        # on_activity feeds the /live liveness window (BLDX-1552).
        return create_worker(
            client,
            task_queue=config.task_queue,
            handler=handler,
            on_activity=health_server.record_activity,
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

    def _signal_handler() -> None:
        logger.info("Shutdown signal received")
        shutdown_event.set()
        uvicorn_server.should_exit = True

    loop = asyncio.get_running_loop()
    loop.set_exception_handler(_loop_exception_handler)
    _install_graceful_signal_handlers(loop, _signal_handler)

    logger.info(
        "Combined mode started: app=%s queue=%s port=%d",
        app_name,
        config.task_queue,
        config.handler_port,
    )
    _log_process_memory_baseline()
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
            ),
        )

    from application_sdk.infrastructure.context import (  # noqa: PLC0415 — cold path: only when infrastructure init is needed
        close_infrastructure,
    )

    await close_infrastructure()
    await _flush_observability()
    if auth_manager is not None:
        await auth_manager.shutdown()
        logger.info("Auth manager stopped")

    logger.info("Combined mode stopped")


async def run_dev_combined(
    app_class: type[App],
    *,
    credential_stores: Mapping[str, SecretStore] | None = None,
    credentials: dict[str, Any] | None = None,
    example_input: dict[str, Any] | None = None,
    host: str | None = None,
    port: int | None = None,
    temporal_host: str | None = None,  # deprecated — ignored, kept for back-compat
    temporal_namespace: str | None = None,
    temporal_ui: bool = False,
    temporal_ui_port: int = 8233,
    task_queue: str | None = None,
) -> None:
    """Run worker + handler in a single process for local development.

    Boots an **in-process workflow runtime** and uses **in-process backends**
    for state, secrets, and object storage — no Temporal CLI, no Dapr
    sidecar, no Redis required. The customer's only prerequisite is
    Python + ``uv``.

    Use this in ``run_dev.py`` scripts; production containers use
    ``run_combined_mode()`` via CLI flags, which goes through Dapr.

    All four connection-shaped kwargs (``host``, ``port``,
    ``temporal_namespace``, ``task_queue``) follow the same precedence as
    the CLI path — ``explicit kwarg → env var → AppConfig default`` —
    because they are resolved by :func:`_build_dev_config` which routes
    through :meth:`AppConfig.from_args_and_env`. CI-stack overrides like
    ``ATLAN_HANDLER_PORT`` or ``ATLAN_TASK_QUEUE`` work without any
    per-connector ``os.environ.get(...)`` boilerplate at the call site.

    ``temporal_host`` is still accepted for backward compatibility but is
    ignored — the SDK always boots its own in-process workflow runtime in
    this mode. Passing it emits a :class:`DeprecationWarning`.

    Args:
        app_class: The App class to serve (must already be imported).
        credential_stores: Optional mapping of store name → SecretStore.
        credentials: Optional credential dict (host, port, username, password,
            authType, extra, etc.). If provided, credentials are auto-provisioned
            via ``POST /workflows/v1/dev/local-vault`` once the handler is ready, and the
            returned ``credential_guid`` is injected into ``example_input``.
            This mimics the production flow where Heracles provisions credentials
            before starting a workflow.
        example_input: Optional dict used as the workflow input. If ``credentials``
            is also provided, ``credential_guid`` is auto-injected before the
            workflow starts.
        host: Bind host. Default precedence: kwarg → ``ATLAN_HANDLER_HOST`` →
            ``ATLAN_APP_HTTP_HOST`` → ``"127.0.0.1"``.
        port: Handler HTTP port. Default precedence: kwarg →
            ``ATLAN_HANDLER_PORT`` → ``ATLAN_APP_HTTP_PORT`` → ``8000``.
        temporal_namespace: Temporal namespace. Default precedence: kwarg →
            ``ATLAN_TEMPORAL_NAMESPACE`` → ``ATLAN_WORKFLOW_NAMESPACE`` →
            ``"default"``.
        temporal_ui: Enable the embedded Temporal Web UI for local debugging.
            Default ``False`` keeps the dev server headless.
        temporal_ui_port: Temporal Web UI port. Defaults to ``8233``.
        task_queue: Task queue name. Default precedence: kwarg →
            ``ATLAN_TASK_QUEUE`` → ``"{app_name}-queue"``.

    Example::

        import asyncio
        from my_app import MyApp

        asyncio.run(run_dev_combined(
            MyApp,
            example_input={
                "connection": {"connection_name": "test"},
            },
        ))
    """
    # Local dev is unconditional: boot an in-process Temporal *and* an
    # embedded ``daprd`` so the entire infrastructure code path is the same
    # one production uses. Both daemons are auto-downloaded and managed by
    # the SDK — the customer's host stays clean.
    from application_sdk.dev import embedded_dapr, embedded_runtime  # noqa: PLC0415

    if temporal_host is not None:
        import warnings  # noqa: PLC0415 — cold path: deprecation warning only

        warnings.warn(
            "`temporal_host` is deprecated and ignored and will be removed in "
            "v4.0: `run_dev_combined` now always boots an in-process workflow "
            "runtime. To target an external Temporal cluster, use "
            "`run_combined_mode(config)` directly.",
            DeprecationWarning,
            stacklevel=2,
        )

    app_name = getattr(app_class, "_app_name", "") or app_class.__name__.lower()

    # ``embedded_dapr`` sets ``DAPR_HTTP_PORT`` / ``DAPR_GRPC_PORT`` /
    # ``DAPR_COMPONENTS_PATH`` itself, so the existing Dapr code path in
    # ``_create_infrastructure`` and the observability sink see the right
    # values from the moment daprd is spawning (avoiding races during the
    # ~3s startup window).
    # ``embedded_dapr`` first so ``DAPR_COMPONENTS_PATH`` is set before any
    # observability flush cycle fires during the ~5s ``embedded_runtime``
    # cold-start window. Otherwise the periodic flush would race with daprd
    # startup and log spurious "objectstore upload failed" warnings.
    async with (
        embedded_dapr(app_id=app_name) as _dapr,
        embedded_runtime(
            namespace=temporal_namespace or "default",
            temporal_ui=temporal_ui,
            temporal_ui_port=temporal_ui_port,
        ) as _rt,
    ):
        del _dapr  # env-side-effect is sufficient; the dataclass is just for tests
        if _rt.ui_url:
            # conformance: ignore[L005] direct dev-mode terminal banner; existing tests assert on this via capsys, not the logging pipeline
            print(f"\nTemporal UI running at {_rt.ui_url}")
        await _run_dev_combined_inner(
            app_class=app_class,
            credential_stores=credential_stores,
            credentials=credentials,
            example_input=example_input,
            host=host,
            port=port,
            temporal_host=_rt.host,
            temporal_namespace=_rt.namespace,
            task_queue=task_queue,
        )


async def _run_dev_combined_inner(
    *,
    app_class: type[App],
    credential_stores: Mapping[str, SecretStore] | None,
    credentials: dict[str, Any] | None,
    example_input: dict[str, Any] | None,
    host: str | None,
    port: int | None,
    temporal_host: str,
    temporal_namespace: str,
    task_queue: str | None,
) -> None:
    """Body of ``run_dev_combined`` — runs against fully-resolved connection details.

    Accepts ``host`` / ``port`` / ``task_queue`` as ``None`` and lets
    ``_build_dev_config`` apply the same defaults the CLI path uses (env
    vars → AppConfig fields). ``temporal_host`` and ``temporal_namespace``
    always arrive populated from the embedded runtime.
    """
    app_module = f"{app_class.__module__}:{app_class.__name__}"

    config = _build_dev_config(
        app_module,
        host=host,
        port=port,
        temporal_host=temporal_host,
        temporal_namespace=temporal_namespace,
        task_queue=task_queue,
    )

    # Build infrastructure via the standard Dapr path. ``run_dev_combined``
    # has already started an embedded ``daprd`` (via ``embedded_dapr``) and
    # exported ``DAPR_HTTP_PORT`` + ``DAPR_COMPONENTS_PATH``, so this goes
    # through identical code as production / SDR / CI.
    from application_sdk.infrastructure.context import (  # noqa: PLC0415 — cold path: only when infrastructure init is needed
        set_infrastructure,
    )

    infra = await _create_infrastructure(credential_stores=credential_stores)
    from application_sdk.storage.preflight import (  # noqa: PLC0415 — cold path: SDR-gated; only runs when ENABLE_ATLAN_UPLOAD=true
        verify_object_store_access,
    )

    await verify_object_store_access(infra)
    set_infrastructure(infra)

    # Auto-provision credentials if provided (mimics Heracles writing to
    # /workflows/v1/dev/local-vault before starting the workflow). This writes non-sensitive
    # config to object storage and sensitive secrets to the local secrets file.
    if credentials is not None:
        import httpx  # noqa: PLC0415 — cold path: lazy load to keep import-time cost low

        async def _provision_and_start() -> None:
            """Wait for handler, provision creds, start workflow — mimics prod."""
            base = f"http://{config.handler_host}:{config.handler_port}"
            async with httpx.AsyncClient() as client:
                # Wait for the handler to be ready
                for _ in range(30):
                    try:
                        resp = await client.get(f"{base}/health", timeout=2)
                        if resp.status_code == 200:
                            break
                    # conformance: ignore[E004] startup readiness poll; /health transient errors expected and retried
                    except Exception as exc:
                        logger.debug(
                            "Skipping /health poll iteration due to transient error: %s",
                            exc,
                            exc_info=True,
                        )
                    await asyncio.sleep(1)

                # Step 1: Provision credentials (mimics Heracles)
                resp = await client.post(
                    f"{base}/workflows/v1/dev/local-vault",
                    json=credentials,
                    timeout=10,
                )
                result = resp.json()
                credential_guid = result.get("data", {}).get(
                    "credential_guid"
                ) or result.get("credential_guid")
                logger.info("Auto-provisioned credentials: guid=%s", credential_guid)

                # Step 2: Start workflow (mimics Heracles/AE)
                workflow_input = dict(example_input or {})
                workflow_input["credential_guid"] = credential_guid
                resp = await client.post(
                    f"{base}/workflows/v1/start",
                    json=workflow_input,
                    timeout=30,
                )
                start_result = resp.json()
                wf_data = start_result.get("data", {})
                workflow_id = wf_data.get("workflow_id", "")
                run_id = wf_data.get("run_id", "")
                logger.info(
                    "Auto-started workflow: id=%s run_id=%s",
                    workflow_id,
                    run_id,
                )

            # conformance: ignore[L005] direct dev-mode terminal banner; existing tests assert on this via capsys, not the logging pipeline
            print(f"\n  Credentials provisioned: credential_guid={credential_guid}")
            # conformance: ignore[L005] direct dev-mode terminal banner; existing tests assert on this via capsys, not the logging pipeline
            print(f"  Workflow started: workflow_id={workflow_id} run_id={run_id}")
            # conformance: ignore[L005] direct dev-mode terminal banner; existing tests assert on this via capsys, not the logging pipeline
            print(f"\n  curl {base}/workflows/v1/result/{workflow_id}\n")

        # Schedule provisioning + start as a background task — runs after the server starts
        asyncio.create_task(_provision_and_start())
    else:
        # conformance: ignore[L005] direct dev-mode terminal banner; existing tests assert on this via capsys, not the logging pipeline
        print(
            f"\nDev server running at http://{config.handler_host}:{config.handler_port}"
        )
        # conformance: ignore[L005] direct dev-mode terminal banner; existing tests assert on this via capsys, not the logging pipeline
        print(
            "  POST /workflows/v1/dev/local-vault                            - Provision credentials"
        )
        # conformance: ignore[L005] direct dev-mode terminal banner; existing tests assert on this via capsys, not the logging pipeline
        print("  POST /workflows/v1/start                         - Start workflow")
        # conformance: ignore[L005] direct dev-mode terminal banner; existing tests assert on this via capsys, not the logging pipeline
        print("  POST /workflows/v1/stop/{workflow_id}/{run_id}   - Stop workflow")
        # conformance: ignore[L005] direct dev-mode terminal banner; existing tests assert on this via capsys, not the logging pipeline
        print("  GET  /workflows/v1/result/{workflow_id}          - Get result")
        # conformance: ignore[L005] direct dev-mode terminal banner; existing tests assert on this via capsys, not the logging pipeline
        print("  GET  /workflows/v1/status/{workflow_id}/{run_id} - Get status")
        # conformance: ignore[L005] direct dev-mode terminal banner; existing tests assert on this via capsys, not the logging pipeline
        print("  GET  /health                                      - Health check")
        if example_input is not None:
            # conformance: ignore[L005] direct dev-mode terminal banner; existing tests assert on this via capsys, not the logging pipeline
            print("\nExample:")
            example_json = orjson.dumps(
                example_input, option=orjson.OPT_INDENT_2
            ).decode()
            # conformance: ignore[L005] direct dev-mode terminal banner; existing tests assert on this via capsys, not the logging pipeline
            print(
                f"  curl -X POST http://{config.handler_host}:{config.handler_port}/workflows/v1/start \\"
            )
            # conformance: ignore[L005] direct dev-mode terminal banner; existing tests assert on this via capsys, not the logging pipeline
            print('    -H "Content-Type: application/json" \\')
            # conformance: ignore[L005] direct dev-mode terminal banner; existing tests assert on this via capsys, not the logging pipeline
            print(f"    -d '{example_json}'")
        # conformance: ignore[L005] direct dev-mode terminal banner; existing tests assert on this via capsys, not the logging pipeline
        print(
            f"\n  curl http://{config.handler_host}:{config.handler_port}/workflows/v1/result/{{workflow_id}}\n"
        )

    await run_combined_mode(config)


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

    if config.mode == "worker":
        asyncio.run(run_worker_mode(config))
    elif config.mode == "handler":
        run_handler_mode(config)
    elif config.mode == "combined":
        asyncio.run(run_combined_mode(config))
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
        from application_sdk.storage.errors import (  # noqa: PLC0415 — cold path: lazy import avoids loading storage module on every boot
            ObjectStorePreflightError,
        )

        if isinstance(exc, ObjectStorePreflightError):
            logger.error(
                "SDR object-store preflight failed — cannot start:\n%s",
                exc,
                exc_info=True,
            )
            try:
                asyncio.run(_flush_observability())
            # conformance: ignore[E002,E004] best-effort flush on fatal exit; fatal error already logged above
            except Exception:  # noqa: S110 — best-effort flush on fatal exit; error already logged above
                pass
            sys.exit(1)
        logger.error("Fatal error", exc_info=True)
        try:
            asyncio.run(_flush_observability())
        # conformance: ignore[E002,E004] best-effort flush on fatal exit; fatal error already logged above
        except Exception:  # noqa: S110 — best-effort flush on fatal exit; error already logged above
            pass
        sys.exit(1)

    sys.exit(0)


if __name__ == "__main__":
    main()
