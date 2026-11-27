"""Typed contracts for the deprecated upload_to_atlan task.

These contracts cover ``BaseMetadataExtractor.upload_to_atlan``, which is
now a thin ``@deprecated`` wrapper around :meth:`App.upload`.  New code
should call :meth:`App.upload` with
:class:`application_sdk.contracts.storage.UploadInput` instead.

The classes were named ``UploadInput`` / ``UploadOutput``, the same names as
the storage contracts with different fields, so which one an import meant
depended on the module it came from. They are now
:class:`UploadToAtlanInput` / :class:`UploadToAtlanOutput`, and the old names
are deprecated aliases until v4.0.0.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from application_sdk import _deprecation
from application_sdk.contracts.base import Input, Output

__all__ = [
    "UploadInput",
    "UploadOutput",
    "UploadToAtlanInput",
    "UploadToAtlanOutput",
]


class UploadToAtlanInput(Input):
    """Input for the deprecated ``upload_to_atlan`` task.

    Carries a single ``output_path`` that the wrapper forwards as
    ``local_path`` on the storage ``UploadInput``.
    """

    output_path: str = ""
    """Local path (file or directory) to push to the platform via App.upload."""


class UploadToAtlanOutput(Output):
    """Output from the deprecated ``upload_to_atlan`` task."""

    migrated_files: int = 0
    """Number of files successfully uploaded."""

    total_files: int = 0
    """Total number of files attempted (matches ``migrated_files`` on success)."""


#: name -> (replacement, why); served with a warning by ``__getattr__`` below.
_DEPRECATED_CONSTANTS: dict[str, tuple[str, str]] = {
    "UploadInput": (
        "application_sdk.templates.contracts.base_metadata_extraction.UploadToAtlanInput",
        "the same class, renamed so it no longer shares a name with application_sdk.contracts.storage.UploadInput",
    ),
    "UploadOutput": (
        "application_sdk.templates.contracts.base_metadata_extraction.UploadToAtlanOutput",
        "the same class, renamed so it no longer shares a name with application_sdk.contracts.storage.UploadOutput",
    ),
}

if TYPE_CHECKING:
    UploadInput = UploadToAtlanInput
    UploadOutput = UploadToAtlanOutput

__getattr__ = _deprecation.deprecated_module_getattr(
    __name__, _DEPRECATED_CONSTANTS, remove_in="4.0.0"
)
