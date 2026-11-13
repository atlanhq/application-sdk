"""``TaskResult`` is deprecated; ``TaskStatistics`` is not."""

import warnings

from application_sdk.common.models import TaskResult, TaskStatistics


def test_task_result_is_marked_deprecated() -> None:
    """Static marker only: calling a TypedDict builds a plain dict, so nothing
    can warn at runtime. Type checkers and B001 read ``__deprecated__``."""
    notice = TaskResult.__deprecated__  # type: ignore[attr-defined]
    assert "TaskStatistics" in notice
    assert "removed in v4.0" in notice


def test_task_statistics_does_not_warn() -> None:
    with warnings.catch_warnings():
        warnings.simplefilter("error", DeprecationWarning)
        TaskStatistics(total_record_count=1)


def test_common_package_still_reexports_task_result() -> None:
    from application_sdk import common

    assert common.TaskResult is TaskResult
