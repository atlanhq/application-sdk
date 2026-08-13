"""The layering contract that makes ``application_sdk._runtime`` load-bearing.

FND-316: ``storage/`` used to reach *upward* into ``execution/`` for the offload
pool and the progress seam, while ``execution/`` reached back down into
``storage.ops`` through its package ``__init__``. Every call site on that edge
carried its own lazy import and its own copy of the explanation, and each new one
had to rediscover the constraint by hitting an ``ImportError``.

The fix (ADR-0019) moved both primitives to a dependency-neutral substrate. The
tests here are what stop that from rotting back: one asserts the substrate
imports nothing from the layers above it, one asserts the storage module that
sat on the cycle imports cleanly on its own, and one asserts the app-facing
façades still resolve to the same objects.

Each import check runs in a **subprocess**. In-process, pytest has already
imported most of the SDK, so ``sys.modules`` would show every layer loaded
regardless of what the module under test actually pulls in — the assertion would
pass or fail for reasons unrelated to the import graph.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

#: Every module in the substrate. Listed explicitly rather than discovered by
#: walking the package: a new module added here without a conscious decision
#: about its dependencies is exactly what this test exists to catch, and a
#: discovery loop would silently accept it.
_RUNTIME_MODULES = [
    "application_sdk._runtime.offload",
    "application_sdk._runtime.progress",
]

#: Packages a substrate module may not pull in, directly or transitively. Each
#: one reaches ``storage.ops`` (``contracts`` and ``credentials`` via
#: ``contracts.types`` → ``credentials.ref`` → ``common.utils``; ``execution``
#: and ``app`` via ``execution/__init__`` → ``_temporal`` → ``app.base``), so
#: importing any of them from here re-creates the cycle FND-316 removed.
_FORBIDDEN = (
    "application_sdk.app",
    "application_sdk.contracts",
    "application_sdk.credentials",
    "application_sdk.execution",
    "application_sdk.storage",
)


def _loaded_after_importing(module: str) -> set[str]:
    """Import *module* in a fresh interpreter; return the SDK modules it pulled in."""
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys, importlib;"
            f"importlib.import_module({module!r});"
            "print('\\n'.join(m for m in sys.modules if m.startswith('application_sdk')))",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert (
        result.returncode == 0
    ), f"importing {module} in a fresh interpreter failed:\n{result.stderr}"
    return {line for line in result.stdout.splitlines() if line}


@pytest.mark.parametrize("module", _RUNTIME_MODULES)
def test_a_runtime_module_pulls_in_no_layer_above_it(module: str) -> None:
    """The substrate imports the logger and nothing else from ``application_sdk``.

    A violation here is not a style point: it means some ``storage/`` module that
    imports this one at module scope will raise ``ImportError`` the moment it is
    the first thing imported in a process. Fix it by moving the dependency down
    into ``_runtime`` or by not needing it, never by making the caller lazy.
    """
    loaded = _loaded_after_importing(module)
    violations = sorted(
        name
        for name in loaded
        if any(name == pkg or name.startswith(pkg + ".") for pkg in _FORBIDDEN)
    )
    assert not violations, (
        f"{module} is in the dependency-neutral substrate but pulled in "
        f"{violations}. See application_sdk/_runtime/__init__.py and ADR-0019."
    )


def test_storage_ops_imports_without_the_execution_layer() -> None:
    """``storage.ops`` must load standalone — the FND-316 cycle, directly asserted.

    This is the import that used to fail: ``storage.ops`` at module scope reached
    ``execution.progress``, which ran ``execution/__init__``, which came back
    round to a half-initialised ``storage.ops``. Asserting ``execution`` is
    absent (not merely that the import succeeded) is what keeps the *layering*
    honest rather than only the load order.
    """
    loaded = _loaded_after_importing("application_sdk.storage.ops")
    assert "application_sdk.storage.ops" in loaded
    assert "application_sdk.execution" not in loaded, (
        "storage.ops pulled in the execution layer again — the storage → "
        "execution edge is back. Import from application_sdk._runtime instead."
    )


def test_the_app_facing_facades_re_export_the_substrate_objects() -> None:
    """``execution.heartbeat`` / ``execution.progress`` stay the documented paths.

    Apps and the SDK docs import from those two modules, so the re-exports must
    be the *same objects* — a shim that rebuilt or wrapped them would give a
    patch applied at one path no effect at the other.
    """
    from application_sdk._runtime import offload, progress
    from application_sdk.execution import heartbeat
    from application_sdk.execution import progress as progress_facade

    assert heartbeat.run_in_thread is offload.run_in_thread
    assert heartbeat.submit_in_thread is offload.submit_in_thread
    assert heartbeat.run_fault_isolated is offload.run_fault_isolated
    assert heartbeat.run_best_effort is offload.run_best_effort
    assert progress_facade.current_progress_tracker is progress.current_progress_tracker
    assert progress_facade.holding_progress is progress.holding_progress
    assert progress_facade.ProgressTracker is progress.ProgressTracker


#: Every name that resolved as an attribute of ``application_sdk.execution.heartbeat``
#: before the offload seam moved out (FND-316) and is a real SDK or library symbol
#: rather than incidental import leakage (a stdlib module alias, a ``TypeVar``, a
#: ``typing`` helper). Several were never used by that module — it imported them for
#: the implementation that has since moved — but they *resolved*, so dropping them
#: would be a breaking change dressed up as a refactor.
_HEARTBEAT_BACK_COMPAT = (
    "AtlanLoggerAdapter",
    "BrokenProcessPool",
    "HeartbeatController",
    "NoopHeartbeatController",
    "ProgressTracker",
    "ProgressWatchdogMode",
    "TemporalHeartbeatController",
    "auto_heartbeat_loop",
    "current_progress_tracker",
    "declared_hold_active",
    "parse_pod_memory_limit",
    "record_no_progress_gap",
    "run_best_effort",
    "run_fault_isolated",
    "run_in_thread",
    "submit_in_thread",
)


@pytest.mark.parametrize("name", _HEARTBEAT_BACK_COMPAT)
def test_heartbeat_still_resolves_every_name_it_used_to(name: str) -> None:
    """``from application_sdk.execution.heartbeat import <name>`` must not regress.

    A module split silently narrows the namespace of the file it splits: names the
    original imported for its own use stop resolving through it. Nothing in the SDK
    imports these from here, so no other test would notice — which is exactly why
    this one enumerates them.
    """
    from application_sdk.execution import heartbeat

    assert hasattr(heartbeat, name), (
        f"application_sdk.execution.heartbeat.{name} no longer resolves; it did "
        "before FND-316, so removing it is a breaking change for any consumer that "
        "imported it from here."
    )


def test_execution_progress_resolves_as_a_package_attribute() -> None:
    """``import application_sdk.execution`` must still bind ``.progress``.

    Before FND-316 this held by accident: ``heartbeat`` imported the submodule, so the
    attribute was set as a side effect. ``heartbeat`` now reaches ``_runtime.progress``
    instead, so without an explicit import in ``execution/__init__`` the pattern
    ``import application_sdk.execution as ex; ex.progress.holding_progress(...)``
    would raise AttributeError.
    """
    import application_sdk.execution as execution_package

    assert hasattr(execution_package, "progress")
    assert callable(execution_package.progress.holding_progress)


def test_auto_heartbeat_loop_stays_patchable_through_the_heartbeat_module() -> None:
    """Consumers neutralise the beat in their tests by patching it here.

    At least one connector's ``conftest.py`` patches
    ``application_sdk.execution.heartbeat.auto_heartbeat_loop``. That only intercepts
    the activity's call if the activity resolves the name through this module at call
    time, so ``activities.py`` must not bind it at module scope. The failure mode this
    guards is silent: the patch still applies, and the real loop runs anyway.
    """
    from application_sdk.execution._temporal import activities

    assert not hasattr(activities, "auto_heartbeat_loop"), (
        "activities.py bound auto_heartbeat_loop at module scope, which silently "
        "breaks consumers patching application_sdk.execution.heartbeat."
        "auto_heartbeat_loop — keep it a call-time import."
    )


def test_serializable_enum_never_left_contracts_base() -> None:
    """``SerializableEnum`` stays exactly where consumers already subclass it.

    App code subclasses this to make its own enums Temporal-serialisable, so its
    definition site is part of the contract, not an implementation detail:
    ``__module__`` feeds pickling and schema naming, and relocating it would
    change both for every downstream subclass.

    It briefly moved to the substrate on the theory that
    ``_runtime.progress.ProgressWatchdogMode`` needed it. It did not — nothing in
    ``_runtime`` reads that enum, so the enum belongs in ``execution/`` with the
    watchdog that does, and the substrate needs no ``contracts`` dependency at all.
    """
    from application_sdk.contracts import SerializableEnum as public_enum
    from application_sdk.contracts.base import OutputStatus
    from application_sdk.contracts.base import SerializableEnum as base_enum

    assert public_enum is base_enum
    assert base_enum.__module__ == "application_sdk.contracts.base"
    assert issubclass(OutputStatus, base_enum)

    class AppOwnedStatus(base_enum):
        READY = "ready"

    assert AppOwnedStatus.READY == "ready"
    assert isinstance(AppOwnedStatus.READY, str)


def test_the_watchdog_mode_lives_with_the_watchdog() -> None:
    """``ProgressWatchdogMode`` is the execution layer's vocabulary, not the tracker's.

    Its documented import path is ``execution.progress``, and keeping it there is
    what allows :mod:`application_sdk._runtime.progress` to import nothing from
    ``application_sdk.contracts`` — the dependency that would otherwise drag
    ``storage.ops`` back into the substrate.
    """
    from application_sdk._runtime import progress as substrate
    from application_sdk.execution.progress import ProgressWatchdogMode

    assert not hasattr(substrate, "ProgressWatchdogMode")
    assert ProgressWatchdogMode.WARN == "warn"
