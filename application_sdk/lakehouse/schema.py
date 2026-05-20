"""Conceptual schema primitives for the lakehouse SDK.

These dataclasses describe what apps want to write — column names, primitive
types, nullability, partitioning — without leaking the underlying Iceberg or
Arrow types. The internal ``_iceberg`` module maps them to ``pyiceberg.Schema``
and ``pyarrow.Schema`` when an SDK call needs them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

# Conceptual primitive types. The SDK maps these to Iceberg + Arrow internally.
FieldType = Literal[
    "string",
    "int",  # 32-bit
    "long",  # 64-bit
    "double",
    "boolean",
    "timestamp",  # microsecond precision, naive UTC (Iceberg convention)
    "date",
]

PartitionTransform = Literal[
    "identity",
    "year",
    "month",
    "day",
    "hour",
    "bucket",
]


@dataclass(frozen=True)
class Field:
    """One column in a lakehouse table."""

    name: str
    type: FieldType
    nullable: bool = True


@dataclass(frozen=True)
class PartitionBy:
    """A single partition specification: column + transform.

    For ``transform="bucket"`` you must also pass ``bucket_count``.
    """

    column: str
    transform: PartitionTransform = "identity"
    bucket_count: int | None = None

    def __post_init__(self) -> None:
        if self.transform == "bucket" and self.bucket_count is None:
            raise ValueError("bucket_count is required when transform='bucket'")


@dataclass(frozen=True)
class Schema:
    """A lakehouse table schema declaration.

    ``partition_by`` may be omitted, a single ``PartitionBy``, or a list for
    multi-column partitioning.
    """

    fields: list[Field]
    partition_by: PartitionBy | list[PartitionBy] | None = None

    def field_names(self) -> list[str]:
        return [f.name for f in self.fields]

    def partition_specs(self) -> list[PartitionBy]:
        if self.partition_by is None:
            return []
        if isinstance(self.partition_by, PartitionBy):
            return [self.partition_by]
        return list(self.partition_by)

    def __post_init__(self) -> None:
        if not self.fields:
            raise ValueError("Schema must have at least one field")
        names = [f.name for f in self.fields]
        if len(names) != len(set(names)):
            raise ValueError(f"Schema has duplicate field names: {names}")
        for spec in self.partition_specs():
            if spec.column not in names:
                raise ValueError(
                    f"PartitionBy references unknown field: {spec.column!r}"
                )


# Re-exported as a convenience builder so apps can write Schema(fields=[...]) ergonomically
__all__ = ["Field", "PartitionBy", "Schema", "FieldType", "PartitionTransform"]
