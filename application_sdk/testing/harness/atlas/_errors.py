"""Typed error leaves for the Atlas reads and the one Atlas write.

Private module: the leaves that are public surface are re-exported from
:mod:`application_sdk.testing.harness.atlas`. Mirrors
:mod:`application_sdk.testing.harness.cluster._errors`.

Two leaves, and both belong to the writes rather than to the reads. Every *read*
here answers with an
:class:`~application_sdk.testing.harness.outcome.Outcome` — an unreadable search
is :class:`~application_sdk.testing.harness.outcome.Indeterminate`, never an
empty result — so there is nothing for a leaf to carry. A rejected *write* is
the opposite shape: the caller is left with no connection at all and no degraded
mode to report, so it raises.

:class:`SeedWriteUnconfirmedError` is the second write's shape: the glossary
seed (FND-5199) registers every GUID Atlas assigns so that teardown can purge
it, and a create that comes back without one cannot be registered.

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

__all__ = ["SeedWriteUnconfirmedError", "UnknownConnectorTypeError"]


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
class SeedWriteUnconfirmedError(PreconditionError):
    """Atlas accepted a seed write but did not report a GUID for what it created.

    Raised by :func:`~application_sdk.testing.harness.atlas.create_glossary` and
    :func:`~application_sdk.testing.harness.atlas.create_glossary_term`. The
    harness tracks seeded assets by GUID so that teardown can purge them, and an
    asset whose GUID is unknown cannot be tracked. Failing here, before the DAG
    runs, is better than a run that passes and leaves an asset nobody reclaims
    on a shared tenant.
    """

    code: ClassVar[str] = "PRECONDITION_SEED_WRITE_UNCONFIRMED"
    expected_state: str | None = "a GUID assigned to every asset the seed created"
