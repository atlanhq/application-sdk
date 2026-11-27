"""Unit tests for the output interceptor.

Tests the OutputInterceptor and its components for collecting and merging
workflow outputs from activities.
"""

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping
from unittest import mock

import orjson
import pytest
from pydantic import BaseModel, Field

import application_sdk.outputs as outputs_module
from application_sdk.app.registry import _running_app_name
from application_sdk.contracts.base import Output, OutputStatus
from application_sdk.execution._temporal.interceptors.outputs import (
    OutputActivityInboundInterceptor,
    OutputInterceptor,
    OutputWorkflowInboundInterceptor,
)
from application_sdk.outputs import (
    Artifact,
    BlankIdentity,
    FetchFailureReason,
    Metric,
    OutputCollector,
    RecordSkipped,
    SkipReason,
    current_outputs,
    get_outputs,
    record_fetch_failure,
    skip_record,
)
from application_sdk.outputs.fetch_failures import (
    FETCH_FAILURES_LAYOUT,
    FETCH_FAILURES_METRIC,
)
from application_sdk.outputs.skipped import (
    SKIPPED_RECORDS_METRIC,
    SkippableRecord,
    SkippedRecordSink,
    merge_run_log,
    merge_run_skips,
)
from tests.unit._doubles import ActivityInfo, ExecuteInput

_RUN_PREFIX = "artifacts/apps/app/workflows/wf-1/run-1"


class _TaskOutput(Output):
    """A task-style typed result."""

    rows: int = 0


class _AliasedOutput(Output):
    """A workflow result with aliased fields and no ``populate_by_name``.

    A required aliased field makes a ``model_dump()`` / ``model_validate()``
    round trip raise; a defaulted one makes it silently reset to the default.
    """

    rows: int = 0
    schema_name: str = Field(alias="schemaName")
    table_name: str = Field(default="", alias="tableName")


class _PlainModel(BaseModel):
    """A model with no ``metrics`` / ``artifacts`` fields."""

    name: str = ""


class _IntMetricsModel(BaseModel):
    """A model whose ``metrics`` field cannot hold the collector's map."""

    metrics: int = 0


@dataclass
class MockActivityInfo(ActivityInfo):
    workflow_run_id: str = "test-workflow-run-id"
    activity_type: str = "test_activity"
    activity_id: str = "activity-123"


