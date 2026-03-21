"""Type definitions for payload-safe contracts.

Provides types and utilities for contracts that stay within Temporal's 2MB payload limit.

Key types:
- FileReference: Reference to externally-stored data
- GitReference: Reference to a Git repository
- MaxItems: Constraint marker for bounded collections
- BoundedList/BoundedDict: Type aliases with size bounds
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Annotated, TypeVar

from application_sdk.credentials.ref import CredentialRef

T = TypeVar("T")
K = TypeVar("K")
V = TypeVar("V")


class StorageTier(StrEnum):
    """Storage lifecycle tier for a ``FileReference``.

    Controls where the file is stored and whether it is cleaned up automatically
    at the end of a workflow run.

    * ``TRANSIENT``: stored under ``file_refs/`` and deleted by
      ``App.cleanup_storage()`` at the end of every run.  This is the default
      and is appropriate for intermediary files that are only needed between
      tasks.
    * ``RETAINED``: stored under the run-scoped artifacts prefix
      (``artifacts/apps/{app}/workflows/{wf_id}/{run_id}/file_refs/``) and
      **not** deleted by default cleanup.  Use this when you want the file to
      survive the run for post-run investigation.  It can still be removed with
      ``StorageCleanupInput(include_prefix_cleanup=True)``.
    * ``PERSISTENT``: stored under ``persistent-artifacts/`` and never deleted
      by cleanup.  Use this for files that must survive across multiple runs.
    """

    TRANSIENT = "transient"
    RETAINED = "retained"
    PERSISTENT = "persistent"

    # ------------------------------------------------------------------
    # Canonical tier → object-store path helpers
    #
    # These are the *single source of truth* for tier-based path generation.
    # All other modules (storage/reference.py, app/base.py, etc.) delegate
    # here instead of duplicating conditional logic.
    #
    # Adding methods to a StrEnum does NOT affect Temporal serde — the
    # payload converter only uses the string value ("transient" etc.).
    # ------------------------------------------------------------------

    def upload_prefix(self, *, run_prefix: str = "", app_name: str = "") -> str:
        """Return the base object-store prefix for ``App.upload`` at this tier.

        This prefix is used as the destination root when no explicit
        ``storage_path`` is given to :meth:`~application_sdk.app.base.App.upload`.

        * ``TRANSIENT``  → ``file_refs`` (cleaned at end of run)
        * ``RETAINED``   → *run_prefix* — requires *run_prefix*
        * ``PERSISTENT`` → ``persistent-artifacts/apps/{app_name}``
        """
        if self is StorageTier.TRANSIENT:
            return "file_refs"
        if self is StorageTier.RETAINED:
            if not run_prefix:
                raise ValueError(
                    "run_prefix is required when computing upload prefix for RETAINED tier"
                )
            return run_prefix
        # PERSISTENT
        return (
            f"persistent-artifacts/apps/{app_name}"
            if app_name
            else "persistent-artifacts"
        )

    def _make_file_ref_path(
        self, *, suffix: str = "", run_prefix: str = "", app_name: str = ""
    ) -> str:
        """Return a unique single-file object-store key for auto-persisted ``FileReference`` objects.

        * ``TRANSIENT``  → ``file_refs/{uuid}{suffix}``
        * ``RETAINED``   → ``{run_prefix}/file_refs/{uuid}{suffix}``
        * ``PERSISTENT`` → ``persistent-artifacts/apps/{app_name}/file_refs/{uuid}{suffix}``

        .. warning::
            Uses ``uuid.uuid4()`` internally, which is **not** deterministic.
            This method must only be called from within a Temporal **activity**
            (i.e. a ``@task``-decorated function or a utility called from one).
            Calling it from ``@workflow.defn`` code will violate Temporal's
            sandbox non-determinism restrictions.

        Args:
            suffix: File extension including the leading dot (e.g. ``".parquet"``).
            run_prefix: Run-scoped base prefix.  Required for ``RETAINED``.
            app_name: Application name.  Used by ``PERSISTENT``.
        """
        import uuid

        base = self._file_ref_base(run_prefix=run_prefix, app_name=app_name)
        return f"{base}/{uuid.uuid4().hex}{suffix}"

    def _make_file_ref_prefix(self, *, run_prefix: str = "", app_name: str = "") -> str:
        """Return a unique directory prefix for auto-persisted ``FileReference`` directories.

        Identical to :meth:`_make_file_ref_path` with a trailing slash and no
        suffix — used for directory uploads.

        .. warning::
            Uses ``uuid.uuid4()`` internally — **activity-context only**.
            See :meth:`_make_file_ref_path` for details.
        """
        import uuid

        base = self._file_ref_base(run_prefix=run_prefix, app_name=app_name)
        return f"{base}/{uuid.uuid4().hex}/"

    def _file_ref_base(self, *, run_prefix: str = "", app_name: str = "") -> str:
        """Return the base prefix under which ``file_refs/{uid}`` paths are stored."""
        if self is StorageTier.TRANSIENT:
            return "file_refs"
        if self is StorageTier.RETAINED:
            if not run_prefix:
                raise ValueError(
                    "run_prefix is required when persisting a RETAINED-tier FileReference"
                )
            return f"{run_prefix}/file_refs"
        # PERSISTENT
        return (
            f"persistent-artifacts/apps/{app_name}/file_refs"
            if app_name
            else "persistent-artifacts/file_refs"
        )


@dataclass(frozen=True)
class MaxItems:
    """Constraint marker indicating maximum collection size.

    Use with Annotated to declare bounded collections in contracts:

        @dataclass
        class MyInput(Input):
            settings: Annotated[dict[str, str], MaxItems(100)]
            items: Annotated[list[Record], MaxItems(1000)]
    """

    limit: int
    """Maximum number of items allowed in the collection."""


BoundedList = Annotated[list[T], MaxItems]
"""Bounded list type. Use: Annotated[list[T], MaxItems(N)]"""

BoundedDict = Annotated[dict[K, V], MaxItems]
"""Bounded dict type. Use: Annotated[dict[K, V], MaxItems(N)]"""


@dataclass(frozen=True)
class FileReference:
    """Reference to externally-stored data (for large payloads).

    Use this instead of embedding large data directly in Input/Output.
    Store the actual data in a file/blob storage and pass only this reference.

    Temporal has a 2MB payload limit. Large data (files, blobs, large datasets)
    should be stored externally and referenced via FileReference.

    Attributes:
        local_path: Local filesystem path to the file or directory.
        storage_path: Object-store key (single file) or prefix (directory).
        is_durable: ``True`` when the data has been uploaded to the object
            store and ``storage_path`` is set.
        file_count: Number of files this reference covers.  Defaults to 1
            for single-file references; set to the total number of files for
            directory uploads/downloads.
        tier: Storage lifecycle tier.  Controls where the file is stored and
            whether it is automatically cleaned up at the end of a run.
            Defaults to ``StorageTier.TRANSIENT`` (cleaned up automatically).
            Set to ``StorageTier.RETAINED`` to keep the file under the
            run-scoped prefix for post-run investigation, or
            ``StorageTier.PERSISTENT`` to keep it indefinitely under
            ``persistent-artifacts/``.
    """

    local_path: str | None = None
    storage_path: str | None = None
    is_durable: bool = False
    file_count: int = 1
    tier: StorageTier = StorageTier.TRANSIENT

    @staticmethod
    def from_local(
        path: str | Path,
    ) -> "FileReference":
        """Create an ephemeral FileReference from a local filesystem path.

        Args:
            path: Local file or directory path.

        Returns:
            An ephemeral ``FileReference`` (``is_durable=False``) with
            ``local_path`` set.  ``file_count`` is always 1; use
            :func:`~application_sdk.storage.transfer.upload` if you need
            accurate file counts for directories.
        """
        p = Path(path) if not isinstance(path, Path) else path
        return FileReference(
            local_path=str(p),
        )


@dataclass(frozen=True)
class GitReference:
    """Reference to a Git repository for workflow inputs.

    Temporal-safe data carrier for specifying a git repo to clone.
    Checkout precedence: commit > tag > branch.
    """

    repo_url: str
    branch: str = "main"
    path: str = ""
    tag: str = ""
    commit: str = ""
    credential: "CredentialRef | None" = None
