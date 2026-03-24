"""Column extraction utilities for incremental metadata extraction.

This package provides generic helpers for incremental column extraction:
- analysis: Daft-based table state analysis to identify tables needing extraction
- backfill: DuckDB-based backfill detection comparing current vs previous state
"""

from application_sdk.common.incremental.column_extraction.analysis import (
    get_tables_needing_column_extraction,
    get_transformed_dir,
)
from application_sdk.common.incremental.column_extraction.backfill import (
    get_backfill_tables,
)

__all__ = [
    "get_tables_needing_column_extraction",
    "get_transformed_dir",
    "get_backfill_tables",
]
