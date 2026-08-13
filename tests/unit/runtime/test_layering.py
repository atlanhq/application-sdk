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
    "application_sdk._runtime.enums",
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


def test_serializable_enum_keeps_its_public_identity() -> None:
    """The contracts re-export is the substrate class, so subclass checks hold.

    ``SerializableEnum`` moved down to ``_runtime.enums`` because
    ``_runtime.progress`` needs it and ``contracts`` is not importable from
    ``storage/``. Two distinct classes would silently break every
    ``issubclass`` check and Temporal round-trip that relies on the base.
    """
    from application_sdk._runtime.enums import SerializableEnum as substrate_enum
    from application_sdk.contracts import SerializableEnum as public_enum
    from application_sdk.contracts.base import OutputStatus

    assert public_enum is substrate_enum
    assert issubclass(OutputStatus, substrate_enum)
