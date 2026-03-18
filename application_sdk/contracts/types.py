"""Type definitions for payload-safe contracts.

Provides types and utilities for contracts that stay within Temporal's 2MB payload limit.

Key types:
- FileReference: Reference to externally-stored data
- MaxItems: Constraint marker for bounded collections
- BoundedList/BoundedDict: Type aliases with size bounds
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, TypeVar

T = TypeVar("T")
K = TypeVar("K")
V = TypeVar("V")


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
    """

    local_path: str | None = None
    storage_path: str | None = None
    is_durable: bool = False
    file_count: int = 1

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