class TestOutputActivityInboundInterceptor:
    """Tests for OutputActivityInboundInterceptor."""

    @pytest.fixture
    def mock_next_inbound(self):
        """Create a mock next inbound interceptor."""
        mock_next = mock.AsyncMock()
        mock_next.execute_activity = mock.AsyncMock(return_value={"status": "success"})
        return mock_next

    @pytest.fixture
    def interceptor(self, mock_next_inbound):
        """Create the interceptor instance."""
        return OutputActivityInboundInterceptor(mock_next_inbound)

    @pytest.fixture(autouse=True)
    def cleanup_state(self):
        """Clean up ContextVar state before and after each test."""
        current_outputs.set(None)
        yield
        current_outputs.set(None)

    async def test_creates_fresh_collector_for_activity(
        self, interceptor, mock_next_inbound
    ):
        """Test that a fresh collector is created for each activity.

        The collector is set on current_outputs during execution and reset
        to None afterwards (try/finally cleanup). Verify it exists during
        the activity via a side effect on the mock.
        """
        collector_during_activity: list = []

        async def capture_collector(input):
            collector_during_activity.append(current_outputs.get())
            return {"status": "success"}

        mock_next_inbound.execute_activity.side_effect = capture_collector
        input_data = ExecuteInput()

        with mock.patch(
            "application_sdk.execution._temporal.interceptors.outputs.activity"
        ) as mock_activity:
            mock_activity.info.return_value = MockActivityInfo()
            await interceptor.execute_activity(input_data)

        # Collector was set during execution
        assert len(collector_during_activity) == 1
        assert collector_during_activity[0] is not None
        assert isinstance(collector_during_activity[0], OutputCollector)
        # Collector is reset to None after execution (try/finally cleanup)
        assert current_outputs.get() is None

    async def test_returns_original_activity_result(
        self, interceptor, mock_next_inbound
    ):
        """Test that the original activity result is returned unchanged."""
        expected_result = {"status": "success", "data": [1, 2, 3]}
        mock_next_inbound.execute_activity.return_value = expected_result
        input_data = ExecuteInput()

        with mock.patch(
            "application_sdk.execution._temporal.interceptors.outputs.activity"
        ) as mock_activity:
            mock_activity.info.return_value = MockActivityInfo()
            result = await interceptor.execute_activity(input_data)

        assert result == expected_result

    async def test_dict_result_carries_outputs(self, interceptor, mock_next_inbound):
        """Collected outputs are added to a dict result under their namespaces."""

        async def activity_with_outputs(input):
            outputs = get_outputs()
            outputs.add_metric(Metric(name="count", value=42))
            outputs.add_artifact(Artifact(name="dump", path="s3://b/dump"))
            return {"status": "ok"}

        mock_next_inbound.execute_activity.side_effect = activity_with_outputs

        result = await interceptor.execute_activity(ExecuteInput())

        assert result == {
            "status": "ok",
            "_atlan_outputs": {
                "metrics": {"count": 42},
                "artifacts": {"dump": "s3://b/dump"},
            },
        }

    async def test_output_model_carries_outputs(self, interceptor, mock_next_inbound):
        """An Output result carries outputs on its metrics/artifacts fields."""
        returned = _TaskOutput(rows=3)

        async def activity_with_outputs(input):
            get_outputs().add_metric(Metric(name="rows", value=3, display_name="Rows"))
            return returned

        mock_next_inbound.execute_activity.side_effect = activity_with_outputs

        result = await interceptor.execute_activity(ExecuteInput())

        assert isinstance(result, _TaskOutput)
        assert result.rows == 3
        assert result.metrics == {"rows": {"value": 3, "display_name": "Rows"}}
        assert result.artifacts is None
        # The activity's own object is not mutated.
        assert returned.metrics is None

    async def test_output_passthrough_does_not_carry_upstream_metrics(
        self, interceptor, mock_next_inbound
    ):
        """A task returning an upstream output unchanged must not re-carry its metrics.

        Otherwise the workflow would count the upstream task's metrics twice.
        """
        upstream = _TaskOutput(rows=1, metrics={"rows": 1}, artifacts={"a": "p"})
        mock_next_inbound.execute_activity.return_value = upstream

        result = await interceptor.execute_activity(ExecuteInput())

        assert result.metrics is None
        assert result.artifacts is None
        assert result.rows == 1

    async def test_result_that_cannot_carry_outputs_is_unchanged(
        self, interceptor, mock_next_inbound
    ):
        """A None result is returned as-is; its outputs are dropped with a warning."""

        async def activity_with_outputs(input):
            get_outputs().add_metric(Metric(name="count", value=1))
            return None

        mock_next_inbound.execute_activity.side_effect = activity_with_outputs

        with (
            mock.patch(
                "application_sdk.execution._temporal.interceptors.outputs.activity"
            ) as mock_activity,
            mock.patch(
                "application_sdk.execution._temporal.interceptors.outputs.logger"
            ) as mock_logger,
        ):
            mock_activity.info.return_value = MockActivityInfo()
            result = await interceptor.execute_activity(ExecuteInput())

        assert result is None
        mock_logger.warning.assert_called_once()

    async def test_dict_without_outputs_is_unchanged(
        self, interceptor, mock_next_inbound
    ):
        """No outputs collected: a dict result keeps its own keys untouched."""
        own = {"metrics": "not ours"}
        mock_next_inbound.execute_activity.return_value = own

        result = await interceptor.execute_activity(ExecuteInput())

        assert result is own

    def test_no_process_level_store(self):
        """Outputs must not be held in process memory keyed by run id.

        The activity and its workflow can run in different worker processes,
        so a process-local store silently loses them.
        """
        assert not hasattr(outputs_module, "_collected_outputs")


