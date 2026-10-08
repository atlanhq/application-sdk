"""The handler never imports worker code (FND-3280).

The handler is moving to a shared pod that serves every app, with no worker
beside it, and must stay movable into its own codebase. The allowed direction is
**worker → handler only**: the worker may call the handler; the handler never
imports, reuses or calls into worker code.

At runtime that already held. At import time it did not: importing
``application_sdk.handler`` loaded 32 worker modules (``application_sdk.execution.*``,
``temporalio.worker.*``, ``temporalio.activity``) through ``app/__init__`` and the
``/check`` route's helpers in ``preflight_gate``. This test is what keeps the
fix from rotting back.

The import runs in a **subprocess**: in-process, pytest has already imported
most of the SDK, so ``sys.modules`` would show the worker loaded whatever the
handler actually pulls in.
"""

from __future__ import annotations

import importlib
import subprocess
import sys

#: The handler surface that must load without the worker. Listed explicitly
#: rather than discovered by walking the package: a new entry point should be a
#: conscious decision. ``_runtime.offload`` is here because the handler calls
#: ``run_in_thread`` / ``CancelHandle`` (FND-3269) and must keep being able to.
_HANDLER_MODULES = (
    "application_sdk.handler",
    "application_sdk.handler.contracts",
    "application_sdk.handler.base",
    "application_sdk.handler.service",
    "application_sdk._runtime.offload",
)

#: Worker code: the SDK's execution layer and Temporal's worker-side packages.
_FORBIDDEN_PREFIXES = (
    "application_sdk.execution",
    "temporalio.worker",
    "temporalio.activity",
)

#: TEMPORARY. ``temporalio.client`` and everything it pulls in (which includes
#: ``temporalio.activity``) are excused, because the ``/workflows/v1/start``
#: route uses the client to start workflows. That route is expected to be
#: removed entirely in the shared-server migration; delete this allowance, and
#: ``temporalio.client`` from ``handler/service.py``, together with the route.
_ALLOWED_FOR_START_ROUTE = "temporalio.client"


def _loaded_after_importing(*modules: str) -> set[str]:
    """Import *modules* in a fresh interpreter; return everything in ``sys.modules``."""
    imports = "".join(f"importlib.import_module({m!r});" for m in modules)
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            f"import sys, importlib;{imports}print('\\n'.join(sys.modules))",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert (
        result.returncode == 0
    ), f"importing {modules} in a fresh interpreter failed:\n{result.stderr}"
    return {line for line in result.stdout.splitlines() if line}


def _is_worker_module(name: str) -> bool:
    return any(
        name == prefix or name.startswith(prefix + ".")
        for prefix in _FORBIDDEN_PREFIXES
    )


def test_the_handler_imports_no_worker_code() -> None:
    """Importing the handler surface loads no worker module.

    Fix a failure by moving the shared piece down into a neutral module
    (``handler/``, ``contracts``, ``errors``, ``common``, ``_runtime``) and having
    the worker import it from there — never by importing worker code lazily
    from the handler, which only hides the edge from this test.
    """
    excused = _loaded_after_importing(_ALLOWED_FOR_START_ROUTE)
    loaded = _loaded_after_importing(*_HANDLER_MODULES)
    offenders = sorted(name for name in loaded - excused if _is_worker_module(name))
    assert not offenders, (
        f"Importing the handler loaded {len(offenders)} worker module(s); "
        "the handler may not import worker code (worker → handler only):\n  "
        + "\n  ".join(offenders)
    )


def test_creating_the_handler_service_imports_no_worker_code() -> None:
    """Building the service runs startup code (the SageV2 warmup drift check)
    that importing the module does not; it must stay worker-free too (F-b20a23).

    Fails if a startup path reaches the app registry, whose package imports
    execution code.
    """
    excused = _loaded_after_importing(_ALLOWED_FOR_START_ROUTE)
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys\n"
            "from application_sdk.handler.base import DefaultHandler\n"
            "from application_sdk.handler.service import create_app_handler_service\n"
            "create_app_handler_service(DefaultHandler(), app_name='boundary-app')\n"
            "print('\\n'.join(sys.modules))",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    loaded = {line for line in result.stdout.splitlines() if line}
    offenders = sorted(name for name in loaded - excused if _is_worker_module(name))
    assert not offenders, (
        "Creating the handler service loaded worker module(s):\n  "
        + "\n  ".join(offenders)
    )


