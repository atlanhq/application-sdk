# ruff: noqa: E402
"""Deprecated: use application_sdk.common.models instead."""

import warnings

warnings.warn(
    "application_sdk.activities.common.models is deprecated and will be removed in v3.1.0. "
    "Use application_sdk.common.models (TaskStatistics, TaskResult) instead.",
    DeprecationWarning,
    stacklevel=2,
)

from application_sdk.common.models import TaskResult, TaskStatistics  # noqa: F401

# Backward-compatible aliases — removed when this shim is deleted in v3.1.0.
ActivityStatistics = TaskStatistics
ActivityResult = TaskResult