class TestOutputWorkflowInboundInterceptor:
    """Tests for OutputWorkflowInboundInterceptor and its outbound harvest."""

    @pytest.fixture
    def mock_next_inbound(self):
        """Create a mock next inbound interceptor."""
        mock_next = mock.AsyncMock()
        mock_next.execute_workflow = mock.AsyncMock(
            return_value={"transformed_data_prefix": "s3://bucket/path"}
        )
        mock_next.init = mock.MagicMock()
        return mock_next

    @pytest.fixture
    def interceptor(self, mock_next_inbound):
        """Create the interceptor instance."""
        return OutputWorkflowInboundInterceptor(mock_next_inbound)

    @pytest.fixture
    def mock_next_outbound(self):
        """The outbound chain below the output interceptor."""
        return mock.MagicMock()

    @pytest.fixture
    def outbound(self, interceptor, mock_next_inbound, mock_next_outbound):
        """The outbound interceptor the workflow's activity calls go through."""
        interceptor.init(mock_next_outbound)
        return mock_next_inbound.init.call_args.args[0]

    @pytest.fixture(autouse=True)
    def cleanup_state(self):
        """Clean up ContextVar state before and after each test."""
        current_outputs.set(None)
        yield
        current_outputs.set(None)

    @staticmethod
    def _completed(result: Any) -> "asyncio.Future[Any]":
        """A handle that resolves on a later loop tick, as Temporal's does."""
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        loop.call_soon(fut.set_result, result)
        return fut

    async def test_returns_original_result_when_no_outputs(
        self, interceptor, mock_next_inbound
    ):
        """Test that original result is returned when no outputs collected."""
        expected_result = {"key": "value"}
        mock_next_inbound.execute_workflow.return_value = expected_result

        result = await interceptor.execute_workflow(ExecuteInput())

        assert result == expected_result

    async def test_merges_outputs_carried_on_activity_results(
        self, interceptor, outbound, mock_next_inbound, mock_next_outbound
    ):
        """Outputs carried on completed activity results reach the workflow result."""
        handles = [
            self._completed(_TaskOutput(rows=1, metrics={"count": 50})),
            self._completed(
                {
                    "x": 1,
                    "_atlan_outputs": {
                        "metrics": {"count": 30, "other": 10},
                        "artifacts": {"a": "p"},
                    },
                }
            ),
        ]
        mock_next_outbound.start_activity.side_effect = handles

        async def run(input):
            for _ in handles:
                await outbound.start_activity(mock.MagicMock())
            return {"base": "data"}

        mock_next_inbound.execute_workflow.side_effect = run

        result = await interceptor.execute_workflow(ExecuteInput())

        assert result == {
            "base": "data",
            "metrics": {"count": 80, "other": 10},
            "artifacts": {"a": "p"},
        }

    async def test_handle_already_done_when_returned_is_still_merged(
        self, interceptor, outbound, mock_next_inbound, mock_next_outbound
    ):
        """A done handle's callback is only queued; the exit flush still merges it."""
        fut = asyncio.get_running_loop().create_future()
        fut.set_result(_TaskOutput(metrics={"early": 1}))
        mock_next_outbound.start_activity.return_value = fut

        async def run(input):
            await outbound.start_activity(mock.MagicMock())
            return {}

        mock_next_inbound.execute_workflow.side_effect = run

        result = await interceptor.execute_workflow(ExecuteInput())

        assert result == {"metrics": {"early": 1}}

    async def test_returns_the_same_handle(self, outbound, mock_next_outbound):
        """start_activity keeps returning Temporal's cancellable handle."""
        handle = self._completed(None)
        mock_next_outbound.start_activity.return_value = handle

        assert outbound.start_activity(mock.MagicMock()) is handle

    async def test_harvests_local_activities(
        self, interceptor, outbound, mock_next_inbound, mock_next_outbound
    ):
        """Local activity results carry outputs the same way."""
        mock_next_outbound.start_local_activity.return_value = self._completed(
            _TaskOutput(metrics={"local": 2})
        )

        async def run(input):
            await outbound.start_local_activity(mock.MagicMock())
            return {}

        mock_next_inbound.execute_workflow.side_effect = run

        result = await interceptor.execute_workflow(ExecuteInput())

        assert result == {"metrics": {"local": 2}}

    async def test_harvests_through_a_coroutine_returning_outbound(
        self, interceptor, outbound, mock_next_inbound, mock_next_outbound
    ):
        """An outbound below that returns a coroutine (the lock interceptor) still works."""

        async def locked_start(input):
            return _TaskOutput(metrics={"locked": 1})

        mock_next_outbound.start_activity.side_effect = locked_start

        async def run(input):
            result = await outbound.start_activity(mock.MagicMock())
            assert result.metrics == {"locked": 1}
            return {}

        mock_next_inbound.execute_workflow.side_effect = run

        result = await interceptor.execute_workflow(ExecuteInput())

        assert result == {"metrics": {"locked": 1}}

    async def test_failed_activity_contributes_nothing(
        self, interceptor, outbound, mock_next_inbound, mock_next_outbound
    ):
        """A failed activity's handle is ignored and its error still propagates."""
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        loop.call_soon(fut.set_exception, RuntimeError("boom"))
        mock_next_outbound.start_activity.return_value = fut

        async def run(input):
            with pytest.raises(RuntimeError):
                await outbound.start_activity(mock.MagicMock())
            return {"status": "recovered"}

        mock_next_inbound.execute_workflow.side_effect = run

        result = await interceptor.execute_workflow(ExecuteInput())

        assert result == {"status": "recovered"}

    async def test_dict_own_metrics_key_is_not_harvested(
        self, interceptor, outbound, mock_next_inbound, mock_next_outbound
    ):
        """A dict activity's own ``metrics`` / ``artifacts`` data is not collected output.

        Only the reserved carrier key is read, so an activity that returns
        ``{"metrics": {...}}`` without calling get_outputs() reports nothing.
        """
        mock_next_outbound.start_activity.return_value = self._completed(
            {"metrics": {"rows": 500}, "artifacts": {"a": "p"}}
        )

        async def run(input):
            await outbound.start_activity(mock.MagicMock())
            return {"status": "done"}

        mock_next_inbound.execute_workflow.side_effect = run

        result = await interceptor.execute_workflow(ExecuteInput())

        assert result == {"status": "done"}

    async def test_activity_result_crosses_processes(
        self, interceptor, outbound, mock_next_inbound, mock_next_outbound
    ):
        """End to end at unit level: one activity interceptor, one workflow interceptor.

        They share nothing but the activity result, as when the activity runs
        in another worker process.
        """

        async def activity_body(input):
            get_outputs().add_metric(Metric(name="tables", value=100))
            return _TaskOutput(rows=100)

        activity_next = mock.AsyncMock()
        activity_next.execute_activity.side_effect = activity_body
        carried = await OutputActivityInboundInterceptor(
            activity_next
        ).execute_activity(ExecuteInput())

        mock_next_outbound.start_activity.return_value = self._completed(carried)

        async def run(input):
            await outbound.start_activity(mock.MagicMock())
            return {"connection_qualified_name": "default/redshift/prod"}

        mock_next_inbound.execute_workflow.side_effect = run

        result = await interceptor.execute_workflow(ExecuteInput())

        assert result["connection_qualified_name"] == "default/redshift/prod"
        assert result["metrics"] == {"tables": 100}

    async def test_carrier_key_is_stripped_from_dict_workflow_result(
        self, interceptor, outbound, mock_next_inbound, mock_next_outbound
    ):
        """A workflow returning an activity's dict result does not leak the carrier."""
        mock_next_outbound.start_activity.return_value = self._completed(
            {"status": "ok", "_atlan_outputs": {"metrics": {"rows": 2}}}
        )

        async def run(input):
            return await outbound.start_activity(mock.MagicMock())

        mock_next_inbound.execute_workflow.side_effect = run

        result = await interceptor.execute_workflow(ExecuteInput())

        assert result == {"status": "ok", "metrics": {"rows": 2}}

    async def test_handles_non_dict_result_with_outputs(
        self, interceptor, mock_next_inbound
    ):
        """Test handling when workflow returns non-dict but outputs exist."""

        async def run(input):
            get_outputs().add_metric(Metric(name="m", value=1))
            return "not a dict"

        mock_next_inbound.execute_workflow.side_effect = run

        result = await interceptor.execute_workflow(ExecuteInput())

        assert result == {"metrics": {"m": 1}}

    async def test_workflow_level_outputs_are_included(
        self, interceptor, mock_next_inbound
    ):
        """Test that workflow-level outputs are included."""

        async def workflow_with_outputs(input):
            outputs = get_outputs()
            outputs.add_metric(Metric(name="workflow-metric", value=999))
            return {"status": "done"}

        mock_next_inbound.execute_workflow.side_effect = workflow_with_outputs

        result = await interceptor.execute_workflow(ExecuteInput())

        assert result["metrics"]["workflow-metric"] == 999
        assert result["status"] == "done"

    async def test_aliased_output_keeps_its_fields_when_outputs_merge(
        self, interceptor, mock_next_inbound
    ):
        """An Output with an aliased field keeps every field and gains the outputs.

        ``model_dump()`` emits field names, which a model without
        ``populate_by_name`` rejects on ``model_validate``; the merge must not
        depend on that round trip.
        """

        async def run(input):
            get_outputs().add_metric(Metric(name="count", value=7))
            return _AliasedOutput(rows=3, schemaName="sales", tableName="orders")

        mock_next_inbound.execute_workflow.side_effect = run

        result = await interceptor.execute_workflow(ExecuteInput())

        assert isinstance(result, _AliasedOutput)
        assert result.schema_name == "sales"
        assert result.table_name == "orders"
        assert result.rows == 3
        assert result.status == OutputStatus.SUCCESS
        assert result.metrics == {"count": 7}

    async def test_aliased_output_marked_partial_keeps_its_fields(
        self, interceptor, mock_next_inbound
    ):
        """A run that skipped records is PARTIAL_SUCCESS without losing fields."""

        async def run(input):
            get_outputs().add_metric(Metric(name=SKIPPED_RECORDS_METRIC, value=2))
            return _AliasedOutput(rows=3, schemaName="sales", tableName="orders")

        mock_next_inbound.execute_workflow.side_effect = run

        result = await interceptor.execute_workflow(ExecuteInput())

        assert isinstance(result, _AliasedOutput)
        assert result.schema_name == "sales"
        assert result.table_name == "orders"
        assert result.rows == 3
        assert result.status == OutputStatus.PARTIAL_SUCCESS
        assert result.metrics == {SKIPPED_RECORDS_METRIC: 2}

    async def test_model_without_output_fields_is_returned_unchanged(
        self, interceptor, mock_next_inbound
    ):
        """A model that cannot carry outputs is returned as-is, never replaced."""
        original = _PlainModel(name="x")

        async def run(input):
            get_outputs().add_metric(Metric(name="count", value=7))
            return original

        mock_next_inbound.execute_workflow.side_effect = run

        result = await interceptor.execute_workflow(ExecuteInput())

        assert result is original

    async def test_model_whose_output_field_rejects_the_map_is_returned_unchanged(
        self, interceptor, mock_next_inbound
    ):
        """A ``metrics`` field of another type is never set to the collector map."""
        original = _IntMetricsModel(metrics=1)

        async def run(input):
            get_outputs().add_metric(Metric(name="count", value=7))
            return original

        mock_next_inbound.execute_workflow.side_effect = run

        result = await interceptor.execute_workflow(ExecuteInput())

        assert result is original
        assert result.metrics == 1


