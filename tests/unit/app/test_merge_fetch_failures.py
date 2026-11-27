"""FND-5294: the run-end merge is scheduled only when the run recorded fetch failures."""

from __future__ import annotations

from unittest import mock

import pytest

from application_sdk.app import base
from application_sdk.contracts.fetch_failures import (
    MergeFetchFailuresInput,
    MergeFetchFailuresOutput,
)
from application_sdk.execution._temporal import app_workflow
from application_sdk.outputs import Metric, OutputCollector, current_outputs
from application_sdk.outputs.fetch_failures import (
    FETCH_FAILURES_LAYOUT,
    FETCH_FAILURES_METRIC,
    FETCH_FAILURES_TRUNCATED_METRIC,
    FETCH_FAILURES_UNPERSISTED_METRIC,
)
from application_sdk.outputs.skipped import MergeResult


@pytest.fixture
def collector():
    c = OutputCollector()
    token = current_outputs.set(c)
    yield c
    current_outputs.reset(token)


@pytest.fixture(autouse=True)
def patched():
    """Run as new workflow code: the fetch-failures-merge patch is applied."""
    with mock.patch(
        "application_sdk.app.base.workflow.patched", return_value=True
    ) as p:
        yield p


@pytest.fixture
def app():
    instance = mock.MagicMock()
    instance.merge_fetch_failures = mock.AsyncMock(
        return_value=MergeFetchFailuresOutput()
    )
    return instance


async def test_no_failures_schedules_nothing(collector, app):
    collector.add_metric(Metric(name="reports", value=5))
    await app_workflow._merge_fetch_failures(app)
    app.merge_fetch_failures.assert_not_awaited()


async def test_failures_schedule_the_merge_with_counts(collector, app):
    collector.add_metric(Metric(name=FETCH_FAILURES_METRIC, value=7))
    collector.add_metric(Metric(name=FETCH_FAILURES_UNPERSISTED_METRIC, value=2))
    await app_workflow._merge_fetch_failures(app)
    app.merge_fetch_failures.assert_awaited_once_with(
        MergeFetchFailuresInput(failure_count=7, unpersisted_count=2)
    )


async def test_replay_of_pre_patch_history_schedules_nothing(collector, app, patched):
    # An app on an older SDK emitted its own ``fetch-failures`` metric; replaying
    # that history must not gain the merge activity.
    patched.return_value = False
    collector.add_metric(Metric(name=FETCH_FAILURES_METRIC, value=3))
    await app_workflow._merge_fetch_failures(app)
    patched.assert_called_once_with("fetch-failures-merge")
    app.merge_fetch_failures.assert_not_awaited()


async def test_no_failures_does_not_record_the_patch_marker(collector, app, patched):
    await app_workflow._merge_fetch_failures(app)
    patched.assert_not_called()


async def test_merge_failure_fails_open_and_counts_the_rest(collector, app):
    collector.add_metric(Metric(name=FETCH_FAILURES_METRIC, value=7))
    collector.add_metric(Metric(name=FETCH_FAILURES_UNPERSISTED_METRIC, value=2))
    app.merge_fetch_failures.side_effect = RuntimeError("store down")
    with mock.patch.object(app_workflow, "_safe_log") as safe_log:
        await app_workflow._merge_fetch_failures(app)

    assert safe_log.call_args.args[0] == "error"
    assert collector.numeric_metric(FETCH_FAILURES_UNPERSISTED_METRIC) == 7


async def test_task_merges_the_fetch_failures_layout_and_records_truncation(
    collector,
):
    with (
        mock.patch(
            "application_sdk.execution.build_output_path", return_value="artifacts/run"
        ),
        mock.patch(
            "application_sdk.execution._temporal.activity_utils.run_temp_dir",
            return_value="/tmp/x",
        ),
        mock.patch(
            "application_sdk.outputs.skipped.merge_run_log",
            new_callable=mock.AsyncMock,
            return_value=MergeResult(key="artifacts/run/fetch-failures.ndjson", kept=5),
        ) as merge,
    ):
        out = await base.App.merge_fetch_failures(
            mock.MagicMock(),
            MergeFetchFailuresInput(failure_count=10, unpersisted_count=2),
        )

    merge.assert_awaited_once_with("artifacts/run", "/tmp/x", FETCH_FAILURES_LAYOUT)
    assert (out.kept, out.truncated) == (5, 3)
    assert collector.numeric_metric(FETCH_FAILURES_TRUNCATED_METRIC) == 3
