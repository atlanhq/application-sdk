"""Tests for WorkflowAppClient.call() and call_by_name()."""

from __future__ import annotations

# BLDX-878: inter-app calls deactivated pending review.
# Re-enable by removing these three lines once the feature is restored.
import pytest

pytest.skip(
    "Inter-app calls deactivated pending BLDX-878; re-enable when resolved.",
    allow_module_level=True,
)

from datetime import datetime, timedelta  # noqa: E402
from unittest.mock import AsyncMock, patch  # noqa: E402

from application_sdk.app.client import WorkflowAppClient  # noqa: E402
from application_sdk.contracts.base import Input, Output  # noqa: E402

# ---------------------------------------------------------------------------
# Fixtures & Helpers
# ---------------------------------------------------------------------------


class _StubInput(Input):
    """Minimal Input subclass for testing."""

    source: str = "test"


class _StubOutput(Output):
    """Minimal Output subclass for testing."""

    result: str = "ok"


def _make_app_cls(app_name: str = "child-app", output_type: type = _StubOutput) -> type:
    """Build a fake App class with the attributes WorkflowAppClient reads."""
    cls = type("FakeApp", (), {"_app_name": app_name, "_output_type": output_type})
    return cls


def _make_client(
    run_id: str = "parent-run-1", correlation_id: str = "corr-abc"
) -> WorkflowAppClient:
    return WorkflowAppClient(
        parent_context_data={"run_id": run_id, "correlation_id": correlation_id}
    )


# ---------------------------------------------------------------------------
# WorkflowAppClient.call()
# ---------------------------------------------------------------------------