class TestOutputInterceptor:
    """Tests for the main OutputInterceptor class."""

    @pytest.fixture
    def interceptor(self):
        """Create the main interceptor instance."""
        return OutputInterceptor()

    def test_returns_workflow_interceptor_class(self, interceptor):
        """Test that workflow_interceptor_class returns the correct class."""
        mock_input = mock.MagicMock()
        result = interceptor.workflow_interceptor_class(mock_input)
        assert result == OutputWorkflowInboundInterceptor

    def test_intercept_activity_wraps_next(self, interceptor):
        """Test that intercept_activity wraps the next interceptor."""
        mock_next = mock.MagicMock()
        result = interceptor.intercept_activity(mock_next)
        assert isinstance(result, OutputActivityInboundInterceptor)


class TestGetOutputsFunction:
    """Tests for the get_outputs() function."""

    @pytest.fixture(autouse=True)
    def cleanup_state(self):
        """Clean up ContextVar state before and after each test."""
        current_outputs.set(None)
        yield
        current_outputs.set(None)

    def test_creates_collector_when_none_exists(self):
        """Test that get_outputs creates a new collector when none exists."""
        collector = get_outputs()
        assert collector is not None
        assert isinstance(collector, OutputCollector)

    def test_returns_same_collector_on_repeated_calls(self):
        """Test that get_outputs returns the same collector."""
        collector1 = get_outputs()
        collector2 = get_outputs()
        assert collector1 is collector2

    def test_returns_existing_collector(self):
        """Test that get_outputs returns an existing collector."""
        existing = OutputCollector()
        existing.add_metric(Metric(name="test", value=123))
        current_outputs.set(existing)

        collector = get_outputs()
        assert collector is existing
        assert collector.to_dict()["metrics"]["test"] == 123


