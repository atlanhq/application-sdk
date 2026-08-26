"""Typed error leaves for the shared test harness.

Private module: leaves that are public surface are re-exported from
:mod:`application_sdk.testing.harness`. Mirrors
:mod:`application_sdk.testing.e2e._errors`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from application_sdk.errors.leaves import (
    InvalidInputError,
    PreconditionError,
    UnimplementedError,
)

__all__ = [
    "HarnessNotBuiltError",
    "MissingTenantEnvError",
    "SyncBridgeInAsyncContextError",
]


@dataclass(kw_only=True)
class SyncBridgeInAsyncContextError(PreconditionError):
    """:func:`~application_sdk.testing.harness.run_sync` was called from a running loop.

    The bridge owns its thread's event loop, so it cannot be re-entered from
    inside one: ``run_until_complete`` on a running loop raises, and standing up
    a second loop on the same thread would fragment every client the harness
    caches per loop. The caller wants the ``_async`` twin of whatever it called.
    """

    code: ClassVar[str] = "PRECONDITION_SYNC_BRIDGE_IN_ASYNC_CONTEXT"
    component: str | None = "harness_sync_bridge"


@dataclass(kw_only=True)
class HarnessNotBuiltError(UnimplementedError, NotImplementedError):
    """A scaffolded harness function whose implementation has not landed yet.

    Inherits :class:`NotImplementedError` alongside the SDK's
    :class:`~application_sdk.errors.leaves.UnimplementedError` leaf so both hold:
    it is a typed leaf carrying a category and an audience, *and* it is still
    what Python's convention — and any reader's ``except`` — expects from a
    function that has not been written.

    :attr:`issue` names the child issue that fills the function in, as a field
    rather than as a substring of the message, so an audit of what is left in the
    scaffold can enumerate it instead of grepping prose.

    Attributes:
        issue: Identifier of the issue that lands the implementation.
        component: Which part of the harness the gap is in.
    """

    code: ClassVar[str] = "UNIMPLEMENTED_HARNESS_NOT_BUILT"
    issue: str | None = None
    component: str | None = "test_harness"


@dataclass(kw_only=True)
class MissingTenantEnvError(InvalidInputError):
    """The environment carries no tenant for the harness to run against.

    Named for the tenant rather than for the harness because that is what is
    missing: ``application_sdk.testing.e2e._errors.MissingHarnessEnvError`` is
    the pre-harness leaf covering the same gap, and it stays until child H
    re-expresses ``testing/e2e`` over this package. Two leaves with one name and
    two codes is the confusion this avoids.

    Attributes:
        field: The variable names that were absent, comma-separated.
    """

    code: ClassVar[str] = "INVALID_INPUT_HARNESS_TENANT_ENV"
    field: str | None = "ATLAN_BASE_URL,ATLAN_API_KEY"
