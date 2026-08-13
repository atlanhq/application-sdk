"""Shared utilities for Atlan application connectors.

Public API::

    from application_sdk.common import (
        # SQL filter helpers
        read_sql_files, prepare_query, prepare_filters, normalize_filters,
        # App-agnostic filter matching (regex vs exact)
        FilterPattern, filter_matches,
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

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from application_sdk.common.concurrency import (
        get_actual_cpu_count,
        get_safe_num_threads,
    )
    from application_sdk.common.filter_matching import FilterPattern, filter_matches
    from application_sdk.common.models import TaskResult, TaskStatistics
    from application_sdk.common.sql_filters import (
        normalize_filters,
        prepare_filters,
        prepare_query,
        read_sql_files,
    )
    from application_sdk.common.types import DataframeType

# BOOT-TIME: re-exports are lazy (PEP 562). Importing any submodule of
# application_sdk.common executes this __init__; the eager version pulled
# sql_filters -> errors -> pydantic wire models onto every boot path.
_LAZY_EXPORTS = {
    "read_sql_files": "application_sdk.common.sql_filters",
    "prepare_query": "application_sdk.common.sql_filters",
    "prepare_filters": "application_sdk.common.sql_filters",
    "normalize_filters": "application_sdk.common.sql_filters",
    "FilterPattern": "application_sdk.common.filter_matching",
    "filter_matches": "application_sdk.common.filter_matching",
    "get_actual_cpu_count": "application_sdk.common.concurrency",
    "get_safe_num_threads": "application_sdk.common.concurrency",
    "TaskStatistics": "application_sdk.common.models",
    "TaskResult": "application_sdk.common.models",
    "DataframeType": "application_sdk.common.types",
}


def __getattr__(name: str):
    module_path = _LAZY_EXPORTS.get(name)
    if module_path is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib

    value = getattr(importlib.import_module(module_path), name)
    globals()[name] = value
    return value

__all__ = [
    # SQL filter helpers
    "read_sql_files",
    "prepare_query",
    "prepare_filters",
    "normalize_filters",
    # App-agnostic filter matching (regex vs exact)
    "FilterPattern",
    "filter_matches",
    # Concurrency helpers
    "get_actual_cpu_count",
    "get_safe_num_threads",
    # Task models
    "TaskStatistics",
    "TaskResult",
    # DataFrame type enum
    "DataframeType",
]
