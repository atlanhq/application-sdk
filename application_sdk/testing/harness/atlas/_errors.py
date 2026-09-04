"""Typed error leaves for the Atlas reads and the one Atlas write.

Private module: the leaves that are public surface are re-exported from
:mod:`application_sdk.testing.harness.atlas`. Mirrors
:mod:`application_sdk.testing.harness.cluster._errors`.

One leaf, and it belongs to the write rather than to the reads. Every *read*
here answers with an
:class:`~application_sdk.testing.harness.outcome.Outcome` — an unreadable search
is :class:`~application_sdk.testing.harness.outcome.Indeterminate`, never an
empty result — so there is nothing for a leaf to carry. A rejected *write* is
the opposite shape: the caller is left with no connection at all and no degraded
mode to report, so it raises.

:class:`UnknownConnectorTypeError` moved here from
:mod:`application_sdk.testing.e2e._errors` with
:func:`~application_sdk.testing.harness.atlas.create_connection` (child H on
FND-224), the same way nine AE leaves moved in child F: same class object, same
``code``, re-exported from its old module, so every existing import and
``except`` clause is unchanged. The move is only about direction — a harness
module cannot raise a leaf that lives in the package it is re-expressed under.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from application_sdk.errors.leaves import InvalidInputError, PreconditionError

__all__ = [
    "SeedConnectionNotSearchableError",
    "SeedTreeNotWritableError",
    "UnknownConnectorTypeError",
]


@dataclass(kw_only=True)
class UnknownConnectorTypeError(InvalidInputError):
    """The suite's connection type is not a pyatlan ``AtlanConnectorType``.

    Raised by :func:`~application_sdk.testing.harness.atlas.create_connection`,
    which needs a real connector type to build the Connection's ``category`` and
    ``connectorName``. A suite's ``connection_type or connector_short_name``
    fallback is fine for composing a qualifiedName segment but not for this,
    because an app name and an Atlan catalog type legitimately differ (the
    OpenAPI connector is ``connector_short_name="openapi"`` /
    ``connection_type="api"``). Failing here names the fix rather than surfacing
    a bare pyatlan ``ValueError``.
    """

    code: ClassVar[str] = "INVALID_INPUT_UNKNOWN_CONNECTOR_TYPE"
    field: str | None = "connection_type"


@dataclass(kw_only=True)
class SeedConnectionNotSearchableError(PreconditionError):
    """A seeded lineage-parent Connection never became searchable in Atlas.

    The harness-level sibling of
    :class:`~application_sdk.testing.e2e._errors.SeededConnectionNotSearchableError`
    — a distinct leaf rather than a re-export because the direction of that one
    is wrong for :func:`~application_sdk.testing.harness.atlas.seed.seed_assets`:
    a harness module cannot raise a leaf that lives in the package expressed on
    top of it. Same diagnosis either way: a wedged seed is a harness
    precondition failure, not a connector defect, and writing a skeleton tree
    beneath a connection Atlas cannot see would only defer the failure to the
    first child write.
    """

    code: ClassVar[str] = "PRECONDITION_SEED_CONNECTION_NOT_SEARCHABLE"
    expected_state: str | None = "seeded lineage-parent connection searchable in Atlas"


@dataclass(kw_only=True)
class SeedTreeNotWritableError(PreconditionError):
    """The first skeleton write never ran to a verdict at all.

    Not the 403 case — when the first write is *refused* until the budget runs
    out, :func:`~application_sdk.testing.harness.atlas.seed.seed_assets`
    re-raises the write's own last error so the suite sees the 403 itself. This
    leaf covers the remaining shape: the wait ended with no reading (the loop
    itself was Indeterminate), so whether the connection's policies ever went
    live is unknown rather than known-bad.
    """

    code: ClassVar[str] = "PRECONDITION_SEED_TREE_NOT_WRITABLE"
    expected_state: str | None = "first skeleton write permitted under the seed"