def _map_column(column: Mapping[str, object]) -> str:
    """A sync, pure mapper in the canonical-app shape, guarding one identity input.

    The value is handed to a stand-in "creator" that rejects names it does not
    like with ``ValueError`` — the guard must not turn that into a skip.
    """
    if not column.get("name"):
        raise BlankIdentity(
            column,
            record_id=str(column["id"]),
            entity_type="Column",
            detail="column name is blank",
        )
    name = str(column["name"])
    if " " in name:
        raise ValueError(f"creator rejects {name!r}")
    return name


async def _map_table_columns(table: Mapping[str, Any]) -> list[str]:
    """A @task-style per-record loop over a nested record the loop never holds."""
    mapped: list[str] = []
    for column in table["columns"]:
        try:
            mapped.append(_map_column(column))
        except RecordSkipped as skip:
            await skip_record(
                skip.record,
                skip.reason,
                record_id=skip.record_id,
                entity_type=skip.entity_type,
                detail=skip.detail,
            )
            continue
    return mapped


class TestSkippedRecordsThroughInterceptors:
    """FND-4881: skips reach the workflow output as metric, artifact and status."""

    @pytest.fixture(autouse=True)
    def cleanup_state(self):
        current_outputs.set(None)
        yield
        current_outputs.set(None)

    @pytest.fixture
    def uploads(self, tmp_path):
        """An object store double: parts uploaded by activities, read by the merge."""
        store: dict[str, bytes] = {}

        async def upload_file(key, local_path):
            store[key] = Path(local_path).read_bytes()

        async def list_data_keys(prefix):
            return sorted(k for k in store if k.startswith(prefix))

        async def download_file(key, local_path):
            Path(local_path).write_bytes(store[key])

        sink_dir = tmp_path / "scratch"
        with (
            mock.patch.multiple(
                "application_sdk.storage",
                upload_file=upload_file,
                list_data_keys=list_data_keys,
                download_file=download_file,
            ),
            mock.patch(
                "application_sdk.execution._temporal.activity_utils.build_output_path",
                return_value=_RUN_PREFIX,
            ),
            mock.patch(
                "application_sdk.outputs.skipped._new_sink",
                side_effect=lambda: SkippedRecordSink(
                    sink_dir / f"{len(store)}-{id(object())}.ndjson"
                ),
            ),
        ):
            yield store

    @staticmethod
    async def _run_activity(body) -> Any:
        nxt = mock.AsyncMock()
        nxt.execute_activity.side_effect = body
        return await OutputActivityInboundInterceptor(nxt).execute_activity(
            ExecuteInput()
        )

    @staticmethod
    async def _run_workflow(carried: list[Any], result: Output) -> Any:
        """Drive the workflow interceptor with the given activity results."""
        nxt = mock.AsyncMock()
        nxt.init = mock.MagicMock()
        interceptor = OutputWorkflowInboundInterceptor(nxt)
        outbound_next = mock.MagicMock()
        interceptor.init(outbound_next)
        outbound = nxt.init.call_args.args[0]
        loop = asyncio.get_running_loop()
        futures = []
        for value in carried:
            fut = loop.create_future()
            fut.set_result(value)
            futures.append(fut)
        outbound_next.start_activity.side_effect = futures

        async def run(input):
            for _ in carried:
                await outbound.start_activity(mock.MagicMock())
            return result

        nxt.execute_workflow.side_effect = run
        return await interceptor.execute_workflow(ExecuteInput())

    async def test_skips_reach_workflow_output(self, uploads, tmp_path):
        async def extract(input):
            for i in range(2):
                await skip_record(
                    {"name": f"t{i}", "schema": ""},
                    SkipReason.BLANK_IDENTITY,
                    record_id=f"t{i}",
                    entity_type="Table",
                )
            return _TaskOutput(rows=10)

        extracted = await self._run_activity(extract)
        assert len(uploads) == 1
        (part_key,) = uploads
        assert part_key.startswith(f"{_RUN_PREFIX}/skipped-records/parts/")

        # The run-end framework task, driven through the same activity interceptor.
        async def merge(input):
            await merge_run_skips(_RUN_PREFIX, str(tmp_path))
            return Output()

        merged = await self._run_activity(merge)

        result = await self._run_workflow([extracted, merged], Output())

        assert result.status == OutputStatus.PARTIAL_SUCCESS
        assert result.metrics[SKIPPED_RECORDS_METRIC] == 2
        assert result.metrics["skipped-records-blank-identity"] == 2
        merged_key = f"{_RUN_PREFIX}/skipped-records.ndjson"
        assert result.artifacts == {
            "skipped-records": {"path": merged_key, "display_name": "Skipped records"}
        }
        assert len(uploads[merged_key].splitlines()) == 2

    async def test_no_skips_means_no_artifact_metric_or_status_change(self, uploads):
        async def extract(input):
            get_outputs().add_metric(Metric(name="tables", value=3))
            return _TaskOutput(rows=3)

        extracted = await self._run_activity(extract)
        result = await self._run_workflow([extracted], Output())

        assert uploads == {}
        assert result.status == OutputStatus.SUCCESS
        assert result.artifacts is None
        assert result.metrics == {"tables": 3}

    async def test_failure_status_is_never_downgraded(self, uploads):
        async def extract(input):
            await skip_record(
                {}, SkipReason.MALFORMED_RECORD, record_id="r", entity_type="Table"
            )
            return _TaskOutput()

        extracted = await self._run_activity(extract)
        result = await self._run_workflow(
            [extracted], Output(status=OutputStatus.FAILURE)
        )

        assert result.status == OutputStatus.FAILURE
        assert result.metrics[SKIPPED_RECORDS_METRIC] == 1

    async def test_failed_activity_uploads_nothing(self, uploads):
        async def extract(input):
            await skip_record(
                {}, SkipReason.MALFORMED_RECORD, record_id="r", entity_type="Table"
            )
            raise RuntimeError("boom")

        with pytest.raises(RuntimeError):
            await self._run_activity(extract)
        assert uploads == {}

    # FND-4963: a sync mapper hands its skip to the loop as RecordSkipped.

    async def test_sync_mapper_skip_reaches_workflow_output(self, uploads, tmp_path):
        blank: SkippableRecord = {"id": "c2", "name": "", "type": "int"}
        table = {"id": "t1", "columns": [{"id": "c1", "name": "a"}, blank]}
        mapped: list[str] = []

        async def transform(input):
            mapped.extend(await _map_table_columns(table))
            return _TaskOutput(rows=len(mapped))

        transformed = await self._run_activity(transform)

        async def merge(input):
            await merge_run_skips(_RUN_PREFIX, str(tmp_path))
            return Output()

        merged = await self._run_activity(merge)
        result = await self._run_workflow([transformed, merged], Output())

        assert mapped == ["a"]
        assert result.status == OutputStatus.PARTIAL_SUCCESS
        assert result.metrics[SKIPPED_RECORDS_METRIC] == 1
        assert result.metrics["skipped-records-blank-identity"] == 1
        merged_key = f"{_RUN_PREFIX}/skipped-records.ndjson"
        assert result.artifacts == {
            "skipped-records": {"path": merged_key, "display_name": "Skipped records"}
        }
        # The file keeps the nested record the mapper skipped, which the loop
        # (holding only the table) could not have supplied itself.
        (line,) = uploads[merged_key].splitlines()
        assert orjson.loads(line) == {
            "record_id": "c2",
            "entity_type": "Column",
            "reason": "blank_identity",
            "detail": "column name is blank",
            "record": blank,
        }

    async def test_creator_value_error_is_not_caught(self, uploads):
        table = {"id": "t1", "columns": [{"id": "c1", "name": "bad name"}]}

        async def transform(input):
            await _map_table_columns(table)
            return _TaskOutput()

        with pytest.raises(ValueError, match="creator rejects"):
            await self._run_activity(transform)
        assert uploads == {}


