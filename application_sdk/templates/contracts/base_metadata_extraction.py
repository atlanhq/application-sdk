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

    skip_if_exists: bool = True
    """SHA-compare each local file against the remote sidecar and skip
    upload when the hash matches.

    Defaults to ``True`` because the v3 ``SqlApp`` template's per-entity
    extract/transform activities now pre-set canonical ``storage_path``
    values on their emitted ``FileReference`` objects — the activity
    interceptor's persist step already uploads every raw/transformed
    file to the same key this task's directory walk would produce
    (``<run_prefix>/raw/<entity>/records.json`` /
    ``<run_prefix>/transformed/<entity>/entities.json``). With
    ``skip_if_exists=True`` the walk becomes a safety net: any side
    files a subclass wrote under ``output_path`` outside the
    FileReference contract (e.g. mysql-app's ``lineage_stage/`` or
    pre-FileReference ``extras-procedure/`` outputs) still get
    uploaded, but the entity files already in the store are
    SHA-skipped — no doubled network IO, no doubled object-store
    write cost.

    Set to ``False`` for callers that genuinely want the directory
    walk to overwrite remote files unconditionally (e.g. recovery
    workflows that need to force-re-upload regardless of the stored
    hash, or apps that don't use the FileReference interceptor at
    all and therefore have nothing pre-uploaded to compare against).
    """


class UploadOutput(Output):
    """Output from the deprecated ``upload_to_atlan`` task."""

    migrated_files: int = 0
    """Number of files successfully uploaded."""

    total_files: int = 0
    """Total number of files attempted (matches ``migrated_files`` on success)."""
