"""Tests for the public context API (workflow_id, output_prefix, build_output_path).

These properties were promoted from private activity_utils helpers to the
public AppContext / TaskExecutionContext API in BLDX-1149.
"""

from __future__ import annotations

import warnings
from unittest.mock import MagicMock, patch

import pytest

from application_sdk.app.context import AppContext, TaskExecutionContext
from application_sdk.observability.context import (
    ExecutionContext,
    set_execution_context,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def _activity_execution_context():
    """Set up an ExecutionContext that looks like a Temporal activity."""
    ctx = ExecutionContext(
        execution_type="activity",
        workflow_id="wf-test-123",
        workflow_run_id="run-abc-456",
        workflow_type="TestWorkflow",
        namespace="default",
        task_queue="test-queue",
        attempt=1,
        activity_id="act-1",
        activity_type="TestActivity",
    )
    set_execution_context(ctx)
    yield
    # Reset to default
    set_execution_context(ExecutionContext())


@pytest.fixture()
def app_context() -> AppContext:
    return AppContext(app_name="test-app", app_version="1.0.0", run_id="run-123")


@pytest.fixture()
def task_exec_context(app_context: AppContext) -> TaskExecutionContext:
    heartbeat = MagicMock()
    return TaskExecutionContext(
        app_context=app_context,
        task_name="my_task",
        heartbeat_controller=heartbeat,
    )


# ---------------------------------------------------------------------------
# AppContext.workflow_id / workflow_run_id
# ---------------------------------------------------------------------------


class TestAppContextWorkflowProperties:
    """Tests for AppContext.workflow_id and AppContext.workflow_run_id."""

    @pytest.mark.usefixtures("_activity_execution_context")
    def test_workflow_id_returns_value_from_execution_context(
        self, app_context: AppContext
    ) -> None:
        assert app_context.workflow_id == "wf-test-123"

    @pytest.mark.usefixtures("_activity_execution_context")
    def test_workflow_run_id_returns_value_from_execution_context(
        self, app_context: AppContext
    ) -> None:
        assert app_context.workflow_run_id == "run-abc-456"

    def test_workflow_id_empty_outside_temporal(self, app_context: AppContext) -> None:
        """Outside Temporal, workflow_id is an empty string (default)."""
        # Reset to default context (no Temporal)
        set_execution_context(ExecutionContext())
        assert app_context.workflow_id == ""

    def test_workflow_run_id_empty_outside_temporal(
        self, app_context: AppContext
    ) -> None:
        set_execution_context(ExecutionContext())
        assert app_context.workflow_run_id == ""


# ---------------------------------------------------------------------------
# TaskExecutionContext.output_prefix / build_output_path
# ---------------------------------------------------------------------------


class TestTaskExecutionContextOutputPath:
    """Tests for TaskExecutionContext.output_prefix and build_output_path."""

    @patch(
        "application_sdk.execution._temporal.activity_utils._build_output_path",
        return_value="artifacts/apps/myapp/workflows/wf-1/run-2",
    )
    def test_output_prefix(
        self, mock_build: MagicMock, task_exec_context: TaskExecutionContext
    ) -> None:
        result = task_exec_context.output_prefix
        assert result == "artifacts/apps/myapp/workflows/wf-1/run-2"
        mock_build.assert_called_once()

    @patch(
        "application_sdk.execution._temporal.activity_utils._build_output_path",
        return_value="artifacts/apps/myapp/workflows/wf-1/run-2",
    )
    def test_build_output_path_no_segments(
        self, mock_build: MagicMock, task_exec_context: TaskExecutionContext
    ) -> None:
        result = task_exec_context.build_output_path()
        assert result == "artifacts/apps/myapp/workflows/wf-1/run-2"

    @patch(
        "application_sdk.execution._temporal.activity_utils._build_output_path",
        return_value="artifacts/apps/myapp/workflows/wf-1/run-2",
    )
    def test_build_output_path_with_segments(
        self, mock_build: MagicMock, task_exec_context: TaskExecutionContext
    ) -> None:
        result = task_exec_context.build_output_path("raw", "tables")
        assert result == "artifacts/apps/myapp/workflows/wf-1/run-2/raw/tables"

    @patch(
        "application_sdk.execution._temporal.activity_utils._build_output_path",
        return_value="artifacts/apps/myapp/workflows/wf-1/run-2",
    )
    def test_build_output_path_single_segment(
        self, mock_build: MagicMock, task_exec_context: TaskExecutionContext
    ) -> None:
        result = task_exec_context.build_output_path("output.parquet")
        assert result == "artifacts/apps/myapp/workflows/wf-1/run-2/output.parquet"


# ---------------------------------------------------------------------------
# Deprecation warnings on old functions
# ---------------------------------------------------------------------------


class TestDeprecationWarnings:
    """Old activity_utils functions should emit DeprecationWarning."""

    @patch(
        "application_sdk.execution._temporal.activity_utils.activity",
    )
    def test_get_workflow_id_warns(self, mock_activity: MagicMock) -> None:
        mock_activity.info.return_value.workflow_id = "wf-dep"
        from application_sdk.execution._temporal.activity_utils import get_workflow_id

        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            result = get_workflow_id()
            assert result == "wf-dep"
            assert len(w) == 1
            assert issubclass(w[0].category, DeprecationWarning)
            assert "self.context.workflow_id" in str(w[0].message)

    @patch(
        "application_sdk.execution._temporal.activity_utils.activity",
    )
    def test_get_workflow_run_id_warns(self, mock_activity: MagicMock) -> None:
        mock_activity.info.return_value.workflow_run_id = "run-dep"
        from application_sdk.execution._temporal.activity_utils import (
            get_workflow_run_id,
        )

        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            result = get_workflow_run_id()
            assert result == "run-dep"
            assert len(w) == 1
            assert issubclass(w[0].category, DeprecationWarning)
            assert "self.context.workflow_run_id" in str(w[0].message)

    @patch(
        "application_sdk.execution._temporal.activity_utils.activity",
    )
    def test_build_output_path_warns(self, mock_activity: MagicMock) -> None:
        mock_activity.info.return_value.workflow_id = "wf-1"
        mock_activity.info.return_value.workflow_run_id = "run-1"
        from application_sdk.execution._temporal.activity_utils import build_output_path

        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            result = build_output_path()
            assert "wf-1" in result
            # build_output_path emits 1 warning (its own); the internal
            # _get_workflow_id / _get_workflow_run_id do NOT warn.
            deprecation_warnings = [
                x for x in w if issubclass(x.category, DeprecationWarning)
            ]
            assert len(deprecation_warnings) == 1
            assert "task_context" in str(deprecation_warnings[0].message)
