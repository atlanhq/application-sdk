"""Typed contracts for the deprecated upload_to_atlan task.

These contracts cover ``BaseMetadataExtractor.upload_to_atlan``, which is
now a thin ``@deprecated`` wrapper around :meth:`App.upload`.  New code
should import :class:`UploadInput` / :class:`UploadOutput` directly from
``application_sdk.contracts.storage`` instead.
"""

from __future__ import annotations

from application_sdk.contracts.base import Input, Output


class UploadInput(Input):
    """Input for the deprecated ``upload_to_atlan`` task.

    Carries a single ``output_path`` that the wrapper forwards as
    ``local_path`` on the storage ``UploadInput``.
    """

    output_path: str = ""
    """Local path (file or directory) to push to the platform via App.upload."""


class UploadOutput(Output):
    """Output from the deprecated ``upload_to_atlan`` task."""

    migrated_files: int = 0
    """Number of files successfully uploaded."""

    total_files: int = 0
    """Total number of files attempted (matches ``migrated_files`` on success)."""
