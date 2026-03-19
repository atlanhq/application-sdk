"""Unit tests for the cleanup interceptor."""

from __future__ import annotations

from typing import Any
from unittest import mock

import pytest

from application_sdk.interceptors.cleanup import (
    CleanupInterceptor,
    CleanupWorkflowInboundInterceptor,
    cleanup,
)


class TestCleanupInterceptor:
    """Tests for CleanupInterceptor."""

    def test_workflow_interceptor_class_returns_correct_type(self) -> None:
        interceptor = CleanupInterceptor()
        mock_input = mock.MagicMock()
        result = interceptor.workflow_interceptor_class(mock_input)
        assert result is CleanupWorkflowInboundInterceptor

    def test_workflow_interceptor_class_never_returns_none(self) -> None:
        interceptor = CleanupInterceptor()
        mock_input = mock.MagicMock()
        result = interceptor.workflow_interceptor_class(mock_input)
        assert result is not None


class TestCleanupWorkflowInboundInterceptor:
    """Tests for CleanupWorkflowInboundInterceptor."""

    @pytest.mark.asyncio
    async def test_calls_next_execute_workflow(self) -> None:
        mock_next = mock.AsyncMock()
        mock_next.execute_workflow = mock.AsyncMock(return_value="result")
        interceptor = CleanupWorkflowInboundInterceptor(mock_next)

        mock_input = mock.MagicMock()

        with mock.patch(
            "application_sdk.execution._temporal.interceptors.cleanup.workflow"
        ) as mock_wf:
            mock_wf.execute_activity = mock.AsyncMock(return_value=None)
            result = await interceptor.execute_workflow(mock_input)

        mock_next.execute_workflow.assert_called_once_with(mock_input)
        assert result == "result"

    @pytest.mark.asyncio
    async def test_calls_cleanup_activity_after_workflow(self) -> None:
        mock_next = mock.AsyncMock()
        mock_next.execute_workflow = mock.AsyncMock(return_value="done")
        interceptor = CleanupWorkflowInboundInterceptor(mock_next)

        mock_input = mock.MagicMock()

        with mock.patch(
            "application_sdk.execution._temporal.interceptors.cleanup.workflow"
        ) as mock_wf:
            mock_wf.execute_activity = mock.AsyncMock(return_value=None)
            await interceptor.execute_workflow(mock_input)

        mock_wf.execute_activity.assert_called_once()

    @pytest.mark.asyncio
    async def test_reraises_workflow_exception(self) -> None:
        mock_next = mock.AsyncMock()
        mock_next.execute_workflow = mock.AsyncMock(
            side_effect=RuntimeError("workflow failed")
        )
        interceptor = CleanupWorkflowInboundInterceptor(mock_next)

        mock_input = mock.MagicMock()

        with mock.patch(
            "application_sdk.execution._temporal.interceptors.cleanup.workflow"
        ) as mock_wf:
            mock_wf.execute_activity = mock.AsyncMock(return_value=None)
            with pytest.raises(RuntimeError, match="workflow failed"):
                await interceptor.execute_workflow(mock_input)

    @pytest.mark.asyncio
    async def test_cleanup_failure_does_not_fail_workflow(self) -> None:
        mock_next = mock.AsyncMock()
        mock_next.execute_workflow = mock.AsyncMock(return_value="ok")
        interceptor = CleanupWorkflowInboundInterceptor(mock_next)

        mock_input = mock.MagicMock()

        with mock.patch(
            "application_sdk.execution._temporal.interceptors.cleanup.workflow"
        ) as mock_wf:
            mock_wf.execute_activity = mock.AsyncMock(
                side_effect=Exception("cleanup error")
            )
            # Should not raise — cleanup failures are swallowed
            result = await interceptor.execute_workflow(mock_input)

        assert result == "ok"


class TestCleanupActivity:
    """Tests for the cleanup() activity function."""

    @pytest.mark.asyncio
    async def test_removes_existing_directory(self, tmp_path: Any) -> None:
        # Create a test directory structure
        test_dir = tmp_path / "test-workflow-artifacts"
        test_dir.mkdir()
        (test_dir / "some-file.txt").write_text("data")

        with mock.patch(
            "application_sdk.execution._temporal.interceptors.cleanup.build_output_path",
            return_value="test-run-id",
        ):
            with mock.patch(
                "application_sdk.execution._temporal.interceptors.cleanup.CLEANUP_BASE_PATHS",
                [str(test_dir)],
            ):
                result = await cleanup()

        assert not test_dir.exists()
        assert result.path_results[str(test_dir)] is True

    @pytest.mark.asyncio
    async def test_no_error_when_directory_missing(self, tmp_path: Any) -> None:
        missing_dir = str(tmp_path / "nonexistent-dir")

        with mock.patch(
            "application_sdk.execution._temporal.interceptors.cleanup.build_output_path",
            return_value="test-run-id",
        ):
            with mock.patch(
                "application_sdk.execution._temporal.interceptors.cleanup.CLEANUP_BASE_PATHS",
                [missing_dir],
            ):
                result = await cleanup()

        # Missing directory → success (True)
        assert result.path_results[missing_dir] is True

    @pytest.mark.asyncio
    async def test_returns_cleanup_result(self, tmp_path: Any) -> None:
        test_dir = tmp_path / "cleanup-test"
        test_dir.mkdir()

        with mock.patch(
            "application_sdk.execution._temporal.interceptors.cleanup.build_output_path",
            return_value="test-run-id",
        ):
            with mock.patch(
                "application_sdk.execution._temporal.interceptors.cleanup.CLEANUP_BASE_PATHS",
                [str(test_dir)],
            ):
                result = await cleanup()

        from application_sdk.interceptors.cleanup import CleanupResult

        assert isinstance(result, CleanupResult)
        assert isinstance(result.path_results, dict)
