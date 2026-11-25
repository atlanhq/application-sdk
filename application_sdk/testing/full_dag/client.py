"""Deprecated re-exports of the Automation Engine harness types and ``AEWorkflowClient``.

Every name here resolves to the canonical object, with a ``DeprecationWarning``
on access, so the symbol-removal gate can see the deprecation that
``application_sdk.testing.full_dag`` has carried since 3.24 (FND-4659).

.. deprecated:: 3.24
    Import the DAG types and ``cold_start_submit_kwargs`` from
    ``application_sdk.testing.harness.automation_engine``, their canonical home.
    ``AEWorkflowClient`` is itself deprecated (3.31): use
    ``application_sdk.testing.harness.automation_engine.AEClient`` and
    ``application_sdk.testing.harness.atlas`` instead. Will be removed in v4.0.0.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from application_sdk import _deprecation

if TYPE_CHECKING:
    # Type checkers only: binds nothing at runtime, so every access below still
    # goes through ``__getattr__`` and warns. It is also what lets the symbol
    # gate follow each name to its definition, methods included.
    from application_sdk.testing.e2e.client import AEWorkflowClient  # noqa: F401
    from application_sdk.testing.harness.automation_engine import (  # noqa: F401
        DAGNodeResult,
        DAGNodeStatus,
        DAGRunResult,
        DAGRunStatus,
        cold_start_submit_kwargs,
    )

#: name -> (replacement, why). Served by ``__getattr__`` below (PEP 562), which
#: returns the implementation's object itself, so a patch or an ``isinstance``
#: check sees one object, not two.
_DEPRECATED_CONSTANTS: dict[str, tuple[str, str]] = {
    "AEWorkflowClient": (
        "application_sdk.testing.harness.automation_engine.AEClient and application_sdk.testing.harness.atlas",
        "application_sdk.testing.full_dag is deprecated in favour of application_sdk.testing.e2e (FND-4659)",
    ),
    "DAGNodeResult": (
        "application_sdk.testing.harness.automation_engine.DAGNodeResult",
        "application_sdk.testing.full_dag is deprecated in favour of application_sdk.testing.e2e (FND-4659)",
    ),
    "DAGNodeStatus": (
        "application_sdk.testing.harness.automation_engine.DAGNodeStatus",
        "application_sdk.testing.full_dag is deprecated in favour of application_sdk.testing.e2e (FND-4659)",
    ),
    "DAGRunResult": (
        "application_sdk.testing.harness.automation_engine.DAGRunResult",
        "application_sdk.testing.full_dag is deprecated in favour of application_sdk.testing.e2e (FND-4659)",
    ),
    "DAGRunStatus": (
        "application_sdk.testing.harness.automation_engine.DAGRunStatus",
        "application_sdk.testing.full_dag is deprecated in favour of application_sdk.testing.e2e (FND-4659)",
    ),
    "cold_start_submit_kwargs": (
        "application_sdk.testing.harness.automation_engine.cold_start_submit_kwargs",
        "application_sdk.testing.full_dag is deprecated in favour of application_sdk.testing.e2e (FND-4659)",
    ),
}

#: Where each served name is defined. ``AEWorkflowClient`` is the one name
#: ``testing.e2e.client`` defines; the rest are its re-exports of
#: ``harness.automation_engine``, so they are served from there directly.
_LAZY: dict[str, tuple[str, str]] = {
    "AEWorkflowClient": ("application_sdk.testing.e2e.client", "AEWorkflowClient"),
    "DAGNodeResult": (
        "application_sdk.testing.harness.automation_engine",
        "DAGNodeResult",
    ),
    "DAGNodeStatus": (
        "application_sdk.testing.harness.automation_engine",
        "DAGNodeStatus",
    ),
    "DAGRunResult": (
        "application_sdk.testing.harness.automation_engine",
        "DAGRunResult",
    ),
    "DAGRunStatus": (
        "application_sdk.testing.harness.automation_engine",
        "DAGRunStatus",
    ),
    "cold_start_submit_kwargs": (
        "application_sdk.testing.harness.automation_engine",
        "cold_start_submit_kwargs",
    ),
}

#: Kept so ``from <this module> import *`` still binds every moved name: a star
#: import reads ``__all__`` and then resolves each entry through ``__getattr__``,
#: so each name warns. A literal, because the symbol gate reads it statically.
__all__ = [
    "AEWorkflowClient",
    "DAGNodeResult",
    "DAGNodeStatus",
    "DAGRunResult",
    "DAGRunStatus",
    "cold_start_submit_kwargs",
]


__getattr__ = _deprecation.deprecated_module_getattr(
    __name__,
    _DEPRECATED_CONSTANTS,
    remove_in="4.0.0",
    resolve=_deprecation.lazy_resolver(_LAZY),
)


def __dir__() -> list[str]:
    return sorted({*globals(), *_DEPRECATED_CONSTANTS})