class TestFetchFailuresThroughInterceptors:
    """FND-5294: fetch failures reach the workflow output as metric, artifact and status."""

    _run_activity = staticmethod(TestSkippedRecordsThroughInterceptors._run_activity)
    _run_workflow = staticmethod(TestSkippedRecordsThroughInterceptors._run_workflow)

    @pytest.fixture(autouse=True)
    def cleanup_state(self):
        current_outputs.set(None)
        yield
        current_outputs.set(None)

    @pytest.fixture
    def uploads(self, tmp_path):
        store: dict[str, bytes] = {}

        async def upload_file(key, local_path):
            store[key] = Path(local_path).read_bytes()

        async def list_data_keys(prefix):
            return sorted(k for k in store if k.startswith(prefix))

        async def download_file(key, local_path):
            Path(local_path).write_bytes(store[key])

        parts = iter(range(1000))
        with (
            mock.patch.multiple(
                "application_sdk.storage",
                upload_file=upload_file,
                list_data_keys=list_data_keys,
                download_file=download_file,
            ),
            mock.patch(
                "application_sdk.execution._temporal.activity_utils.build_output_path",
                return_value=_RUN_PREFIX,
            ),
            mock.patch(
                "application_sdk.outputs.fetch_failures._part_location",
                side_effect=lambda: (
                    tmp_path / "scratch" / f"{(n := next(parts))}.ndjson",
                    f"act-{n}.ndjson",
                ),
            ),
        ):
            yield store

    async def test_failures_from_gather_reach_workflow_output(self, uploads, tmp_path):
        async def fetch(report_id: str) -> str | None:
            if report_id.endswith("x"):
                record_fetch_failure(
                    FetchFailureReason.NOT_FOUND,
                    entity_type="Report",
                    record_id=report_id,
                    http_status=404,
                )
                return None
            return report_id

        async def extract(input):
            got = await asyncio.gather(*(fetch(r) for r in ["a", "bx", "c", "dx"]))
            return _TaskOutput(rows=len([g for g in got if g]))

        extracted = await self._run_activity(extract)
        (part_key,) = uploads
        assert part_key.startswith(f"{_RUN_PREFIX}/fetch-failures/parts/")

        async def merge(input):
            await merge_run_log(_RUN_PREFIX, str(tmp_path), FETCH_FAILURES_LAYOUT)
            return Output()

        merged = await self._run_activity(merge)
        result = await self._run_workflow([extracted, merged], Output())

        assert result.status == OutputStatus.PARTIAL_SUCCESS
        assert result.metrics[FETCH_FAILURES_METRIC] == 2
        assert result.metrics["fetch-failures-not-found"] == 2
        merged_key = f"{_RUN_PREFIX}/fetch-failures.ndjson"
        assert result.artifacts == {
            "fetch-failures": {"path": merged_key, "display_name": "Fetch failures"}
        }
        ids = sorted(
            orjson.loads(x)["record_id"] for x in uploads[merged_key].splitlines()
        )
        assert ids == ["bx", "dx"]

    async def test_no_failures_means_no_artifact_metric_or_status_change(self, uploads):
        async def extract(input):
            get_outputs().add_metric(Metric(name="reports", value=3))
            return _TaskOutput(rows=3)

        extracted = await self._run_activity(extract)
        result = await self._run_workflow([extracted], Output())

        assert uploads == {}
        assert result.status == OutputStatus.SUCCESS
        assert result.artifacts is None
        assert result.metrics == {"reports": 3}

    async def test_failed_activity_records_nothing(self, uploads):
        async def extract(input):
            record_fetch_failure(
                FetchFailureReason.SOURCE_UNAVAILABLE,
                entity_type="Report",
                record_id="r",
            )
            raise RuntimeError("boom")

        with pytest.raises(RuntimeError):
            await self._run_activity(extract)
        assert uploads == {}

    async def test_failure_status_is_never_downgraded(self, uploads):
        async def extract(input):
            record_fetch_failure(
                FetchFailureReason.FORBIDDEN, entity_type="Report", record_id="r"
            )
            return _TaskOutput()

        extracted = await self._run_activity(extract)
        result = await self._run_workflow(
            [extracted], Output(status=OutputStatus.FAILURE)
        )

        assert result.status == OutputStatus.FAILURE
        assert result.metrics[FETCH_FAILURES_METRIC] == 1


