"""Contracts for the ``App.merge_fetch_failures`` framework task (FND-5294)."""

from __future__ import annotations

from pydantic import Field

from application_sdk.contracts.base import Input, Output

__all__ = [
    "MergeFetchFailuresInput",
    "MergeFetchFailuresOutput",
]


class MergeFetchFailuresInput(Input):
    """Input for ``App.merge_fetch_failures``.

    Args:
        failure_count: The run's summed ``fetch-failures`` metric.
        unpersisted_count: The run's summed ``fetch-failures-unpersisted``
            metric — failures whose activity part failed to upload.
    """

    failure_count: int = Field(default=0, ge=0)
    unpersisted_count: int = Field(default=0, ge=0)


class MergeFetchFailuresOutput(Output):
    """Output from ``App.merge_fetch_failures``.

    Args:
        key: Object-store key of the merged file, or ``None`` when there were
            no parts to merge.
        kept: Lines in the merged file.
        truncated: Failures counted but dropped by ``MAX_SKIPPED_RECORDS``.
    """

    key: str | None = None
    kept: int = 0
    truncated: int = 0
