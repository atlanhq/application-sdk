"""``run_dev_combined``: worker + handler in one process for local development.

Moved out of ``application_sdk.main`` (FND-4689). ``application_sdk.main``
re-exports :func:`run_dev_combined`, which stays the documented import path.

This module reaches back into ``application_sdk.main`` for ``AppConfig`` and
``run_combined_mode`` at call time rather than at import: ``main`` imports this
module to re-export ``run_dev_combined``, so a module-scope import here would
be circular.
"""

from __future__ import annotations

import argparse
import asyncio
import os
from typing import TYPE_CHECKING, Any

import orjson

from application_sdk.infrastructure._bootstrap import _create_infrastructure
from application_sdk.observability.logger_adaptor import get_logger

if TYPE_CHECKING:
    from collections.abc import Mapping

    from application_sdk.app.base import App
    from application_sdk.infrastructure.secrets import SecretStore
    from application_sdk.main import AppConfig

logger = get_logger(__name__)


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
    from application_sdk.main import (  # noqa: PLC0415 — main imports this module; see the module docstring
        AppConfig,
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


#: Default for ``run_dev_combined(credential_stores=...)``. A sentinel rather
#: than ``None`` so an explicit ``credential_stores=None`` also warns.
_CREDENTIAL_STORES_UNSET = object()


async def run_dev_combined(
    app_class: type[App],
    *,
    credential_stores: Mapping[str, SecretStore]
    | None
    | object = _CREDENTIAL_STORES_UNSET,  # deprecated — ignored, kept for back-compat
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

    ``credential_stores`` is likewise accepted but ignored: secrets resolve
    through the embedded ``daprd``'s secret store, never through a store
    passed here. Passing it, even as ``None``, emits a
    :class:`DeprecationWarning`; use
    ``credentials`` to provision a local credential instead.

    Args:
        app_class: The App class to serve (must already be imported).
        credential_stores: Deprecated and ignored. Use ``credentials``.
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

    if credential_stores is not _CREDENTIAL_STORES_UNSET:
        import warnings  # noqa: PLC0415 — cold path: deprecation warning only

        warnings.warn(
            "`credential_stores` is deprecated and ignored and will be removed "
            "in v4.0.0: `run_dev_combined` resolves secrets through the "
            "embedded Dapr secret store, never through a store passed here. "
            "Use `credentials=` to provision a local credential instead.",
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

    infra = await _create_infrastructure()
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
                    # conformance: ignore[P060] readiness poll of this pod's own handler /health at a fixed 1s, not a retry against a shared dependency
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

    from application_sdk.main import (  # noqa: PLC0415 — main imports this module; see the module docstring
        run_combined_mode,
    )

    await run_combined_mode(config)
