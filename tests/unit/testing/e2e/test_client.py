"""Unit tests for AEWorkflowClient.poll_native_status transient-error handling.

Verifies that the except AppError branch in poll_native_status is reachable
(i.e. AtlanApiHttpError — which inherits AppError — is caught) and that the
transient_streak budget works correctly.
"""

from __future__ import annotations

import io
import urllib.error
from unittest.mock import patch

import pytest

from application_sdk.testing.e2e._errors import AtlanApiHttpError, AtlanApiTimeoutError
from application_sdk.testing.e2e.client import (
    _REQUEST_MAX_ATTEMPTS,
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


class TestPostWithRetry:
    """_post_with_retry: locks in the timeout-is-retriable contract."""

    def test_timeout_then_success(self):
        """TimeoutError on attempt 1 → succeeds on attempt 2."""
        client = _make_client()
        with (
            patch.object(
                client,
                "_request",
                side_effect=[TimeoutError("timed out"), (200, {"ok": True})],
            ),
            patch("time.sleep"),
        ):
            status, body = client._post_with_retry(
                "/some/path",
                total_attempts=2,
                sleep_seconds=1,
                retryable=lambda s, b: s >= 500,
                op_name="test_op",
            )
        assert status == 200
        assert body == {"ok": True}

    def test_repeated_timeout_raises_atlan_timeout(self):
        """TimeoutError on every attempt → raises AtlanApiTimeoutError."""
        client = _make_client()
        with (
            patch.object(client, "_request", side_effect=TimeoutError("timed out")),
            patch("time.sleep"),
            pytest.raises(AtlanApiTimeoutError),
        ):
            client._post_with_retry(
                "/some/path",
                total_attempts=3,
                sleep_seconds=1,
                retryable=lambda s, b: s >= 500,
                op_name="test_op",
            )

    def test_5xx_then_success(self):
        """HTTP 503 on attempt 1 → succeeds on attempt 2."""
        client = _make_client()
        with (
            patch.object(
                client,
                "_request",
                side_effect=[(503, {"err": "overloaded"}), (200, {"ok": True})],
            ),
            patch("time.sleep"),
        ):
            status, _ = client._post_with_retry(
                "/some/path",
                total_attempts=2,
                sleep_seconds=1,
                retryable=lambda s, b: s >= 500,
                op_name="test_op",
            )
        assert status == 200

    def test_non_retryable_4xx_returns_immediately_without_sleep(self):
        """HTTP 404 with retryable=False → returns on first attempt, no sleep."""
        client = _make_client()
        with (
            patch.object(client, "_request", side_effect=[(404, {"not": "found"})]),
            patch("time.sleep") as mock_sleep,
        ):
            status, _ = client._post_with_retry(
                "/some/path",
                total_attempts=5,
                sleep_seconds=1,
                retryable=lambda s, b: s >= 500,
                op_name="test_op",
            )
        assert status == 404
        mock_sleep.assert_not_called()

    def test_2xx_body_shape_retry(self):
        """2xx with wrong body shape retries; 2xx with correct shape returns.

        Exercises the ``b`` argument of the retryable predicate — the exact
        path introduced for publish_version.  A future refactor that drops
        ``b`` from the _post_with_retry call site would silently break
        publish_version; this test would catch it.
        """
        client = _make_client()
        pending = (200, {"status": "pending"})
        success = (200, {"status": "success"})
        with (
            patch.object(client, "_request", side_effect=[pending, success]),
            patch("time.sleep") as mock_sleep,
        ):
            status, body = client._post_with_retry(
                "/some/path",
                total_attempts=3,
                sleep_seconds=1,
                retryable=lambda s, b: s >= 300
                or not (isinstance(b, dict) and b.get("status") == "success"),
                op_name="test_op",
            )
        assert status == 200
        assert isinstance(body, dict) and body.get("status") == "success"
        mock_sleep.assert_called_once()


class _FakeResponse:
    """Minimal urlopen() return value usable as a context manager."""

    def __init__(self, status: int, raw: bytes):
        self.status = status
        self._raw = raw

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self) -> bytes:
        return self._raw


