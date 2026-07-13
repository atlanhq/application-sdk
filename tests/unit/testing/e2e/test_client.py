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

from application_sdk.testing.e2e._errors import (
    AtlanApiHttpError,
    AtlanApiTimeoutError,
    NoWorkerOnTaskQueueError,
)
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


def _result(run_status: DAGRunStatus, node_status: DAGNodeStatus) -> DAGRunResult:
    return DAGRunResult(
        run_id=_RUN_ID,
        workflow_slug="slug",
        status=run_status,
        nodes=[
            DAGNodeResult(
                name="extract",
                status=node_status,
                started_at_ms=None,
                completed_at_ms=None,
                error_message=None,
            )
        ],
    )


class TestPollNativeStatusStallGuard:
    """poll_native_status must fail fast when no node starts (no worker)."""

    def test_raises_when_no_node_starts_within_grace(self):
        """Run is Running but the extract node stays Pending → the parent is on
        the AE queue while no worker polls the extract queue. Must raise
        NoWorkerOnTaskQueueError once the grace window elapses, not hang."""
        client = _make_client()
        stuck = _result(DAGRunStatus.RUNNING, DAGNodeStatus.PENDING)

        with patch.object(client, "get_native_status", return_value=stuck):
            with patch("time.sleep"):
                with pytest.raises(NoWorkerOnTaskQueueError) as exc:
                    client.poll_native_status(
                        _RUN_ID,
                        interval_seconds=10,
                        timeout_seconds=600,
                        stall_grace_seconds=30,
                        stall_task_queue="atlan-openapi-e2e-full-ci-42",
                    )
        # Message names the queue so the operator can spot the mismatch.
        assert "atlan-openapi-e2e-full-ci-42" in str(exc.value)

    def test_raises_when_node_stuck_in_scheduled(self):
        """Symmetric to the Pending case: production treats both Pending AND
        Scheduled as 'not started' (client.py), so a node stuck in Scheduled
        must keep the guard armed and raise. Guards against a regression that
        drops SCHEDULED from the not-started set."""
        client = _make_client()
        stuck = _result(DAGRunStatus.RUNNING, DAGNodeStatus.SCHEDULED)

        with patch.object(client, "get_native_status", return_value=stuck):
            with patch("time.sleep"):
                with pytest.raises(NoWorkerOnTaskQueueError):
                    client.poll_native_status(
                        _RUN_ID,
                        interval_seconds=10,
                        timeout_seconds=600,
                        stall_grace_seconds=30,
                        stall_task_queue="atlan-openapi-e2e-full-ci-42",
                    )

    def test_generic_queue_hint_when_stall_task_queue_empty(self):
        """With no stall_task_queue supplied, the error falls back to the
        generic 'the extract task queue' phrasing."""
        client = _make_client()
        stuck = _result(DAGRunStatus.RUNNING, DAGNodeStatus.PENDING)

        with patch.object(client, "get_native_status", return_value=stuck):
            with patch("time.sleep"):
                with pytest.raises(NoWorkerOnTaskQueueError) as exc:
                    client.poll_native_status(
                        _RUN_ID,
                        interval_seconds=10,
                        timeout_seconds=600,
                        stall_grace_seconds=30,
                        stall_task_queue="",
                    )
        assert "the extract task queue" in str(exc.value)

    def test_no_raise_when_node_starts_before_grace(self):
        """Once any node reaches Running the guard latches off — a long-running
        node past the grace window must NOT trip it."""
        client = _make_client()
        running = _result(DAGRunStatus.RUNNING, DAGNodeStatus.RUNNING)
        done = _succeeded_result()
        # Node running for many polls (well past grace), then succeeds.
        side_effects = [running] * 6 + [done]

        with patch.object(client, "get_native_status", side_effect=side_effects):
            with patch("time.sleep"):
                result = client.poll_native_status(
                    _RUN_ID,
                    interval_seconds=10,
                    timeout_seconds=600,
                    stall_grace_seconds=5,
                    stall_task_queue="atlan-openapi-e2e-full-ci-42",
                )
        assert result.status == DAGRunStatus.SUCCEEDED

    def test_guard_disabled_when_grace_none(self):
        """stall_grace_seconds=None disables the guard: a stuck run polls to the
        timeout and returns the last observation instead of raising."""
        client = _make_client()
        stuck = _result(DAGRunStatus.RUNNING, DAGNodeStatus.PENDING)

        with patch.object(client, "get_native_status", return_value=stuck):
            with patch("time.sleep"):
                result = client.poll_native_status(
                    _RUN_ID,
                    interval_seconds=10,
                    timeout_seconds=30,
                    stall_grace_seconds=None,
                )
        assert result.status == DAGRunStatus.RUNNING

    def test_guard_disabled_when_grace_zero(self):
        """stall_grace_seconds=0 also disables the guard (base passes the int
        attr directly, so 0 must be treated as off, not 'grace of 0')."""
        client = _make_client()
        stuck = _result(DAGRunStatus.RUNNING, DAGNodeStatus.PENDING)

        with patch.object(client, "get_native_status", return_value=stuck):
            with patch("time.sleep"):
                result = client.poll_native_status(
                    _RUN_ID,
                    interval_seconds=10,
                    timeout_seconds=30,
                    stall_grace_seconds=0,
                )
        assert result.status == DAGRunStatus.RUNNING

    def test_negative_grace_does_not_fire_on_first_poll(self):
        """A negative grace is non-positive → disabled, NOT 'fire immediately'
        (elapsed 0 >= -1 would otherwise trip the guard on the first poll)."""
        client = _make_client()
        stuck = _result(DAGRunStatus.RUNNING, DAGNodeStatus.PENDING)

        with patch.object(client, "get_native_status", return_value=stuck):
            with patch("time.sleep"):
                result = client.poll_native_status(
                    _RUN_ID,
                    interval_seconds=10,
                    timeout_seconds=30,
                    stall_grace_seconds=-1,
                )
        assert result.status == DAGRunStatus.RUNNING


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
