"""Unified IO interfaces for Application SDK.

This package provides abstract interfaces for reading and writing data in a
consistent way across different formats and backends.

Exposed interfaces:
- Reader: Abstract base class for all readers
- Writer: Abstract base class for all writers
- IOStats: Dataclass for summarizing write statistics

Note: Concrete implementations live alongside this module (e.g., json, parquet,
sql, iceberg) and will adhere to these base contracts.
"""

from .base import BatchDF, DFType, IOStats, Reader, Writer

__all__ = [
    "Reader",
    "Writer",
    "IOStats",
    "DFType",
    "BatchDF",
]
