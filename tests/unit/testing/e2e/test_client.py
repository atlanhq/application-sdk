"""Unit tests for AEWorkflowClient.poll_native_status transient-error handling.

Verifies that the except AppError branch in poll_native_status is reachable
(i.e. AtlanApiHttpError — which inherits AppError — is caught) and that the
transient_streak budget works correctly.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from application_sdk.testing.e2e._errors import AtlanApiHttpError
from application_sdk.testing.e2e.client import (
    AEWorkflowClient,
    DAGNodeResult,
    DAGNodeStatus,
    DAGRunResult,
    DAGRunStatus,
)

_RUN_ID = "test-run-123"


def _make_client() -> AEWorkflowClient:
    return AEWorkflowClient(
        tenant_url="https://tenant.example.com",
        api_token="tok-test",
    )


def _succeeded_result() -> DAGRunResult:
    return DAGRunResult(
        run_id=_RUN_ID,
        workflow_slug="slug",
        status=DAGRunStatus.SUCCEEDED,
        nodes=[
            DAGNodeResult(
                name="extract",
                status=DAGNodeStatus.SUCCEEDED,
                started_at_ms=None,
                completed_at_ms=None,
                error_message=None,
            )
        ],
    )


def _http_error() -> AtlanApiHttpError:
    return AtlanApiHttpError(
        message="AE-COMMON-500-01: An unexpected error occurred",
        target="GET /api/service/package-workflows/native-status HTTP 500",
    )


class TestPollNativeStatusTransientHandling:
    """poll_native_status must survive N < max_transient_failures blips."""

    def test_survives_transient_errors_below_threshold(self):
        """N=2 failures followed by success with max=5 → returns result."""
        client = _make_client()
        err = _http_error()
        ok = _succeeded_result()
        side_effects = [err, err, ok]

        with patch.object(client, "get_native_status", side_effect=side_effects):
            with patch("time.sleep"):
                result = client.poll_native_status(
                    _RUN_ID,
                    interval_seconds=1,
                    timeout_seconds=60,
                    max_transient_failures=5,
                )

        assert result.status == DAGRunStatus.SUCCEEDED

    def test_gives_up_at_max_transient_failures(self):
        """N=max consecutive failures → re-raises the AppError."""
        client = _make_client()
        max_failures = 3
        side_effects = [_http_error()] * max_failures

        with patch.object(client, "get_native_status", side_effect=side_effects):
            with patch("time.sleep"):
                with pytest.raises(AtlanApiHttpError):
                    client.poll_native_status(
                        _RUN_ID,
                        interval_seconds=1,
                        timeout_seconds=60,
                        max_transient_failures=max_failures,
                    )

    def test_streak_resets_after_success(self):
        """Error → success → errors below threshold → success: streak resets."""
        client = _make_client()
        err = _http_error()
        ok = _succeeded_result()
        # 2 errors, then a success (streak reset), then 2 more errors, then final success
        running = DAGRunResult(
            run_id=_RUN_ID,
            workflow_slug="slug",
            status=DAGRunStatus.RUNNING,
            nodes=[],
        )
        side_effects = [err, err, running, err, err, ok]

        with patch.object(client, "get_native_status", side_effect=side_effects):
            with patch("time.sleep"):
                result = client.poll_native_status(
                    _RUN_ID,
                    interval_seconds=1,
                    timeout_seconds=60,
                    max_transient_failures=5,
                )

        assert result.status == DAGRunStatus.SUCCEEDED

    def test_non_app_error_propagates_immediately(self):
        """A non-AppError exception (e.g. ValueError) is not swallowed."""
        client = _make_client()

        with (
            patch.object(
                client, "get_native_status", side_effect=ValueError("unexpected")
            ),
            patch("time.sleep"),
            pytest.raises(ValueError, match="unexpected"),
        ):
            client.poll_native_status(
                _RUN_ID,
                interval_seconds=1,
                timeout_seconds=60,
                max_transient_failures=5,
            )
