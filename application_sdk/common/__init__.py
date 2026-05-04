"""Shared utilities for Atlan application connectors.

Public API::

    from application_sdk.common import (
        # SQL filter helpers
        read_sql_files, prepare_query, prepare_filters, normalize_filters,
        # Concurrency helpers
        get_actual_cpu_count, get_safe_num_threads,
        # Task models
        TaskStatistics, TaskResult,
        # DataFrame type enum
        DataframeType,
    )

Note: ``application_sdk.common.error_codes`` is retained for back-compat with
v2 connectors. Prefer ``application_sdk.errors`` for new code.
"""

from application_sdk.common.concurrency import (
    get_actual_cpu_count,
    get_safe_num_threads,
)
from application_sdk.common.models import TaskResult, TaskStatistics
from application_sdk.common.sql_filters import (
    normalize_filters,
    prepare_filters,
    prepare_query,
    read_sql_files,
)
from application_sdk.common.types import DataframeType

__all__ = [
    # SQL filter helpers
    "read_sql_files",
    "prepare_query",
    "prepare_filters",
    "normalize_filters",
    # Concurrency helpers
    "get_actual_cpu_count",
    "get_safe_num_threads",
    # Task models
    "TaskStatistics",
    "TaskResult",
    # DataFrame type enum
    "DataframeType",
]
