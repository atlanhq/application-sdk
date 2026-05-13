"""Typed precondition errors for closed format readers/writers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from application_sdk.errors import DependencyUnavailableError, PreconditionError


@dataclass(kw_only=True)
class ClosedWriterError(PreconditionError):
    """Operation called on a writer that has already been closed."""

    code: ClassVar[str] = "PRECONDITION_WRITER_CLOSED"


@dataclass(kw_only=True)
class ClosedReaderError(PreconditionError):
    """Operation called on a reader that has already been closed."""

    code: ClassVar[str] = "PRECONDITION_READER_CLOSED"


@dataclass(kw_only=True)
class ObjectStoreReadError(DependencyUnavailableError):
    """Object store read or download operation failed."""

    code: ClassVar[str] = "DEPENDENCY_UNAVAILABLE_OBJECT_STORE_READ"