def test_the_start_route_allowance_is_still_needed() -> None:
    """The ``temporalio.client`` allowance excuses only what the client loads.

    Pins the allowance to its reason: if ``temporalio.client`` stopped pulling in
    ``temporalio.activity``, or the handler stopped importing the client, the
    excuse would be dead weight hiding nothing — remove it then.
    """
    loaded = _loaded_after_importing(*_HANDLER_MODULES)
    assert _ALLOWED_FOR_START_ROUTE in loaded, (
        "The handler no longer imports temporalio.client: delete "
        "_ALLOWED_FOR_START_ROUTE and the allowance it grants."
    )


def test_the_contracts_load_without_the_http_server() -> None:
    """``handler/__init__`` serves the service names lazily.

    Importing a contract (``CheckTier``, ``PreflightInput``) is what the worker
    and a future out-of-tree handler both do; it must not drag in FastAPI and
    the Temporal client behind ``handler.service``.
    """
    loaded = _loaded_after_importing("application_sdk.handler.contracts")
    assert "application_sdk.handler.service" not in loaded
    assert "fastapi" not in loaded


#: Every name ``application_sdk.handler`` exported before FND-3280 made the
#: service names lazy. Enumerated so a dropped name fails here, not in an app.
_HANDLER_EXPORTS = (
    "ApiMetadataObject",
    "ApiMetadataOutput",
    "AuthInput",
    "AuthOutput",
    "AuthStatus",
    "BaseConnectionConfig",
    "BaseMetadataConfig",
    "CheckTier",
    "DefaultHandler",
    "Handler",
    "HandlerContext",
    "HandlerCredential",
    "HandlerError",
    "MetadataInput",
    "MetadataOutput",
    "PreflightCheck",
    "PreflightGateMode",
    "PreflightInput",
    "PreflightOutput",
    "PreflightStatus",
    "SqlMetadataObject",
    "SqlMetadataOutput",
    "WarmupInput",
    "WarmupObservation",
    "WarmupState",
    "create_app_handler_service",
    "run_app_handler_service",
)


def test_every_handler_export_still_resolves() -> None:
    """``from application_sdk.handler import <name>`` keeps working for every name."""
    import application_sdk.handler as handler_pkg
    from application_sdk.handler import service

    assert set(handler_pkg.__all__) == set(_HANDLER_EXPORTS)
    for name in _HANDLER_EXPORTS:
        assert getattr(handler_pkg, name) is not None, name
    assert handler_pkg.create_app_handler_service is service.create_app_handler_service
    assert handler_pkg.run_app_handler_service is service.run_app_handler_service


def test_moved_names_resolve_to_the_same_objects_at_their_old_paths() -> None:
    """The pieces moved down for FND-3280 are re-exported, not copied.

    A copy would split a patch or an ``isinstance`` check across two objects.
    The old modules are fetched with ``import_module``: ``application_sdk.app``
    binds ``entrypoint`` to the decorator, which shadows the submodule.
    """
    old_tree = importlib.import_module("application_sdk.app._generated_tree")
    base_errors = importlib.import_module("application_sdk.app.base_errors")
    build_identity = importlib.import_module("application_sdk.app.build_identity")
    entrypoint = importlib.import_module("application_sdk.app.entrypoint")
    from application_sdk.common import _generated_tree
    from application_sdk.common import build_identity as new_build_identity
    from application_sdk.common import dispatch
    from application_sdk.execution._temporal import preflight_gate
    from application_sdk.handler import _preflight_outcome
    from application_sdk.infrastructure import secrets

    assert entrypoint.canonical_workflow_type is dispatch.canonical_workflow_type
    assert (
        base_errors.SecretStoreNotConfiguredError
        is secrets.SecretStoreNotConfiguredError
    )
    assert build_identity.build_identity is new_build_identity.build_identity
    assert old_tree.choose_form_configmap is _generated_tree.choose_form_configmap
    for name in (
        "PreflightSurface",
        "PreflightRowOutcome",
        "emit_preflight_check_outcome",
        "emit_preflight_crash_outcome",
        "rows_outside_tiers",
        "warn_if_partial",
    ):
        assert getattr(preflight_gate, name) is getattr(_preflight_outcome, name), name


def test_every_declared_handler_name_resolves() -> None:
    """``__all__`` and the lazy ``__getattr__`` allowlist must agree.

    The Symbol Removal Check reads the getter statically; this is the runtime
    half. A name kept in ``__all__`` (and the ``TYPE_CHECKING`` import) after the
    getter stopped serving it would fail ``from application_sdk.handler import``
    for every consumer while the static check still saw it declared.
    """
    import application_sdk.handler as handler

    for name in handler.__all__:
        assert getattr(handler, name, None) is not None, name
