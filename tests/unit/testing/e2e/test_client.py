"""Unit tests for AEWorkflowClient.poll_native_status transient-error handling.

Verifies that the except AppError branch in poll_native_status is reachable
(i.e. AtlanApiHttpError — which inherits AppError — is caught) and that the
transient_streak budget works correctly.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from application_sdk.testing.e2e._errors import AtlanApiHttpError, AtlanApiTimeoutError
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

    def test_body_transform_applied_per_attempt(self):
        """body_transform receives the attempt number and may mutate the body.

        This locks in the mechanism used by submit_workflow to avoid
        "duplicate key value violates unique constraint credentials_name_key"
        when AE's internal CreateVersion call times out on attempt 1 (returning
        HTTP 500) after already committing the credential to the DB.  On attempt
        2 the transform appends -r2 to the credential name, producing a fresh
        name that doesn't conflict with the orphaned record from attempt 1.
        """
        client = _make_client()
        bodies_seen: list[dict | None] = []

        def _capture_and_succeed(method, path, body=None, timeout=None):
            bodies_seen.append(body)
            if len(bodies_seen) == 1:
                return (500, {"error": "timeout"})
            return (200, {"ok": True})

        def _suffix_transform(b, attempt):
            if attempt == 1 or b is None:
                return b
            return {**b, "name": f"{b.get('name', '')}-r{attempt}"}

        with (
            patch.object(client, "_request", side_effect=_capture_and_succeed),
            patch("time.sleep"),
        ):
            status, _ = client._post_with_retry(
                "/some/path",
                body={"name": "cred-base"},
                body_transform=_suffix_transform,
                total_attempts=2,
                sleep_seconds=1,
                retryable=lambda s, b: s >= 500,
                op_name="test_op",
            )
        assert status == 200
        assert bodies_seen[0] == {"name": "cred-base"}
        assert bodies_seen[1] == {"name": "cred-base-r2"}

    def test_submit_workflow_credential_name_unique_per_attempt(self):
        """submit_workflow appends -r{n} to credential name on retries.

        Reproduces the failure pattern from a production CI run where AE's
        package-workflows?submit=true returned HTTP 500 (internal CreateVersion
        timeout) after committing the credential, then returned HTTP 400
        "duplicate key value violates unique constraint credentials_name_key"
        on the next attempt because the same payload was resubmitted.
        """
        client = _make_client()
        bodies_seen: list[dict | None] = []

        def _capture_and_succeed(method, path, body=None, timeout=None):
            bodies_seen.append(body)
            if len(bodies_seen) == 1:
                return (
                    500,
                    {
                        "code": 500,
                        "message": "ae: CreateVersion request failed: context deadline exceeded",
                    },
                )
            return (200, {"run_id": "abc-123"})

        submit_payload = {
            "metadata": {"name": "atlan-mysql-123"},
            "payload": [
                {
                    "parameter": "credentialGuid",
                    "type": "credential",
                    "body": {"name": "default-mysql-123-0", "authType": "basic"},
                }
            ],
        }

        with (
            patch.object(client, "_request", side_effect=_capture_and_succeed),
            patch("time.sleep"),
        ):
            run_id = client.submit_workflow(submit_payload, retries=1)

        assert run_id == "abc-123"
        cred_name_attempt_1 = bodies_seen[0]["payload"][0]["body"]["name"]
        cred_name_attempt_2 = bodies_seen[1]["payload"][0]["body"]["name"]
        assert cred_name_attempt_1 == "default-mysql-123-0"
        assert cred_name_attempt_2 == "default-mysql-123-0-r2"
