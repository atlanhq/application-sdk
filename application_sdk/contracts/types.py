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
from typing import Annotated, ClassVar, TypeVar

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
    """

    local_path: str | None = None
    size_bytes: int | None = None
    checksum: str | None = None
    content_type: str = "application/octet-stream"
    is_durable: bool = False
    storage_key: str | None = None

    _CONTENT_TYPES: ClassVar[dict[str, str]] = {
        ".jsonl": "application/x-ndjson",
        ".ndjson": "application/x-ndjson",
        ".json": "application/json",
        ".csv": "text/csv",
        ".tsv": "text/tab-separated-values",
        ".parquet": "application/x-parquet",
    }

    @staticmethod
    def from_local(
        path: str | Path,
        content_type: str | None = None,
    ) -> FileReference:
        """Create an ephemeral FileReference from a local filesystem path."""
        p = Path(path) if not isinstance(path, Path) else path
        if content_type is None:
            content_type = FileReference._CONTENT_TYPES.get(
                p.suffix.lower(), "application/octet-stream"
            )
        return FileReference(
            local_path=str(p),
            size_bytes=p.stat().st_size if p.exists() else 0,
            content_type=content_type,
        )