class TestCall:
    """Tests for WorkflowAppClient.call()."""

    async def test_call_executes_child_workflow(self):
        """Happy path: call() delegates to workflow.execute_child_workflow."""
        client = _make_client()
        app_cls = _make_app_cls()
        input_data = _StubInput(source="s3://bucket")
        expected_output = _StubOutput(result="done")

        now_time = datetime(2025, 1, 1, 12, 0, 0)

        with (
            patch(
                "application_sdk.app.client.workflow.execute_child_workflow",
                new_callable=AsyncMock,
                return_value=expected_output,
            ) as mock_exec,
            patch(
                "application_sdk.app.client.workflow.uuid4",
                return_value="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            ),
            patch(
                "application_sdk.app.client.workflow.now",
                return_value=now_time,
            ),
            patch("application_sdk.app.client._safe_log"),
        ):
            result = await client.call(app_cls, input_data)

        assert result is expected_output
        mock_exec.assert_awaited_once()
        call_kwargs = mock_exec.call_args
        assert call_kwargs.args[0] == "child-app"
        assert call_kwargs.kwargs["result_type"] is _StubOutput
        assert call_kwargs.kwargs["task_queue"] is None

    async def test_call_routes_to_custom_task_queue(self):
        """call() passes task_queue to execute_child_workflow when specified."""
        client = _make_client()
        app_cls = _make_app_cls()
        input_data = _StubInput()

        now_time = datetime(2025, 1, 1, 12, 0, 0)

        with (
            patch(
                "application_sdk.app.client.workflow.execute_child_workflow",
                new_callable=AsyncMock,
                return_value=_StubOutput(),
            ) as mock_exec,
            patch(
                "application_sdk.app.client.workflow.uuid4",
                return_value="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            ),
            patch(
                "application_sdk.app.client.workflow.now",
                return_value=now_time,
            ),
            patch("application_sdk.app.client._safe_log"),
        ):
            await client.call(app_cls, input_data, task_queue="custom-queue")

        assert mock_exec.call_args.kwargs["task_queue"] == "custom-queue"

    async def test_call_raises_on_child_failure(self):
        """call() propagates exceptions from child workflow execution."""
        client = _make_client()
        app_cls = _make_app_cls()
        input_data = _StubInput()

        now_time = datetime(2025, 1, 1, 12, 0, 0)

        with (
            patch(
                "application_sdk.app.client.workflow.execute_child_workflow",
                new_callable=AsyncMock,
                side_effect=RuntimeError("child exploded"),
            ),
            patch(
                "application_sdk.app.client.workflow.uuid4",
                return_value="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            ),
            patch(
                "application_sdk.app.client.workflow.now",
                return_value=now_time,
            ),
            patch("application_sdk.app.client._safe_log"),
        ):
            with pytest.raises(RuntimeError, match="child exploded"):
                await client.call(app_cls, input_data)

    async def test_call_logs_duration(self):
        """call() logs start and completion with duration_ms."""
        client = _make_client()
        app_cls = _make_app_cls()
        input_data = _StubInput()

        start = datetime(2025, 1, 1, 12, 0, 0)
        end = start + timedelta(seconds=2)
        times = iter([start, end])

        with (
            patch(
                "application_sdk.app.client.workflow.execute_child_workflow",
                new_callable=AsyncMock,
                return_value=_StubOutput(),
            ),
            patch(
                "application_sdk.app.client.workflow.uuid4",
                return_value="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            ),
            patch(
                "application_sdk.app.client.workflow.now",
                side_effect=lambda: next(times),
            ),
            patch("application_sdk.app.client._safe_log") as mock_log,
        ):
            await client.call(app_cls, input_data)

        # Two info-level logs: start + completion
        info_calls = [c for c in mock_log.call_args_list if c.args[0] == "info"]
        assert len(info_calls) == 2
        # Completion log should have duration_ms
        assert info_calls[1].kwargs.get("duration_ms") == 2000.0

    async def test_call_workflow_id_format(self):
        """call() builds workflow id as {app_name}-{config_hash}-{short_id}."""
        client = _make_client()
        app_cls = _make_app_cls(app_name="my-app")
        input_data = _StubInput()

        with (
            patch(
                "application_sdk.app.client.workflow.execute_child_workflow",
                new_callable=AsyncMock,
                return_value=_StubOutput(),
            ) as mock_exec,
            patch(
                "application_sdk.app.client.workflow.uuid4",
                return_value="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            ),
            patch(
                "application_sdk.app.client.workflow.now",
                return_value=datetime(2025, 1, 1),
            ),
            patch("application_sdk.app.client._safe_log"),
        ):
            await client.call(app_cls, input_data)

        wf_id = mock_exec.call_args.kwargs["id"]
        assert wf_id.startswith("my-app-")
        # short_id is 8 hex chars at the end
        short_id = wf_id.rsplit("-", 1)[-1]
        # The short_id comes from uuid with dashes stripped, first 8 chars
        assert len(short_id) >= 8

    async def test_call_missing_parent_context_keys(self):
        """call() handles missing run_id/correlation_id gracefully."""
        client = WorkflowAppClient(parent_context_data={})
        app_cls = _make_app_cls()
        input_data = _StubInput()

        with (
            patch(
                "application_sdk.app.client.workflow.execute_child_workflow",
                new_callable=AsyncMock,
                return_value=_StubOutput(),
            ),
            patch(
                "application_sdk.app.client.workflow.uuid4",
                return_value="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            ),
            patch(
                "application_sdk.app.client.workflow.now",
                return_value=datetime(2025, 1, 1),
            ),
            patch("application_sdk.app.client._safe_log"),
        ):
            # Should not raise
            result = await client.call(app_cls, input_data)
            assert isinstance(result, _StubOutput)


# ---------------------------------------------------------------------------
# WorkflowAppClient.call_by_name()
# ---------------------------------------------------------------------------


class TestCallByName:
    """Tests for WorkflowAppClient.call_by_name()."""

    async def test_call_by_name_returns_raw_result(self):
        """call_by_name() returns result as-is when no output_type given."""
        client = _make_client()
        input_data = _StubInput()
        raw = {"records": 42, "status": "ok"}

        with (
            patch(
                "application_sdk.app.client.workflow.execute_child_workflow",
                new_callable=AsyncMock,
                return_value=raw,
            ),
            patch(
                "application_sdk.app.client.workflow.uuid4",
                return_value="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            ),
            patch(
                "application_sdk.app.client.workflow.now",
                return_value=datetime(2025, 1, 1),
            ),
            patch("application_sdk.app.client._safe_log"),
        ):
            result = await client.call_by_name("remote-app", input_data)

        assert result == raw

    async def test_call_by_name_deserializes_dict_with_output_type(self):
        """call_by_name() deserializes dict result into output_type."""
        client = _make_client()
        input_data = _StubInput()
        raw = {"result": "deserialized"}

        with (
            patch(
                "application_sdk.app.client.workflow.execute_child_workflow",
                new_callable=AsyncMock,
                return_value=raw,
            ),
            patch(
                "application_sdk.app.client.workflow.uuid4",
                return_value="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            ),
            patch(
                "application_sdk.app.client.workflow.now",
                return_value=datetime(2025, 1, 1),
            ),
            patch("application_sdk.app.client._safe_log"),
        ):
            result = await client.call_by_name(
                "remote-app", input_data, output_type=_StubOutput
            )

        assert isinstance(result, _StubOutput)
        assert result.result == "deserialized"

    async def test_call_by_name_filters_unknown_fields(self):
        """call_by_name() filters unknown fields before model_validate."""
        client = _make_client()
        input_data = _StubInput()
        raw = {"result": "ok", "extra_field": "should_be_dropped"}

        with (
            patch(
                "application_sdk.app.client.workflow.execute_child_workflow",
                new_callable=AsyncMock,
                return_value=raw,
            ),
            patch(
                "application_sdk.app.client.workflow.uuid4",
                return_value="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            ),
            patch(
                "application_sdk.app.client.workflow.now",
                return_value=datetime(2025, 1, 1),
            ),
            patch("application_sdk.app.client._safe_log"),
        ):
            result = await client.call_by_name(
                "remote-app", input_data, output_type=_StubOutput
            )

        assert isinstance(result, _StubOutput)
        assert result.result == "ok"

    async def test_call_by_name_no_result_type_passes_through(self):
        """call_by_name() without result_type does not set it on execute_child_workflow."""
        client = _make_client()
        input_data = _StubInput()

        with (
            patch(
                "application_sdk.app.client.workflow.execute_child_workflow",
                new_callable=AsyncMock,
                return_value=_StubOutput(result="pass-through"),
            ) as mock_exec,
            patch(
                "application_sdk.app.client.workflow.uuid4",
                return_value="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            ),
            patch(
                "application_sdk.app.client.workflow.now",
                return_value=datetime(2025, 1, 1),
            ),
            patch("application_sdk.app.client._safe_log"),
        ):
            result = await client.call_by_name("remote-app", input_data)

        # call_by_name does NOT pass result_type to execute_child_workflow
        assert "result_type" not in mock_exec.call_args.kwargs
        assert isinstance(result, _StubOutput)

    async def test_call_by_name_propagates_exceptions(self):
        """call_by_name() propagates exceptions and logs error."""
        client = _make_client()
        input_data = _StubInput()

        with (
            patch(
                "application_sdk.app.client.workflow.execute_child_workflow",
                new_callable=AsyncMock,
                side_effect=ValueError("bad input"),
            ),
            patch(
                "application_sdk.app.client.workflow.uuid4",
                return_value="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            ),
            patch(
                "application_sdk.app.client.workflow.now",
                return_value=datetime(2025, 1, 1),
            ),
            patch("application_sdk.app.client._safe_log") as mock_log,
        ):
            with pytest.raises(ValueError, match="bad input"):
                await client.call_by_name("remote-app", input_data)

        error_calls = [c for c in mock_log.call_args_list if c.args[0] == "error"]
        assert len(error_calls) == 1

    async def test_call_by_name_with_task_queue(self):
        """call_by_name() passes task_queue to execute_child_workflow."""
        client = _make_client()
        input_data = _StubInput()

        with (
            patch(
                "application_sdk.app.client.workflow.execute_child_workflow",
                new_callable=AsyncMock,
                return_value={},
            ) as mock_exec,
            patch(
                "application_sdk.app.client.workflow.uuid4",
                return_value="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            ),
            patch(
                "application_sdk.app.client.workflow.now",
                return_value=datetime(2025, 1, 1),
            ),
            patch("application_sdk.app.client._safe_log"),
        ):
            await client.call_by_name(
                "remote-app", input_data, task_queue="special-queue"
            )

        assert mock_exec.call_args.kwargs["task_queue"] == "special-queue"
