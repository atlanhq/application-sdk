"""Deprecated re-exports of ``application_sdk.testing.e2e.client`` (canonical location).

Every name here resolves to the canonical object, with a ``DeprecationWarning``
on access, so the symbol-removal gate can see the deprecation that
``application_sdk.testing.full_dag`` has carried since 3.24 (FND-4659).

.. deprecated:: 3.24
    Import from ``application_sdk.testing.e2e.client``. Will be removed in v4.0.0.
"""

from __future__ import annotations

import importlib
import warnings
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    # Type checkers only: binds nothing at runtime, so every access below still
    # goes through ``__getattr__`` and warns. It is also what lets the symbol
    # gate follow each name to its definition, methods included.
    from application_sdk.testing.e2e.client import (  # noqa: F401
        AEWorkflowClient,
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
        "application_sdk.testing.e2e.client",
        "application_sdk.testing.full_dag is deprecated in favour of application_sdk.testing.e2e (FND-4659)",
    ),
    "DAGNodeResult": (
        "application_sdk.testing.e2e.client",
        "application_sdk.testing.full_dag is deprecated in favour of application_sdk.testing.e2e (FND-4659)",
    ),
    "DAGNodeStatus": (
        "application_sdk.testing.e2e.client",
        "application_sdk.testing.full_dag is deprecated in favour of application_sdk.testing.e2e (FND-4659)",
    ),
    "DAGRunResult": (
        "application_sdk.testing.e2e.client",
        "application_sdk.testing.full_dag is deprecated in favour of application_sdk.testing.e2e (FND-4659)",
    ),
    "DAGRunStatus": (
        "application_sdk.testing.e2e.client",
        "application_sdk.testing.full_dag is deprecated in favour of application_sdk.testing.e2e (FND-4659)",
    ),
    "cold_start_submit_kwargs": (
        "application_sdk.testing.e2e.client",
        "application_sdk.testing.full_dag is deprecated in favour of application_sdk.testing.e2e (FND-4659)",
    ),
}

_LAZY: dict[str, tuple[str, str]] = {
    "AEWorkflowClient": ("application_sdk.testing.e2e.client", "AEWorkflowClient"),
    "DAGNodeResult": ("application_sdk.testing.e2e.client", "DAGNodeResult"),
    "DAGNodeStatus": ("application_sdk.testing.e2e.client", "DAGNodeStatus"),
    "DAGRunResult": ("application_sdk.testing.e2e.client", "DAGRunResult"),
    "DAGRunStatus": ("application_sdk.testing.e2e.client", "DAGRunStatus"),
    "cold_start_submit_kwargs": (
        "application_sdk.testing.e2e.client",
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


def __getattr__(name: str) -> object:
    """Serve the deprecated names once more, with a deprecation warning (PEP 562)."""
    entry = _DEPRECATED_CONSTANTS.get(name)
    if entry is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    replacement, note = entry
    warnings.warn(
        f"{name} is deprecated; use {replacement} instead — {note}. "
        "Will be removed in v4.0.0.",
        DeprecationWarning,
        stacklevel=2,
    )
    module, attr = _LAZY[name]
    return getattr(importlib.import_module(module), attr)


def __dir__() -> list[str]:
    return sorted({*globals(), *_DEPRECATED_CONSTANTS})