class TestSkippedPartsUseTheRunningApp:
    """FND-4921: a skipped-records part uploads under the App that ran the task.

    The task wrapper records the App's name in ``_running_app_name`` inside the
    activity body; the part is persisted after the body returns. With no single
    registered App and ``ATLAN_APPLICATION_NAME`` naming neither, the part must
    still land under the App's run prefix, which is where
    ``merge_skipped_records`` lists. Concurrent activities keep their own names.
    """

    @pytest.fixture(autouse=True)
    def cleanup_state(self):
        current_outputs.set(None)
        yield
        current_outputs.set(None)

    @pytest.fixture
    def uploaded(self, tmp_path):
        keys: list[str] = []

        async def upload_file(key, _local_path):
            keys.append(key)

        with (
            mock.patch("application_sdk.storage.upload_file", upload_file),
            mock.patch(
                "application_sdk.app.registry.AppRegistry.list_apps", return_value=[]
            ),
            mock.patch("application_sdk.constants.APPLICATION_NAME", "env-app"),
            mock.patch(
                "application_sdk.execution._temporal.activity_utils.get_workflow_id",
                return_value="wf-1",
            ),
            mock.patch(
                "application_sdk.execution._temporal.activity_utils.get_workflow_run_id",
                return_value="run-1",
            ),
            mock.patch(
                "application_sdk.outputs.skipped._new_sink",
                side_effect=lambda: SkippedRecordSink(
                    tmp_path / f"{id(object())}.ndjson"
                ),
            ),
        ):
            yield keys

    @staticmethod
    async def _run_activity(app_name: str) -> None:
        async def body(_input):
            _running_app_name.set(app_name)  # what the task wrapper does
            await asyncio.sleep(0)  # let a concurrent activity interleave
            await skip_record(
                {"name": "t", "schema": ""},
                SkipReason.BLANK_IDENTITY,
                record_id="t",
                entity_type="Table",
            )
            return {"status": "success"}

        nxt = mock.AsyncMock()
        nxt.execute_activity = mock.AsyncMock(side_effect=body)
        await OutputActivityInboundInterceptor(nxt).execute_activity(ExecuteInput())

    async def test_part_uploads_under_the_app_not_the_env_name(self, uploaded):
        await self._run_activity("my-app")

        assert len(uploaded) == 1
        assert uploaded[0].startswith("artifacts/apps/my-app/workflows/wf-1/run-1/")
        # Scoped to the activity: nothing leaks to the caller once it returns.
        assert _running_app_name.get() == ""

    async def test_concurrent_activities_keep_their_own_app(self, uploaded):
        await asyncio.gather(*(self._run_activity(n) for n in ("app-a", "app-b")))

        assert sorted(k.split("/")[2] for k in uploaded) == ["app-a", "app-b"]
