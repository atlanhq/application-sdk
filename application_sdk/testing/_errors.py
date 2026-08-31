"""Typed error leaves for the top-level testing helpers.

Sibling of :mod:`application_sdk.testing.integration._errors`, which holds the
same thing for the integration-tier runner. Each module's leaves sit beside the
module that raises them, so there is one place to look per import namespace.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from application_sdk.errors.leaves import InvalidInputError, PreconditionError


@dataclass(kw_only=True)
class FakeSourceRouteError(InvalidInputError):
    """A route was registered with no HTTP methods."""

    code: ClassVar[str] = "INVALID_INPUT_FAKE_SOURCE_ROUTE"
    field: str | None = "methods"


@dataclass(kw_only=True)
class FakeSourceNotRunningError(PreconditionError):
    """``base_url`` or ``port`` was read before the server was started."""

    code: ClassVar[str] = "PRECONDITION_FAKE_SOURCE_NOT_RUNNING"