class TestRequestNetworkRetry:
    """_request: transient network errors retry; sustained ones surface as
    AtlanApiTimeoutError (an AppError) so the poll loop tolerates them instead
    of crashing on a raw URLError/TimeoutError mid-poll."""

    def test_retries_transient_then_succeeds(self):
        """URLError on attempt 1 → succeeds on attempt 2."""
        client = _make_client()
        ok = _FakeResponse(200, b'{"ok": true}')
        with (
            patch(
                "application_sdk.testing.e2e.client.urllib.request.urlopen",
                side_effect=[urllib.error.URLError("dns blip"), ok],
            ),
            patch("time.sleep"),
        ):
            status, body = client._request("GET", "/native-status")
        assert status == 200
        assert body == {"ok": True}

    def test_sustained_network_error_raises_atlan_timeout(self):
        """URLError on every attempt → AtlanApiTimeoutError after max attempts."""
        client = _make_client()
        with (
            patch(
                "application_sdk.testing.e2e.client.urllib.request.urlopen",
                side_effect=urllib.error.URLError("name resolution"),
            ) as mock_open,
            patch("time.sleep"),
            pytest.raises(AtlanApiTimeoutError),
        ):
            client._request("GET", "/native-status")
        assert mock_open.call_count == _REQUEST_MAX_ATTEMPTS

    def test_httperror_returns_immediately_without_retry(self):
        """A real 5xx HTTP response is returned, not retried as a network error."""
        client = _make_client()
        http_err = urllib.error.HTTPError(
            url="https://tenant.example.com/native-status",
            code=500,
            msg="server error",
            hdrs=None,
            fp=io.BytesIO(b'{"err": "boom"}'),
        )
        with (
            patch(
                "application_sdk.testing.e2e.client.urllib.request.urlopen",
                side_effect=http_err,
            ) as mock_open,
            patch("time.sleep") as mock_sleep,
        ):
            status, body = client._request("GET", "/native-status")
        assert status == 500
        assert body == {"err": "boom"}
        assert mock_open.call_count == 1
        mock_sleep.assert_not_called()


class TestRequiredNodesSubset:
    """required_dag_nodes: assert/poll only a subset of the DAG (e.g. the
    publish-path test cares about extract+publish, not qi/lineage)."""

    @staticmethod
    def _result(status: DAGRunStatus, node_states: dict) -> DAGRunResult:
        return DAGRunResult(
            run_id=_RUN_ID,
            workflow_slug="slug",
            status=status,
            nodes=[
                DAGNodeResult(
                    name=name,
                    status=st,
                    started_at_ms=None,
                    completed_at_ms=None,
                    error_message=None,
                )
                for name, st in node_states.items()
            ],
        )

    def test_succeeded_for_subset_ignores_running_others(self):
        # extract+publish succeeded; qi/lineage still running → required subset OK.
        r = self._result(
            DAGRunStatus.RUNNING,
            {
                "extract": DAGNodeStatus.SUCCEEDED,
                "publish": DAGNodeStatus.SUCCEEDED,
                "qi": DAGNodeStatus.RUNNING,
                "lineage-app": DAGNodeStatus.PENDING,
            },
        )
        assert r.succeeded_for({"extract", "publish"}) is True
        assert r.all_nodes_succeeded is False  # whole-DAG view still not done
        assert r.terminal_for({"extract", "publish"}) is True
        assert r.failed_for({"extract", "publish"}) == []

    def test_subset_fails_when_required_node_failed(self):
        r = self._result(
            DAGRunStatus.RUNNING,
            {"extract": DAGNodeStatus.SUCCEEDED, "publish": DAGNodeStatus.FAILED},
        )
        assert r.succeeded_for({"extract", "publish"}) is False
        assert r.terminal_for({"extract", "publish"}) is True  # failed is terminal
        assert [n.name for n in r.failed_for({"extract", "publish"})] == ["publish"]

    def test_none_required_falls_back_to_whole_dag(self):
        r = self._result(
            DAGRunStatus.RUNNING,
            {"extract": DAGNodeStatus.SUCCEEDED, "qi": DAGNodeStatus.RUNNING},
        )
        assert r.succeeded_for(None) is r.all_nodes_succeeded
        assert r.terminal_for(None) is r.status.is_terminal

    def test_poll_returns_once_required_nodes_terminal(self):
        """Poll stops as soon as the required subset is terminal, even though the
        top-level status is still Running (qi/lineage unfinished)."""
        client = _make_client()
        partial = self._result(
            DAGRunStatus.RUNNING,
            {
                "extract": DAGNodeStatus.SUCCEEDED,
                "publish": DAGNodeStatus.SUCCEEDED,
                "qi": DAGNodeStatus.RUNNING,
            },
        )
        with (
            patch.object(client, "get_native_status", return_value=partial) as g,
            patch("time.sleep"),
        ):
            out = client.poll_native_status(
                _RUN_ID,
                interval_seconds=1,
                timeout_seconds=60,
                required_nodes={"extract", "publish"},
            )
        assert out is partial
        assert g.call_count == 1  # returned on the first poll, didn't wait for qi
