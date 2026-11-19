"""FND-4881: the run-end merge is scheduled only when the run skipped records."""

from __future__ import annotations

from unittest import mock

import pytest

from application_sdk.app import base
from application_sdk.contracts.skipped_records import (
    MergeSkippedRecordsInput,
    MergeSkippedRecordsOutput,
)
from application_sdk.outputs import Metric, OutputCollector, current_outputs
from application_sdk.outputs.skipped import (
    SKIPPED_RECORDS_METRIC,
    SKIPPED_RECORDS_TRUNCATED_METRIC,
    SKIPPED_RECORDS_UNPERSISTED_METRIC,
    MergeResult,
)


@pytest.fixture
def collector():
    c = OutputCollector()
    token = current_outputs.set(c)
    yield c
    current_outputs.reset(token)


@pytest.fixture
def app():
    instance = mock.MagicMock()
    instance.merge_skipped_records = mock.AsyncMock(
        return_value=MergeSkippedRecordsOutput()
    )
    return instance


async def test_no_skips_schedules_nothing(collector, app):
    collector.add_metric(Metric(name="tables", value=5))
    await base._merge_skipped_records(app)
    app.merge_skipped_records.assert_not_awaited()


async def test_skips_schedule_the_merge_with_counts(collector, app):
    collector.add_metric(Metric(name=SKIPPED_RECORDS_METRIC, value=7))
    collector.add_metric(Metric(name=SKIPPED_RECORDS_UNPERSISTED_METRIC, value=2))
    await base._merge_skipped_records(app)
    app.merge_skipped_records.assert_awaited_once_with(
        MergeSkippedRecordsInput(skipped_count=7, unpersisted_count=2)
    )


async def test_merge_failure_fails_open_and_counts_the_rest(collector, app):
    collector.add_metric(Metric(name=SKIPPED_RECORDS_METRIC, value=7))
    collector.add_metric(Metric(name=SKIPPED_RECORDS_UNPERSISTED_METRIC, value=2))
    app.merge_skipped_records.side_effect = RuntimeError("store down")
    with mock.patch.object(base, "_safe_log") as safe_log:
        await base._merge_skipped_records(app)

    assert safe_log.call_args.args[0] == "error"
    assert collector.numeric_metric(SKIPPED_RECORDS_UNPERSISTED_METRIC) == 7


async def test_task_records_truncation(collector):
    task_fn = base.App.merge_skipped_records
    with (
        mock.patch(
            "application_sdk.execution.build_output_path", return_value="artifacts/run"
        ),
        mock.patch(
            "application_sdk.execution._temporal.activity_utils.run_temp_dir",
            return_value="/tmp/x",
        ),
        mock.patch(
            "application_sdk.outputs.skipped.merge_run_skips",
            new_callable=mock.AsyncMock,
            return_value=MergeResult(
                key="artifacts/run/skipped-records.ndjson", kept=5
            ),
        ),
    ):
        out = await task_fn(
            mock.MagicMock(),
            MergeSkippedRecordsInput(skipped_count=10, unpersisted_count=2),
        )

    assert (out.kept, out.truncated) == (5, 3)
    assert collector.numeric_metric(SKIPPED_RECORDS_TRUNCATED_METRIC) == 3
