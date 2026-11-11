"""Contracts for the ``App.merge_skipped_records`` framework task (FND-4881)."""

from __future__ import annotations

from pydantic import Field

from application_sdk.contracts.base import Input, Output


class MergeSkippedRecordsInput(Input):
    """Input for ``App.merge_skipped_records``.

    Args:
        skipped_count: The run's summed ``skipped-records`` metric.
        unpersisted_count: The run's summed ``skipped-records-unpersisted``
            metric — skips whose activity part failed to upload.
    """

    skipped_count: int = Field(default=0, ge=0)
    unpersisted_count: int = Field(default=0, ge=0)


class MergeSkippedRecordsOutput(Output):
    """Output from ``App.merge_skipped_records``.

    Args:
        key: Object-store key of the merged file, or ``None`` when there were
            no parts to merge.
        kept: Lines in the merged file.
        truncated: Skips counted but dropped by ``MAX_SKIPPED_RECORDS``.
    """

    key: str | None = None
    kept: int = 0
    truncated: int = 0
