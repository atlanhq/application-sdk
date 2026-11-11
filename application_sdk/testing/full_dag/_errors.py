"""Deprecated re-exports of the harness errors ``application_sdk.testing.e2e`` raises.

Every name here resolves to the canonical object, with a ``DeprecationWarning``
on access, so the symbol-removal gate can see the deprecation that
``application_sdk.testing.full_dag`` has carried since 3.24 (FND-4659).

.. deprecated:: 3.24
    Move to :class:`~application_sdk.testing.e2e.BaseE2ETest`, and catch the
    :mod:`application_sdk.errors` category leaf each class subclasses rather than
    the class itself: ``application_sdk.testing.e2e._errors`` is private. Will be
    removed in v4.0.0.
"""

from __future__ import annotations

import importlib
import warnings
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    # Type checkers only: binds nothing at runtime, so every access below still
    # goes through ``__getattr__`` and warns. It is also what lets the symbol
    # gate follow each name to its definition, methods included.
    from application_sdk.testing.e2e._errors import (  # noqa: F401
        AdminRoleNotResolvedError,
        AgentSpecRequiredError,
        AtlanApiHttpError,
        AtlanApiResponseInvariantError,
        AtlanApiTimeoutError,
        HarnessMethodNotImplementedError,
        ManifestDagMissingError,
        ManifestFileNotFoundError,
        MissingHarnessClassAttrError,
        MissingHarnessEnvError,
    )

#: name -> (replacement, why). Served by ``__getattr__`` below (PEP 562), which
#: returns the implementation's object itself, so a patch or an ``isinstance``
#: check sees one object, not two.
_DEPRECATED_CONSTANTS: dict[str, tuple[str, str]] = {
    "AdminRoleNotResolvedError": (
        "application_sdk.testing.e2e.BaseE2ETest, catching the application_sdk.errors leaf this class subclasses",
        "application_sdk.testing.full_dag is deprecated in favour of application_sdk.testing.e2e (FND-4659)",
    ),
    "AgentSpecRequiredError": (
        "application_sdk.testing.e2e.BaseE2ETest, catching the application_sdk.errors leaf this class subclasses",
        "application_sdk.testing.full_dag is deprecated in favour of application_sdk.testing.e2e (FND-4659)",
    ),
    "AtlanApiHttpError": (
        "application_sdk.testing.e2e.BaseE2ETest, catching the application_sdk.errors leaf this class subclasses",
        "application_sdk.testing.full_dag is deprecated in favour of application_sdk.testing.e2e (FND-4659)",
    ),
    "AtlanApiResponseInvariantError": (
        "application_sdk.testing.e2e.BaseE2ETest, catching the application_sdk.errors leaf this class subclasses",
        "application_sdk.testing.full_dag is deprecated in favour of application_sdk.testing.e2e (FND-4659)",
    ),
    "AtlanApiTimeoutError": (
        "application_sdk.testing.e2e.BaseE2ETest, catching the application_sdk.errors leaf this class subclasses",
        "application_sdk.testing.full_dag is deprecated in favour of application_sdk.testing.e2e (FND-4659)",
    ),
    "HarnessMethodNotImplementedError": (
        "application_sdk.testing.e2e.BaseE2ETest, catching the application_sdk.errors leaf this class subclasses",
        "application_sdk.testing.full_dag is deprecated in favour of application_sdk.testing.e2e (FND-4659)",
    ),
    "ManifestDagMissingError": (
        "application_sdk.testing.e2e.BaseE2ETest, catching the application_sdk.errors leaf this class subclasses",
        "application_sdk.testing.full_dag is deprecated in favour of application_sdk.testing.e2e (FND-4659)",
    ),
    "ManifestFileNotFoundError": (
        "application_sdk.testing.e2e.BaseE2ETest, catching the application_sdk.errors leaf this class subclasses",
        "application_sdk.testing.full_dag is deprecated in favour of application_sdk.testing.e2e (FND-4659)",
    ),
    "MissingHarnessClassAttrError": (
        "application_sdk.testing.e2e.BaseE2ETest, catching the application_sdk.errors leaf this class subclasses",
        "application_sdk.testing.full_dag is deprecated in favour of application_sdk.testing.e2e (FND-4659)",
    ),
    "MissingHarnessEnvError": (
        "application_sdk.testing.e2e.BaseE2ETest, catching the application_sdk.errors leaf this class subclasses",
        "application_sdk.testing.full_dag is deprecated in favour of application_sdk.testing.e2e (FND-4659)",
    ),
}

_LAZY: dict[str, tuple[str, str]] = {
    "AdminRoleNotResolvedError": (
        "application_sdk.testing.e2e._errors",
        "AdminRoleNotResolvedError",
    ),
    "AgentSpecRequiredError": (
        "application_sdk.testing.e2e._errors",
        "AgentSpecRequiredError",
    ),
    "AtlanApiHttpError": ("application_sdk.testing.e2e._errors", "AtlanApiHttpError"),
    "AtlanApiResponseInvariantError": (
        "application_sdk.testing.e2e._errors",
        "AtlanApiResponseInvariantError",
    ),
    "AtlanApiTimeoutError": (
        "application_sdk.testing.e2e._errors",
        "AtlanApiTimeoutError",
    ),
    "HarnessMethodNotImplementedError": (
        "application_sdk.testing.e2e._errors",
        "HarnessMethodNotImplementedError",
    ),
    "ManifestDagMissingError": (
        "application_sdk.testing.e2e._errors",
        "ManifestDagMissingError",
    ),
    "ManifestFileNotFoundError": (
        "application_sdk.testing.e2e._errors",
        "ManifestFileNotFoundError",
    ),
    "MissingHarnessClassAttrError": (
        "application_sdk.testing.e2e._errors",
        "MissingHarnessClassAttrError",
    ),
    "MissingHarnessEnvError": (
        "application_sdk.testing.e2e._errors",
        "MissingHarnessEnvError",
    ),
}

#: Kept so ``from <this module> import *`` still binds every moved name: a star
#: import reads ``__all__`` and then resolves each entry through ``__getattr__``,
#: so each name warns. A literal, because the symbol gate reads it statically.
__all__ = [
    "AdminRoleNotResolvedError",
    "AgentSpecRequiredError",
    "AtlanApiHttpError",
    "AtlanApiResponseInvariantError",
    "AtlanApiTimeoutError",
    "HarnessMethodNotImplementedError",
    "ManifestDagMissingError",
    "ManifestFileNotFoundError",
    "MissingHarnessClassAttrError",
    "MissingHarnessEnvError",
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
