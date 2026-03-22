"""Typed contracts for base (non-SQL) metadata extraction.

These contracts cover the upload_to_atlan task provided by BaseMetadataExtractor.
Subclasses define their own app-level Input/Output for run().
"""

from __future__ import annotations

from application_sdk.contracts.base import Input, Output


class UploadInput(Input):
    """Input for the upload_to_atlan task."""

    output_path: str = ""
    """Object store prefix to migrate from the deployment store to the upstream store."""


class UploadOutput(Output):
    """Output from the upload_to_atlan task."""

    migrated_files: int = 0
    """Number of files successfully migrated."""

    total_files: int = 0
    """Total number of files found for migration."""
