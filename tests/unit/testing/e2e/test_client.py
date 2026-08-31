"""Unit tests for ``AEWorkflowClient``, the sync face of the harness's AE reader.

Driven through the public sync client rather than through
:class:`~application_sdk.testing.harness.automation_engine.client.AEClient`
directly, because that is the surface every connector suite calls and the one
whose behaviour child F promised not to change. The seams patched below moved
onto ``client._ae`` with the AE half (FND-242); everything above them —
signatures, return types, which leaf is raised, how many POSTs a retry makes —
is asserted exactly as it was.

Two sleeps, two seams, as before the split:

* :data:`_SLEEP` is the *retry* loops' own inter-attempt gap, where
  ``time.sleep`` used to be, and with the same reach.
* the *deadline* loops sleep through
  :mod:`application_sdk.testing.harness._poll`, so they need
  :func:`~application_sdk.testing.harness._poll.fake_clock` instead — which is
  why patching one has never silenced the other.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import httpx
import pytest

from application_sdk.testing.e2e._errors import (
    AppNotReadyError,
    AtlanAEWorkflowAlreadyActiveError,
    AtlanApiHttpError,
    AtlanApiTimeoutError,
    AutomationEngineNotDispatchingError,
    DAGProgressStalledError,
    NoWorkerOnTaskQueueError,
    RequestDelivery,
)
from application_sdk.testing.e2e.client import (
    AEWorkflowClient,
    DAGNodeResult,
    DAGNodeStatus,
    DAGRunResult,
    DAGRunStatus,
    RunLookup,
)
from application_sdk.testing.harness._poll import fake_clock
from application_sdk.testing.harness.automation_engine.client import (
    _RECONCILE_CLOCK_SKEW_SECONDS,
    _REQUEST_MAX_ATTEMPTS,
)
from application_sdk.testing.harness.automation_engine.retry import (
    MAX_RETRY_AFTER_SECONDS as _MAX_RETRY_AFTER_SECONDS,
)
from application_sdk.testing.harness.automation_engine.retry import (
    RETRY_AFTER_BUDGET_SECONDS as _RETRY_AFTER_BUDGET_SECONDS,
)
from application_sdk.testing.harness.automation_engine.retry import (
    classify_delivery as _classify_delivery,
)
from application_sdk.testing.harness.automation_engine.retry import (
    is_already_active_run as _is_already_active_run,
)
from application_sdk.testing.harness.automation_engine.retry import (
    is_app_not_ready as _is_app_not_ready,
)
from application_sdk.testing.harness.automation_engine.retry import (
    is_credential_name_conflict as _is_credential_name_conflict,
)
from application_sdk.testing.harness.automation_engine.retry import (
    newest_run_since as _newest_run_since,
)
from application_sdk.testing.harness.automation_engine.retry import (
    parse_run_timestamp as _parse_run_timestamp,
)
from application_sdk.testing.harness.automation_engine.retry import (
    requested_retry_after as _requested_retry_after,
)
from application_sdk.testing.harness.automation_engine.retry import (
    retry_gap as _retry_gap,
)
from application_sdk.testing.harness.automation_engine.retry import (
    rotate_submit_credential_name as _rotate_submit_credential_name,
)
from application_sdk.testing.harness.automation_engine.wire import (
    safe_node_status as _safe_node_status,
)
from application_sdk.testing.harness.automation_engine.wire import (
    safe_run_status as _safe_run_status,
)
from application_sdk.testing.harness.bridge import run_sync

_RUN_ID = "test-run-123"

#: Where the AE write loops take their inter-attempt gap. The deadline loops
#: take theirs from ``_poll``'s swappable default instead, so patching this
#: cannot accidentally silence one of those.
_SLEEP = "application_sdk.testing.harness.automation_engine.client.sleep_async"


def _drive(coro):
    """Run one of the async reader's internals to completion.

    ``_request`` and ``_post_with_retry`` are private to
    :class:`~application_sdk.testing.harness.automation_engine.client.AEClient`
    and have no facade method, so the tests that drive them directly await them
    through the same bridge every public method uses.
    """
    return run_sync(coro)


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

        with patch.object(client._ae, "get_native_status", return_value=stuck):
            with fake_clock():
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

        with patch.object(client._ae, "get_native_status", return_value=stuck):
            with fake_clock():
                with pytest.raises(NoWorkerOnTaskQueueError):
                    client.poll_native_status(
                        _RUN_ID,
                        interval_seconds=10,
                        timeout_seconds=600,
                        stall_grace_seconds=30,
                        stall_task_queue="atlan-openapi-e2e-full-ci-42",
                    )

    def test_pending_run_is_attributed_to_ae_not_the_app(self):
        """A top-level run still Pending was never dispatched by AE.

        Nothing reached the app's task queue, so blaming a missing worker or a
        wrong agent_name sends triage at the wrong system. Must raise the AE
        error and must not name the app's queue.
        """
        client = _make_client()
        never_dispatched = _result(DAGRunStatus.PENDING, DAGNodeStatus.PENDING)

        with patch.object(
            client._ae, "get_native_status", return_value=never_dispatched
        ):
            with fake_clock():
                with pytest.raises(AutomationEngineNotDispatchingError) as exc:
                    client.poll_native_status(
                        _RUN_ID,
                        interval_seconds=10,
                        timeout_seconds=600,
                        stall_grace_seconds=30,
                        stall_task_queue="atlan-openapi-e2e-full-ci-42",
                    )
        message = str(exc.value)
        assert "atlan-openapi-e2e-full-ci-42" not in message
        assert "agent_spec()" not in message
        assert "automation engine" in message.lower()

    def test_generic_queue_hint_when_stall_task_queue_empty(self):
        """With no stall_task_queue supplied, the error falls back to the
        generic 'the extract task queue' phrasing."""
        client = _make_client()
        stuck = _result(DAGRunStatus.RUNNING, DAGNodeStatus.PENDING)

        with patch.object(client._ae, "get_native_status", return_value=stuck):
            with fake_clock():
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

        with patch.object(client._ae, "get_native_status", side_effect=side_effects):
            with fake_clock():
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

        with patch.object(client._ae, "get_native_status", return_value=stuck):
            with fake_clock():
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

        with patch.object(client._ae, "get_native_status", return_value=stuck):
            with fake_clock():
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

        with patch.object(client._ae, "get_native_status", return_value=stuck):
            with fake_clock():
                result = client.poll_native_status(
                    _RUN_ID,
                    interval_seconds=10,
                    timeout_seconds=30,
                    stall_grace_seconds=-1,
                )
        assert result.status == DAGRunStatus.RUNNING


class TestPollNativeStatusProgressWatchdog:
    """poll_native_status must fail fast when a node started but the DAG makes
    no forward progress for the watchdog window (a node wedged Running that the
    one-time start-stall latch cannot catch)."""

    def test_raises_when_node_stuck_running_no_progress(self):
        """Extract reaches Running (start-stall latch is off) then never changes
        state → DAGProgressStalledError once progress_stall_seconds elapses,
        instead of polling the full timeout."""
        client = _make_client()
        stuck = _result(DAGRunStatus.RUNNING, DAGNodeStatus.RUNNING)

        with patch.object(client._ae, "get_native_status", return_value=stuck):
            with fake_clock():
                with pytest.raises(DAGProgressStalledError) as exc:
                    client.poll_native_status(
                        _RUN_ID,
                        interval_seconds=10,
                        timeout_seconds=600,
                        stall_grace_seconds=None,  # isolate the progress watchdog
                        progress_stall_seconds=30,
                    )
        # The wedged node's state reaches the operator as the attached typed
        # result, not as a name=status list in the message: naming the task
        # queue and the child workflow needs the harness's seed DAG, so the
        # harness renders and the client only carries the observation.
        assert "No DAG node changed state for 30s" in str(exc.value)
        attached = exc.value.result
        assert attached is not None
        assert [n.name for n in attached.nodes] == ["extract"]
        assert attached.progress_stalled is True
        assert attached.progress_stalled_after_seconds == 30.0
        assert attached.stopped_watching is True
        # The watchdog is not the ceiling; conflating them would make the
        # renderer print "at the 600s poll ceiling" for a 30s stall.
        assert attached.timed_out is False
        assert attached.seconds_since_last_progress is not None
        assert attached.seconds_since_last_progress >= 30.0

    def test_disabled_when_progress_stall_none(self):
        """progress_stall_seconds=None disables the watchdog: a wedged-Running
        run polls to the timeout and returns the last observation, no raise."""
        client = _make_client()
        stuck = _result(DAGRunStatus.RUNNING, DAGNodeStatus.RUNNING)

        with patch.object(client._ae, "get_native_status", return_value=stuck):
            with fake_clock():
                result = client.poll_native_status(
                    _RUN_ID,
                    interval_seconds=10,
                    timeout_seconds=30,
                    stall_grace_seconds=None,
                    progress_stall_seconds=None,
                )
        assert result.status == DAGRunStatus.RUNNING

    def test_no_raise_when_run_progresses_to_terminal(self):
        """A run that keeps moving (Running → Succeeded) within the window is not
        killed — the terminal return wins and the watchdog never trips."""
        client = _make_client()
        running = _result(DAGRunStatus.RUNNING, DAGNodeStatus.RUNNING)
        done = _succeeded_result()

        with patch.object(
            client._ae, "get_native_status", side_effect=[running, running, done]
        ):
            with fake_clock():
                result = client.poll_native_status(
                    _RUN_ID,
                    interval_seconds=10,
                    timeout_seconds=600,
                    stall_grace_seconds=None,
                    progress_stall_seconds=30,
                )
        assert result.status == DAGRunStatus.SUCCEEDED


class TestSkippedStatus:
    """AE emits 'Skipped' as a real status; it must parse as a terminal enum
    value rather than being swallowed to PENDING (which masked a skipped run as
    a stalled one and surfaced as a spurious NoWorkerOnTaskQueueError)."""

    def test_run_skipped_parses_and_is_terminal(self):
        assert _safe_run_status("Skipped") is DAGRunStatus.SKIPPED
        assert DAGRunStatus.SKIPPED.is_terminal is True

    def test_node_skipped_parses_terminal_but_not_success(self):
        assert _safe_node_status("Skipped") is DAGNodeStatus.SKIPPED
        assert DAGNodeStatus.SKIPPED.is_terminal is True
        assert DAGNodeStatus.SKIPPED.is_success is False

    def test_skipped_run_returns_fast_without_tripping_stall_guard(self):
        """A Skipped run is terminal, so poll_native_status returns it
        immediately — even though no node started, the stall guard (which would
        otherwise raise NoWorkerOnTaskQueueError) must not fire on a terminal
        status."""
        client = _make_client()
        skipped = _result(DAGRunStatus.SKIPPED, DAGNodeStatus.PENDING)

        with patch.object(client._ae, "get_native_status", return_value=skipped):
            with fake_clock():
                result = client.poll_native_status(
                    _RUN_ID,
                    interval_seconds=10,
                    timeout_seconds=600,
                    stall_grace_seconds=5,
                    stall_task_queue="atlan-openapi-e2e-full-ci-42",
                )
        assert result.status == DAGRunStatus.SKIPPED


class TestPollNativeStatusTransientHandling:
    """poll_native_status must survive N < max_transient_failures blips."""

    def test_survives_transient_errors_below_threshold(self):
        """N=2 failures followed by success with max=5 → returns result."""
        client = _make_client()
        err = _http_error()
        ok = _succeeded_result()
        side_effects = [err, err, ok]

        with patch.object(client._ae, "get_native_status", side_effect=side_effects):
            with fake_clock():
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

        with patch.object(client._ae, "get_native_status", side_effect=side_effects):
            with fake_clock():
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

        with patch.object(client._ae, "get_native_status", side_effect=side_effects):
            with fake_clock():
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
                client._ae, "get_native_status", side_effect=ValueError("unexpected")
            ),
            fake_clock(),
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
                client._ae,
                "_request",
                side_effect=[TimeoutError("timed out"), (200, {"ok": True})],
            ),
            patch(_SLEEP),
        ):
            status, body = _drive(
                client._ae._post_with_retry(
                    "/some/path",
                    total_attempts=2,
                    sleep_seconds=1,
                    retryable=lambda s, b: s >= 500,
                    op_name="test_op",
                )
            )
        assert status == 200
        assert body == {"ok": True}

    def test_repeated_timeout_raises_atlan_timeout(self):
        """TimeoutError on every attempt → raises AtlanApiTimeoutError."""
        client = _make_client()
        with (
            patch.object(client._ae, "_request", side_effect=TimeoutError("timed out")),
            patch(_SLEEP),
            pytest.raises(AtlanApiTimeoutError),
        ):
            _drive(
                client._ae._post_with_retry(
                    "/some/path",
                    total_attempts=3,
                    sleep_seconds=1,
                    retryable=lambda s, b: s >= 500,
                    op_name="test_op",
                )
            )

    def test_5xx_then_success(self):
        """HTTP 503 on attempt 1 → succeeds on attempt 2."""
        client = _make_client()
        with (
            patch.object(
                client._ae,
                "_request",
                side_effect=[(503, {"err": "overloaded"}), (200, {"ok": True})],
            ),
            patch(_SLEEP),
        ):
            status, _ = _drive(
                client._ae._post_with_retry(
                    "/some/path",
                    total_attempts=2,
                    sleep_seconds=1,
                    retryable=lambda s, b: s >= 500,
                    op_name="test_op",
                )
            )
        assert status == 200

    def test_non_retryable_4xx_returns_immediately_without_sleep(self):
        """HTTP 404 with retryable=False → returns on first attempt, no sleep."""
        client = _make_client()
        with (
            patch.object(client._ae, "_request", side_effect=[(404, {"not": "found"})]),
            patch(_SLEEP) as mock_sleep,
        ):
            status, _ = _drive(
                client._ae._post_with_retry(
                    "/some/path",
                    total_attempts=5,
                    sleep_seconds=1,
                    retryable=lambda s, b: s >= 500,
                    op_name="test_op",
                )
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
            patch.object(client._ae, "_request", side_effect=[pending, success]),
            patch(_SLEEP) as mock_sleep,
        ):
            status, body = _drive(
                client._ae._post_with_retry(
                    "/some/path",
                    total_attempts=3,
                    sleep_seconds=1,
                    retryable=lambda s, b: (
                        s >= 300
                        or not (isinstance(b, dict) and b.get("status") == "success")
                    ),
                    op_name="test_op",
                )
            )
        assert status == 200
        assert isinstance(body, dict) and body.get("status") == "success"
        mock_sleep.assert_called_once()


# The exact body an overloaded tenant returned behind the 504s in the incident
# this behaviour was written for: five attempts 5s apart spent the whole retry
# budget ~20s into the 120s window the origin had asked for.
_OVERLOADED_504_BODY = {
    "retryable": True,
    "retry_after": 120,
    "what_you_should_do": "**Wait and retry.** Back off for at least 120 seconds.",
}


class TestRequestedRetryAfter:
    """_requested_retry_after: what counts as the origin asking us to wait."""

    @pytest.mark.parametrize(
        ("body", "expected"),
        [
            (_OVERLOADED_504_BODY, 120),
            ({"retryAfter": 30}, 30),
            ({"retry_after": "45"}, 45),
            # Fractional hints round up — 0.5s means "at least half a second",
            # so truncating to 0 would drop the hint entirely.
            ({"retry_after": 0.5}, 1),
            ({"retry_after": 1.2}, 2),
            # One level of envelope nesting, as AE's generic error shape uses.
            ({"data": {"retry_after": 60}}, 60),
            ({"error": {"retryAfter": 15}}, 15),
            ({"detail": {"retry_after": 7}}, 7),
        ],
    )
    def test_extracts_hint(self, body, expected):
        assert _requested_retry_after(body) == expected

    @pytest.mark.parametrize(
        "body",
        [
            {},
            {"retryable": True},
            {"retry_after": 0},
            {"retry_after": -5},
            {"retry_after": None},
            {"retry_after": "soon"},
            # bool is an int subclass — `retry_after: true` must not read as 1s.
            {"retry_after": True},
            # Nested more than one level deep: not a shape we've seen, and
            # guessing at arbitrary depth risks reading an unrelated field.
            {"data": {"error": {"retry_after": 60}}},
            "504 Gateway Timeout",
            None,
        ],
    )
    def test_no_usable_hint(self, body):
        assert _requested_retry_after(body) is None


class TestRetryGap:
    """_retry_gap: honour the origin's wait, bounded so it can't hang a leg."""

    def test_no_request_uses_the_fixed_gap(self):
        gap = _retry_gap(None, default_seconds=5, budget_left=300)
        assert gap.seconds == 5
        assert gap.requested is None
        assert gap.origin_note == ""

    def test_honours_the_requested_wait(self):
        gap = _retry_gap(120, default_seconds=5, budget_left=300)
        assert gap.seconds == 120
        assert gap.requested == 120

    def test_fixed_gap_is_a_floor_not_a_ceiling(self):
        """A request shorter than the loop's own gap doesn't shorten it."""
        gap = _retry_gap(1, default_seconds=10, budget_left=300)
        assert gap.seconds == 10
        assert gap.requested == 1
        # The wait is the floor, not a cap — the note must not say "capped".
        assert "floored" in gap.origin_note
        assert "capped" not in gap.origin_note

    def test_caps_a_pathological_request(self):
        gap = _retry_gap(86400, default_seconds=5, budget_left=100_000)
        assert gap.seconds == _MAX_RETRY_AFTER_SECONDS
        assert gap.requested == 86400
        assert "capped" in gap.origin_note

    def test_clamps_to_remaining_budget(self):
        gap = _retry_gap(120, default_seconds=5, budget_left=70)
        assert gap.seconds == 70

    @pytest.mark.parametrize("budget_left", [0, -30])
    def test_exhausted_budget_degrades_to_the_fixed_gap(self, budget_left):
        gap = _retry_gap(120, default_seconds=5, budget_left=budget_left)
        assert gap.seconds == 5


class TestPostWithRetryHonoursRetryAfter:
    """The regression this behaviour exists for: a 5xx that names its own window.

    Before, ``_post_with_retry`` slept its fixed gap regardless, so the whole
    attempt budget expired inside the window the origin had asked us to wait
    out — the retry could not succeed by construction.
    """

    def test_sleeps_for_the_window_the_origin_asked_for(self):
        client = _make_client()
        with (
            patch.object(
                client._ae,
                "_request",
                side_effect=[(504, _OVERLOADED_504_BODY), (200, {"ok": True})],
            ),
            patch(_SLEEP) as mock_sleep,
        ):
            status, _ = _drive(
                client._ae._post_with_retry(
                    "/some/path",
                    total_attempts=2,
                    sleep_seconds=5,
                    retryable=lambda s, b: s >= 500,
                    op_name="test_op",
                )
            )
        assert status == 200
        mock_sleep.assert_called_once_with(120)

    def test_5xx_without_a_hint_still_uses_the_fixed_gap(self):
        client = _make_client()
        with (
            patch.object(
                client._ae,
                "_request",
                side_effect=[(503, {"err": "overloaded"}), (200, {"ok": True})],
            ),
            patch(_SLEEP) as mock_sleep,
        ):
            _drive(
                client._ae._post_with_retry(
                    "/some/path",
                    total_attempts=2,
                    sleep_seconds=5,
                    retryable=lambda s, b: s >= 500,
                    op_name="test_op",
                )
            )
        mock_sleep.assert_called_once_with(5)

    def test_honoured_waiting_is_capped_per_call(self):
        """A tenant asking for 120s on every attempt can't stall a leg forever.

        The first attempts honour the request; once the per-call honoured
        budget is spent the remaining attempts fall back to the fixed gap, so
        total honoured-above-the-gap waiting stays at
        ``_RETRY_AFTER_BUDGET_SECONDS``.
        """
        client = _make_client()
        attempts = 5
        fixed_gap = 5
        with (
            patch.object(
                client._ae, "_request", return_value=(504, _OVERLOADED_504_BODY)
            ),
            patch(_SLEEP) as mock_sleep,
        ):
            status, _ = _drive(
                client._ae._post_with_retry(
                    "/some/path",
                    total_attempts=attempts,
                    sleep_seconds=fixed_gap,
                    retryable=lambda s, b: s >= 500,
                    op_name="test_op",
                )
            )
        assert status == 504
        slept = [call.args[0] for call in mock_sleep.call_args_list]
        # One sleep per attempt except the last — the attempt count is
        # unchanged by honouring the hint; only the gaps are.
        assert len(slept) == attempts - 1
        assert max(slept) == _MAX_RETRY_AFTER_SECONDS
        honoured = sum(gap - fixed_gap for gap in slept)
        assert honoured <= _RETRY_AFTER_BUDGET_SECONDS
        # Two full 120s waits, a third clipped to what's left of the budget,
        # then back on the fixed gap for the rest.
        assert slept == [120, 120, 70, fixed_gap]

    def test_terminal_error_carries_the_requested_wait(self):
        """create_workflow's raise keeps the hint for the operator to see."""
        client = _make_client()
        with (
            patch.object(
                client._ae, "_request", return_value=(504, _OVERLOADED_504_BODY)
            ),
            patch(_SLEEP),
            pytest.raises(AtlanApiHttpError) as excinfo,
        ):
            client.create_workflow("wf-name", retries=1, retry_sleep_seconds=1)
        assert excinfo.value.retry_after_seconds == 120


class TestPollNativeStatusHonoursRetryAfter:
    """The same fixed-gap bug in the poll loop: a 120s hint must not burn the
    transient-failure streak inside the origin's own wait window."""

    def test_backs_off_for_the_requested_window(self):
        client = _make_client()
        err = AtlanApiHttpError(
            message="native-status failed: HTTP 504",
            target="GET /api/service/package-workflows/native-status HTTP 504",
            retry_after_seconds=120,
        )
        with (
            patch.object(
                client._ae, "get_native_status", side_effect=[err, _succeeded_result()]
            ),
            fake_clock() as clock,
        ):
            result = client.poll_native_status(
                _RUN_ID,
                interval_seconds=10,
                timeout_seconds=600,
                max_transient_failures=5,
            )
        assert result.status == DAGRunStatus.SUCCEEDED
        assert clock.slept == [120]

    def test_hintless_error_keeps_the_poll_cadence(self):
        client = _make_client()
        with (
            patch.object(
                client._ae,
                "get_native_status",
                side_effect=[_http_error(), _succeeded_result()],
            ),
            fake_clock() as clock,
        ):
            client.poll_native_status(
                _RUN_ID,
                interval_seconds=10,
                timeout_seconds=600,
                max_transient_failures=5,
            )
        assert clock.slept == [10]

    def test_long_backoff_counts_against_the_poll_timeout(self):
        """Honouring a long wait must not let the loop outlive timeout_seconds."""
        client = _make_client()
        err = AtlanApiHttpError(
            message="native-status failed: HTTP 504",
            target="GET /api/service/package-workflows/native-status HTTP 504",
            retry_after_seconds=120,
        )
        with (
            patch.object(client._ae, "get_native_status", side_effect=[err] * 10),
            fake_clock() as clock,
            pytest.raises(AtlanApiTimeoutError),
        ):
            client.poll_native_status(
                _RUN_ID,
                interval_seconds=10,
                timeout_seconds=200,
                max_transient_failures=5,
            )
        # Each honoured sleep is clamped to the remaining timeout budget:
        # 120s requested against a 200s deadline sleeps 120s, then only 80s,
        # so the loop exits exactly at the deadline rather than overshooting.
        assert clock.slept == [120, 80]

    def test_honoured_wait_is_clamped_to_the_remaining_timeout(self):
        """timeout_seconds < retry_after: the first sleep stops at the deadline."""
        client = _make_client()
        err = AtlanApiHttpError(
            message="native-status failed: HTTP 504",
            target="GET /api/service/package-workflows/native-status HTTP 504",
            retry_after_seconds=120,
        )
        with (
            patch.object(client._ae, "get_native_status", side_effect=[err] * 10),
            fake_clock() as clock,
            pytest.raises(AtlanApiTimeoutError),
        ):
            client.poll_native_status(
                _RUN_ID,
                interval_seconds=10,
                timeout_seconds=50,
                max_transient_failures=5,
            )
        # 50s remain and the origin asked for 120s: one clamped 50s sleep
        # reaches the deadline exactly; the loop never sleeps past it.
        assert clock.slept == [50]

    def test_honoured_backoff_is_budgeted_across_the_poll_loop(self):
        """A tenant repeating retry_after: 120 exhausts the shared budget, then
        the loop falls back to the poll cadence — same accounting as
        _post_with_retry."""
        client = _make_client()
        err = AtlanApiHttpError(
            message="native-status failed: HTTP 504",
            target="GET /api/service/package-workflows/native-status HTTP 504",
            retry_after_seconds=120,
        )
        with (
            patch.object(client._ae, "get_native_status", side_effect=[err] * 10),
            fake_clock() as clock,
            pytest.raises(AtlanApiHttpError),
        ):
            client.poll_native_status(
                _RUN_ID,
                interval_seconds=10,
                timeout_seconds=10000,
                max_transient_failures=5,
            )
        # 110s honoured per wait against the 300s budget: two full 120s waits
        # (110 + 110 honoured), a third clamped to the 80s of budget left
        # (the budget caps the whole wait, not just the above-floor part, so
        # 110 + 110 + 80 = 300), then the fixed 10s cadence once the budget
        # is spent — no unbounded 120s waits. The fifth error hits the
        # transient-failure cap and re-raises.
        assert clock.slept == [
            120,
            120,
            80,
            10,
        ]


def _patch_transport(*results: httpx.Response | Exception):
    """Patch the one httpx call ``_request`` makes, per attempt.

    ``_request`` now issues through one pooled ``httpx.AsyncClient``, dropped
    and rebuilt after a transport error so a retry still gets a fresh
    connection; patching ``AsyncClient.request`` covers every attempt from one
    place either way. Pass one entry per expected attempt: a ``Response`` to
    answer with, or an exception to raise. A single entry is repeated for every
    attempt.
    """
    if len(results) == 1:
        only = results[0]
        # A lone Response goes through return_value, not side_effect: an
        # httpx.Response is itself iterable (byte streaming), so mock would
        # consume it as a sequence of per-attempt results.
        kwargs = (
            {"side_effect": only}
            if isinstance(only, Exception)
            else {"return_value": only}
        )
    else:
        kwargs = {"side_effect": list(results)}
    return patch.object(httpx.AsyncClient, "request", autospec=True, **kwargs)


def _response(status: int, raw: bytes) -> httpx.Response:
    return httpx.Response(status_code=status, content=raw)


class TestClassifyDelivery:
    """Only a connect-phase failure proves the origin never saw the request."""

    @pytest.mark.parametrize(
        "exc",
        [
            httpx.ConnectTimeout("handshake never completed"),
            httpx.ConnectError("name resolution failed"),
            httpx.PoolTimeout("no connection acquired"),
        ],
    )
    def test_connect_phase_is_not_delivered(self, exc):
        assert _classify_delivery(exc) is RequestDelivery.NOT_DELIVERED

    @pytest.mark.parametrize(
        "exc",
        [
            httpx.ReadTimeout("the read operation timed out"),
            httpx.WriteTimeout("write timed out"),
            httpx.RemoteProtocolError("server disconnected"),
            httpx.ReadError("connection reset"),
            OSError("something else entirely"),
        ],
    )
    def test_everything_after_the_handshake_is_ambiguous(self, exc):
        assert _classify_delivery(exc) is RequestDelivery.AMBIGUOUS


class TestRequestNetworkRetry:
    """_request: transient network errors retry; sustained ones surface as
    AtlanApiTimeoutError (an AppError) so the poll loop tolerates them instead
    of crashing on a raw transport error mid-poll."""

    def test_retries_transient_then_succeeds(self):
        """A transport error on attempt 1 → succeeds on attempt 2."""
        client = _make_client()
        with (
            _patch_transport(
                httpx.ConnectError("dns blip"), _response(200, b'{"ok": true}')
            ),
            patch(_SLEEP),
        ):
            status, body = _drive(client._ae._request("GET", "/native-status"))
        assert status == 200
        assert body == {"ok": True}

    def test_sustained_network_error_raises_atlan_timeout(self):
        """A transport error on every attempt → AtlanApiTimeoutError."""
        client = _make_client()
        with (
            _patch_transport(httpx.ConnectError("name resolution")) as mock_req,
            patch(_SLEEP),
            pytest.raises(AtlanApiTimeoutError),
        ):
            _drive(client._ae._request("GET", "/native-status"))
        assert mock_req.call_count == _REQUEST_MAX_ATTEMPTS

    def test_5xx_returns_immediately_without_retry(self):
        """A real 5xx HTTP response is returned, not retried as a network error."""
        client = _make_client()
        with (
            _patch_transport(_response(500, b'{"err": "boom"}')) as mock_req,
            patch(_SLEEP) as mock_sleep,
        ):
            status, body = _drive(client._ae._request("GET", "/native-status"))
        assert status == 500
        assert body == {"err": "boom"}
        assert mock_req.call_count == 1
        mock_sleep.assert_not_called()

    def test_non_json_body_returns_raw_text(self):
        """A 2xx that isn't JSON degrades to text rather than crashing."""
        client = _make_client()
        with _patch_transport(_response(200, b"<html>gateway</html>")):
            status, body = _drive(client._ae._request("GET", "/native-status"))
        assert status == 200
        assert body == "<html>gateway</html>"

    def test_error_carries_the_delivery_classification(self):
        """The raised error records whether the origin can have seen the
        request — _post_with_retry's re-POST decision reads this."""
        client = _make_client()
        with (
            _patch_transport(httpx.ReadTimeout("read timed out")),
            patch(_SLEEP),
            pytest.raises(AtlanApiTimeoutError) as excinfo,
        ):
            _drive(client._ae._request("GET", "/native-status"))
        assert excinfo.value.delivery is RequestDelivery.AMBIGUOUS

        with (
            _patch_transport(httpx.ConnectTimeout("handshake never completed")),
            patch(_SLEEP),
            pytest.raises(AtlanApiTimeoutError) as excinfo,
        ):
            _drive(client._ae._request("GET", "/native-status"))
        assert excinfo.value.delivery is RequestDelivery.NOT_DELIVERED


class TestCredentialConflictHelpers:
    """The credential-name conflict detector + name rotator (submit idempotency)."""

    def test_conflict_detected_in_dict_and_str_body(self):
        dup = {
            "code": 1002,
            "message": "ERROR #23505 duplicate key value violates unique "
            'constraint "credentials_name_key"',
        }
        assert _is_credential_name_conflict(400, dup) is True
        assert _is_credential_name_conflict(409, "…credentials_name_key…") is True

    def test_not_a_conflict(self):
        assert _is_credential_name_conflict(500, {"err": "overloaded"}) is False
        assert _is_credential_name_conflict(200, {"ok": True}) is False  # <400
        assert _is_credential_name_conflict(400, {"message": "other"}) is False

    def test_rotate_appends_and_increments_retry_suffix(self):
        payload = {
            "payload": [{"type": "credential", "body": {"name": "default-pg-42-1"}}]
        }
        _rotate_submit_credential_name(payload)
        assert payload["payload"][0]["body"]["name"] == "default-pg-42-1-retry1"
        _rotate_submit_credential_name(payload)
        assert payload["payload"][0]["body"]["name"] == "default-pg-42-1-retry2"

    def test_rotate_is_noop_without_credential(self):
        # public-source submit — no `payload` credential block
        p = {"metadata": {"name": "x"}, "spec": {}}
        _rotate_submit_credential_name(p)
        assert p == {"metadata": {"name": "x"}, "spec": {}}
        _rotate_submit_credential_name(None)  # must not raise


class TestSubmitWorkflowCredentialRetry:
    """submit_workflow recovers from AE's non-idempotent credential create."""

    @staticmethod
    def _payload_with_cred() -> dict:
        return {
            "metadata": {"name": "atlan-postgres-1234"},
            "spec": {
                "templates": [{"dag": {"tasks": [{"arguments": {"parameters": []}}]}}]
            },
            "payload": [
                {
                    "parameter": "credentialGuid",
                    "type": "credential",
                    "body": {"name": "default-postgres-1234-1", "authType": "basic"},
                }
            ],
        }

    def test_credentials_name_key_400_is_retried_with_rotated_name(self):
        """400 credentials_name_key on attempt 1 → rotate name → succeed on retry."""
        client = _make_client()
        dup = (
            400,
            {
                "code": 1002,
                "message": "duplicate key value violates "
                'unique constraint "credentials_name_key"',
            },
        )
        ok = (200, {"data": {"run_id": "run-xyz"}})
        payload = self._payload_with_cred()
        with (
            patch.object(client._ae, "_request", side_effect=[dup, ok]),
            patch(_SLEEP),
        ):
            run_id = client.submit_workflow(payload, retries=4, retry_sleep_seconds=0)
        assert run_id == "run-xyz"
        # the credential name was rotated before the successful retry
        assert payload["payload"][0]["body"]["name"] == "default-postgres-1234-1-retry1"

    def test_5xx_then_success_also_rotates_credential(self):
        """A transient 5xx (which AE may have committed) rotates before retry."""
        client = _make_client()
        payload = self._payload_with_cred()
        with (
            patch.object(
                client._ae,
                "_request",
                side_effect=[(503, {"err": "overloaded"}), (200, {"run_id": "r2"})],
            ),
            patch(_SLEEP),
        ):
            run_id = client.submit_workflow(payload, retries=4, retry_sleep_seconds=0)
        assert run_id == "r2"
        assert payload["payload"][0]["body"]["name"] == "default-postgres-1234-1-retry1"

    def test_persistent_conflict_eventually_raises(self):
        """All attempts conflict → still fails loudly (no silent pass)."""
        client = _make_client()
        dup = (400, {"message": 'unique constraint "credentials_name_key"'})
        with (
            patch.object(client._ae, "_request", side_effect=[dup, dup]),
            patch(_SLEEP),
        ):
            with pytest.raises(AtlanApiHttpError):
                client.submit_workflow(
                    self._payload_with_cred(), retries=1, retry_sleep_seconds=0
                )


# Heracles echoes a refused dial to the tenant app pod back as an HTTP 500
# carrying the Go net error verbatim (FND-402).
_REFUSED_BODY = {
    "error": "dial tcp 10.0.0.5:8000: connect: connection refused",
}


class TestAppNotReadyDetection:
    """_is_app_not_ready: the tenant-app cold-start shape, and only that shape."""

    def test_refused_dial_detected_in_dict_and_str_body(self):
        assert _is_app_not_ready(500, _REFUSED_BODY) is True
        assert (
            _is_app_not_ready(
                502, "… dial tcp 10.0.0.5:8000: connect: Connection Refused"
            )
            is True
        )

    def test_bare_connection_refused_substring_is_not_app_not_ready(self):
        # The match requires the refused-dial-to-:8000 sequence, not the bare
        # substring: a genuine terminal 5xx whose body merely mentions a refused
        # connection (e.g. an upstream DB dial surfaced through Heracles) must
        # not be mis-named AppNotReadyError.
        assert _is_app_not_ready(500, "… connect: Connection Refused") is False
        assert (
            _is_app_not_ready(
                500, {"err": "upstream db dial: connection refused on :5432"}
            )
            is False
        )

    def test_other_submit_failures_are_not_app_not_ready(self):
        # A genuine AE 5xx, a 4xx, the already-active conflict, and any 2xx must
        # not read as a cold start — each has its own terminal handling.
        assert _is_app_not_ready(500, {"err": "internal error"}) is False
        assert _is_app_not_ready(400, {"message": "bad payload"}) is False
        assert _is_app_not_ready(500, _MASKED_409_BODY) is False
        assert _is_app_not_ready(200, {"run_id": "r"}) is False

    def test_4xx_carrying_the_markers_is_not_app_not_ready(self):
        # The status gate is 5xx-only, so a request-side rejection AE decided
        # without dialling the pod stays a terminal AtlanApiHttpError even when
        # its body happens to carry all three markers (e.g. an echoed-back
        # payload). Heracles reports the real refused dial as a 500.
        assert _is_app_not_ready(400, _REFUSED_BODY) is False
        assert _is_app_not_ready(404, _REFUSED_BODY) is False
        assert _is_app_not_ready(499, _REFUSED_BODY) is False
        assert _is_app_not_ready(500, _REFUSED_BODY) is True


class TestSubmitWorkflowAppColdStart:
    """A cold-starting tenant app pod: retried by the existing loop, then named."""

    def test_refused_dial_is_retried_by_the_existing_5xx_retry(self):
        """No second loop needed — a refused dial IS a retryable 5xx."""
        client = _make_client()
        refused = (500, _REFUSED_BODY)
        with (
            patch.object(
                client._ae,
                "_request",
                side_effect=[refused, refused, (200, {"data": {"run_id": "run-warm"}})],
            ),
            patch(_SLEEP),
        ):
            run_id = client.submit_workflow(
                {"metadata": {"name": "wf"}}, retries=60, retry_sleep_seconds=0
            )
        assert run_id == "run-warm"

    def test_budget_exhausted_raises_app_not_ready_with_typed_fields(self):
        """Terminal cold start is named, not surfaced as an opaque 500."""
        client = _make_client()
        refused = (500, _REFUSED_BODY)
        with (
            patch.object(client._ae, "_request", side_effect=[refused] * 3),
            patch(_SLEEP),
        ):
            with pytest.raises(AppNotReadyError) as excinfo:
                client.submit_workflow(
                    {"metadata": {"name": "wf"}}, retries=2, retry_sleep_seconds=0
                )
        err = excinfo.value
        # attempts/elapsed_seconds are queryable fields, not just prose — same
        # shape as DaprSidecarUnreachableError.
        assert err.attempts == 3
        assert err.elapsed_seconds is not None
        assert "never accepted connections on :8000" in err.message

    def test_genuine_5xx_still_raises_atlan_api_http_error(self):
        """The cold-start naming never swallows a real AE failure."""
        client = _make_client()
        with (
            patch.object(
                client._ae, "_request", side_effect=[(500, {"err": "boom"})] * 2
            ),
            patch(_SLEEP),
        ):
            with pytest.raises(AtlanApiHttpError):
                client.submit_workflow(
                    {"metadata": {"name": "wf"}}, retries=1, retry_sleep_seconds=0
                )


# The exact body observed in prod: AE's 409 "already active" masked as a 500 by
# Heracles (the AE proxy; see application-sdk#2657 openapi e2e leg).
_MASKED_409_BODY = {
    "code": 500,
    "error": "Internal Server Error",
    "message": (
        "ae: SubmitWorkflow returned HTTP 409: "
        '{"code":"AE-WF-409-03","message":"A run for workflow '
        "'openapi-e2e-full-ci-1' is already active\"}"
    ),
    "requestId": "oobbCDVUvlvIix5Sh9koKRCQASOtqSbf",
}


class TestSubmitWorkflowIdempotency:
    """A submit is not idempotent: a blind retry after AE already accepted one
    spawns a duplicate run AE marks Skipped, which the harness then mistracks.
    submit_workflow must (a) never re-POST on a network timeout and (b) treat
    the 'already active' conflict — even masked as a 500 — as terminal."""

    def test_is_already_active_detects_masked_500(self):
        assert _is_already_active_run(500, _MASKED_409_BODY) is True

    def test_is_already_active_detects_bare_409(self):
        assert _is_already_active_run(409, {"code": "AE-WF-409-03"}) is True
        # matched by code even in free-form text, case-insensitively
        assert _is_already_active_run(409, "conflict: ae-wf-409-03") is True

    def test_is_already_active_ignores_plain_5xx_and_success(self):
        assert _is_already_active_run(500, {"error": "Internal Server Error"}) is False
        assert _is_already_active_run(503, {"err": "overloaded"}) is False
        # the generic "already active" phrase WITHOUT the AE-WF-409-03 code must
        # NOT match — a transient 5xx that happens to mention it stays retryable
        assert _is_already_active_run(503, "broker already active on node 2") is False
        # never match a 2xx body even if it carried the code
        assert _is_already_active_run(200, {"code": "AE-WF-409-03"}) is False

    def test_masked_409_raises_and_does_not_retry(self):
        """The masked-500 conflict must raise AtlanAEWorkflowAlreadyActiveError
        on the first response — not be retried as a transient 5xx."""
        client = _make_client()
        with (
            patch.object(
                client._ae, "_request", return_value=(500, _MASKED_409_BODY)
            ) as mock_req,
            patch(_SLEEP) as mock_sleep,
            pytest.raises(AtlanAEWorkflowAlreadyActiveError),
        ):
            client.submit_workflow({"any": "payload"})
        assert mock_req.call_count == 1
        mock_sleep.assert_not_called()

    def test_bare_409_raises_and_does_not_retry(self):
        """An unmasked 409 carrying AE-WF-409-03 must also raise
        AtlanAEWorkflowAlreadyActiveError without retrying — submit's retryable
        predicate only covers 5xx, and this conflict is terminal regardless."""
        client = _make_client()
        bare_409 = (409, {"code": "AE-WF-409-03", "message": "already active"})
        with (
            patch.object(client._ae, "_request", return_value=bare_409) as mock_req,
            patch(_SLEEP) as mock_sleep,
            pytest.raises(AtlanAEWorkflowAlreadyActiveError),
        ):
            client.submit_workflow({"any": "payload"})
        assert mock_req.call_count == 1
        mock_sleep.assert_not_called()

    def test_ambiguous_network_timeout_is_not_reposted(self):
        """A read-timeout on submit is ambiguous (AE may have accepted it), so
        submit_workflow must surface it without re-POSTing — reporting the one
        attempt actually made, not total_attempts, and naming why it stopped."""
        client = _make_client()
        ambiguous = AtlanApiTimeoutError(
            message="read timed out",
            operation="/submit",
            delivery=RequestDelivery.AMBIGUOUS,
        )
        with (
            patch.object(client._ae, "_request", side_effect=ambiguous) as mock_req,
            patch(_SLEEP),
            pytest.raises(AtlanApiTimeoutError, match=r"after 1 attempt") as excinfo,
        ):
            client.submit_workflow({"any": "payload"})
        assert mock_req.call_count == 1
        assert "not re-issued" in str(excinfo.value)

    def test_unclassified_timeout_is_treated_as_ambiguous(self):
        """A bare TimeoutError carries no delivery classification, so the
        conservative default applies and submit still refuses to re-POST."""
        client = _make_client()
        with (
            patch.object(
                client._ae, "_request", side_effect=TimeoutError("read timed out")
            ) as mock_req,
            patch(_SLEEP),
            pytest.raises(AtlanApiTimeoutError, match=r"after 1 attempt"),
        ):
            client.submit_workflow({"any": "payload"})
        assert mock_req.call_count == 1

    def test_connect_failure_is_reposted_within_the_budget(self):
        """A connect-phase failure never reached AE, so submit re-POSTs it
        despite being non-idempotent — and recovers when the next one lands.
        This is the blackholed-CI-hairpin case that used to red a whole leg."""
        client = _make_client()
        never_sent = AtlanApiTimeoutError(
            message="handshake never completed",
            operation="/submit",
            delivery=RequestDelivery.NOT_DELIVERED,
        )
        with (
            patch.object(
                client._ae,
                "_request",
                side_effect=[never_sent, (200, {"run_id": "r-2"})],
            ) as mock_req,
            patch(_SLEEP),
        ):
            assert client.submit_workflow({"any": "payload"}) == "r-2"
        assert mock_req.call_count == 2

    def test_sustained_connect_failure_exhausts_the_budget_then_raises(self):
        """Re-POSTing a never-delivered submit is bounded by the caller's
        attempt budget, not unbounded — and the raise reports the real count."""
        client = _make_client()
        never_sent = AtlanApiTimeoutError(
            message="handshake never completed",
            operation="/submit",
            delivery=RequestDelivery.NOT_DELIVERED,
        )
        with (
            patch.object(client._ae, "_request", side_effect=never_sent) as mock_req,
            patch(_SLEEP),
            pytest.raises(AtlanApiTimeoutError, match=r"after 3 attempt") as excinfo,
        ):
            client.submit_workflow({"any": "payload"}, retries=2)
        assert mock_req.call_count == 3
        # It exhausted the budget rather than declining to re-issue.
        assert "not re-issued" not in str(excinfo.value)

    def test_request_no_repost_on_timeout_when_disabled(self):
        """_request(retry_network_errors=False) issues exactly one POST on a
        transport error instead of the usual _REQUEST_MAX_ATTEMPTS — the
        re-POST decision belongs to _post_with_retry, not to a nested loop."""
        client = _make_client()
        with (
            _patch_transport(httpx.ReadTimeout("read timed out")) as mock_req,
            patch(_SLEEP),
            pytest.raises(AtlanApiTimeoutError),
        ):
            _drive(
                client._ae._request(
                    "POST", "/submit", body={}, retry_network_errors=False
                )
            )
        assert mock_req.call_count == 1

    def test_request_no_repost_even_when_never_delivered(self):
        """Same for a connect failure: _request still makes exactly one call.
        Re-POSTing it is safe, but doing so HERE would nest inside
        _post_with_retry's loop and blow the cold-start attempt budget."""
        client = _make_client()
        with (
            _patch_transport(httpx.ConnectTimeout("no handshake")) as mock_req,
            patch(_SLEEP),
            pytest.raises(AtlanApiTimeoutError) as excinfo,
        ):
            _drive(
                client._ae._request(
                    "POST", "/submit", body={}, retry_network_errors=False
                )
            )
        assert mock_req.call_count == 1
        assert excinfo.value.delivery is RequestDelivery.NOT_DELIVERED

    def test_genuine_5xx_still_retries_then_succeeds(self):
        """A real 5xx that is NOT the already-active conflict remains retryable,
        so a transient AE blip still recovers."""
        client = _make_client()
        with (
            patch.object(
                client._ae,
                "_request",
                side_effect=[(503, {"err": "overloaded"}), (200, {"run_id": "r-ok"})],
            ) as mock_req,
            patch(_SLEEP),
        ):
            run_id = client.submit_workflow({"any": "payload"})
        assert run_id == "r-ok"
        assert mock_req.call_count == 2

    def test_happy_path_returns_run_id(self):
        client = _make_client()
        with patch.object(
            client._ae, "_request", return_value=(200, {"run_id": "r-1"})
        ):
            assert client.submit_workflow({"any": "payload"}) == "r-1"


class TestParseRunTimestamp:
    """created_at decides adopt-vs-resubmit, so an unrecognised shape must read
    as unknown rather than as a plausible-looking instant."""

    def test_iso_with_offset(self):
        parsed = _parse_run_timestamp("2026-08-20T13:58:39.123456+00:00")
        assert parsed == datetime(2026, 8, 20, 13, 58, 39, 123456, tzinfo=UTC)

    def test_iso_with_zulu_suffix(self):
        parsed = _parse_run_timestamp("2026-08-20T13:58:39Z")
        assert parsed == datetime(2026, 8, 20, 13, 58, 39, tzinfo=UTC)

    def test_naive_iso_is_read_as_utc(self):
        """Both AE backends stamp UTC; a naive value is not local time."""
        parsed = _parse_run_timestamp("2026-08-20T13:58:39")
        assert parsed == datetime(2026, 8, 20, 13, 58, 39, tzinfo=UTC)

    def test_epoch_milliseconds(self):
        """The Atlan-mode registry derives created_at from a metastore entity
        create time, which is epoch milliseconds."""
        assert _parse_run_timestamp(1787234319000) == datetime(
            2026, 8, 20, 13, 58, 39, tzinfo=UTC
        )

    def test_epoch_seconds(self):
        assert _parse_run_timestamp(1787234319) == datetime(
            2026, 8, 20, 13, 58, 39, tzinfo=UTC
        )

    @pytest.mark.parametrize(
        "raw", [None, "", "not-a-date", True, False, {"nested": 1}, []]
    )
    def test_unrecognised_shapes_are_unknown_not_guessed(self, raw):
        assert _parse_run_timestamp(raw) is None


class TestNewestRunSince:
    """Picking our run out of AE's BulkResponse[WorkflowRun]."""

    _FLOOR = datetime(2026, 8, 20, 13, 0, 0, tzinfo=UTC)

    def test_returns_the_first_run_at_or_after_the_floor(self):
        body = {
            "data": [
                {"guid": "newer", "created_at": "2026-08-20T14:00:00Z"},
                {"guid": "older", "created_at": "2026-08-20T12:00:00Z"},
            ]
        }
        assert _newest_run_since(body, self._FLOOR) == "newer"

    def test_ignores_runs_created_before_the_floor(self):
        body = {"data": [{"guid": "older", "created_at": "2026-08-20T12:00:00Z"}]}
        assert _newest_run_since(body, self._FLOOR) is None

    def test_floor_is_inclusive(self):
        body = {"data": [{"guid": "exact", "created_at": "2026-08-20T13:00:00Z"}]}
        assert _newest_run_since(body, self._FLOOR) == "exact"

    def test_skips_rows_whose_timestamp_cannot_be_parsed(self):
        """An unparseable row must not be adopted on optimism — the harness
        would then poll a run it never submitted."""
        body = {
            "data": [
                {"guid": "unparseable", "created_at": "???"},
                {"guid": "good", "created_at": "2026-08-20T14:00:00Z"},
            ]
        }
        assert _newest_run_since(body, self._FLOOR) == "good"

    def test_accepts_run_id_as_well_as_guid(self):
        body = {"data": [{"run_id": "r-1", "created_at": "2026-08-20T14:00:00Z"}]}
        assert _newest_run_since(body, self._FLOOR) == "r-1"

    @pytest.mark.parametrize(
        "body",
        [
            {},
            {"data": None},
            {"data": []},
            {"data": "not a list"},
            {"data": [None, 7, "x"]},
            {"data": [{"created_at": "2026-08-20T14:00:00Z"}]},  # no id
            {"data": [{"guid": "", "created_at": "2026-08-20T14:00:00Z"}]},
        ],
    )
    def test_malformed_bodies_yield_nothing(self, body):
        assert _newest_run_since(body, self._FLOOR) is None


class TestFindRunCreatedSince:
    """The reconcile read: found / proven-absent / never-answered."""

    _SINCE = datetime(2026, 8, 20, 13, 58, 45, tzinfo=UTC)

    def test_found_run_is_conclusive(self):
        client = _make_client()
        body = {"data": [{"guid": "r-real", "created_at": "2026-08-20T13:58:46Z"}]}
        with patch.object(client._ae, "_request", return_value=(200, body)):
            lookup = client.find_run_created_since("slug-x", self._SINCE)
        assert lookup == RunLookup(run_id="r-real", conclusive=True)

    def test_queries_ae_under_the_slug(self):
        client = _make_client()
        with patch.object(
            client._ae, "_request", return_value=(200, {"data": []})
        ) as mock_req:
            client.find_run_created_since(
                "slug/with space", self._SINCE, timeout_seconds=0
            )
        _, path = mock_req.call_args[0]
        assert path.startswith("/automation/api/v1/runs?")
        assert "workflow_slug=slug%2Fwith%20space" in path
        # Widened so AE's own default filters cannot hide our run.
        assert "include_test_runs=true" in path
        assert "include_system=true" in path

    def test_empty_answer_is_proof_of_absence(self):
        client = _make_client()
        with patch.object(client._ae, "_request", return_value=(200, {"data": []})):
            lookup = client.find_run_created_since(
                "slug-x", self._SINCE, timeout_seconds=0
            )
        assert lookup == RunLookup(run_id=None, conclusive=True)

    def test_unanswered_read_proves_nothing(self):
        """If the fault that killed the submit also kills every reconcile read,
        the answer must stay unknown — never 'absent', which would authorise
        the duplicate submit this whole mechanism exists to prevent."""
        client = _make_client()
        with patch.object(
            client._ae,
            "_request",
            side_effect=AtlanApiTimeoutError(message="blackholed"),
        ):
            lookup = client.find_run_created_since(
                "slug-x", self._SINCE, timeout_seconds=0
            )
        assert lookup == RunLookup(run_id=None, conclusive=False)

    def test_non_2xx_read_proves_nothing(self):
        client = _make_client()
        with patch.object(client._ae, "_request", return_value=(503, {"err": "down"})):
            lookup = client.find_run_created_since(
                "slug-x", self._SINCE, timeout_seconds=0
            )
        assert lookup.conclusive is False

    def test_polls_until_the_run_becomes_searchable(self):
        """AE serves this list from Elasticsearch, so a run committed just
        before the connection died can take a moment to appear. One empty
        answer is not the end of the search.

        Under ``fake_clock`` rather than ``patch(_SLEEP)``: this loop is a
        *deadline* loop, so it sleeps through ``_poll``'s swappable default and
        never touches ``sleep_async``. Patching the retry seam here left the two
        ``interval_seconds`` gaps running on the real clock — twenty seconds of
        the unit suite spent proving nothing the fake does not prove. See
        FND-962.
        """
        client = _make_client()
        late = {"data": [{"guid": "r-late", "created_at": "2026-08-20T13:58:46Z"}]}
        with (
            patch.object(
                client._ae,
                "_request",
                side_effect=[(200, {"data": []}), (200, {"data": []}), (200, late)],
            ) as mock_req,
            fake_clock() as clock,
        ):
            lookup = client.find_run_created_since(
                "slug-x", self._SINCE, timeout_seconds=60, interval_seconds=10
            )
        assert lookup.run_id == "r-late"
        assert mock_req.call_count == 3
        # The cadence itself, which the inert patch could not see: two full
        # intervals between the three reads.
        assert clock.slept == [10, 10]

    def test_clock_skew_window_admits_a_slightly_earlier_run(self):
        """The runner's clock and the tenant's are compared directly, so a run
        AE stamped just before our own 'now' is still ours."""
        client = _make_client()
        skewed = self._SINCE - timedelta(seconds=_RECONCILE_CLOCK_SKEW_SECONDS // 2)
        body = {"data": [{"guid": "r-skewed", "created_at": skewed.isoformat()}]}
        with patch.object(client._ae, "_request", return_value=(200, body)):
            lookup = client.find_run_created_since("slug-x", self._SINCE)
        assert lookup.run_id == "r-skewed"

    def test_run_older_than_the_skew_window_is_not_adopted(self):
        client = _make_client()
        stale = self._SINCE - timedelta(seconds=_RECONCILE_CLOCK_SKEW_SECONDS + 60)
        body = {"data": [{"guid": "r-stale", "created_at": stale.isoformat()}]}
        with patch.object(client._ae, "_request", return_value=(200, body)):
            lookup = client.find_run_created_since(
                "slug-x", self._SINCE, timeout_seconds=0
            )
        assert lookup == RunLookup(run_id=None, conclusive=True)


class TestProbeRunIsListed:
    """The success-path probe that settles _RESUBMIT_WHEN_AE_REPORTS_NO_RUN.

    Its whole value is that a wrong answer is worse than no answer, so
    'listed', 'answered but absent' and 'never answered' stay three outcomes.
    """

    def test_listed_run_returns_true(self):
        body = {"data": [{"guid": "r-1"}, {"guid": "r-0"}]}
        client = _make_client()
        with patch.object(client._ae, "_request", return_value=(200, body)):
            assert client.probe_run_is_listed("slug-x", "r-1") is True

    def test_answered_without_the_run_returns_false(self):
        client = _make_client()
        with patch.object(client._ae, "_request", return_value=(200, {"data": []})):
            assert client.probe_run_is_listed("slug-x", "r-1") is False

    def test_unanswered_read_returns_none_not_false(self):
        """A read that never got through settles nothing — reporting it as
        'absent' would be recorded as evidence for flipping the gate."""
        client = _make_client()
        with patch.object(
            client._ae,
            "_request",
            side_effect=AtlanApiTimeoutError(message="blackholed"),
        ):
            assert client.probe_run_is_listed("slug-x", "r-1") is None

    def test_non_2xx_returns_none(self):
        client = _make_client()
        with patch.object(client._ae, "_request", return_value=(503, {"err": "down"})):
            assert client.probe_run_is_listed("slug-x", "r-1") is None

    def test_never_raises_into_the_leg(self):
        """Outcome-neutral: a broken probe must not fail a passing e2e run."""
        client = _make_client()
        with patch.object(client._ae, "_request", return_value=(200, {"data": "junk"})):
            assert client.probe_run_is_listed("slug-x", "r-1") is False


class TestSubmitReconciliation:
    """submit_workflow resolves an ambiguous timeout by asking AE what happened
    rather than guessing from the transport error."""

    @staticmethod
    def _ambiguous() -> AtlanApiTimeoutError:
        return AtlanApiTimeoutError(
            message="read timed out",
            operation="/submit",
            delivery=RequestDelivery.AMBIGUOUS,
        )

    def test_adopts_the_run_ae_already_created(self):
        """The submit landed and only the response was lost: the DAG is really
        running, so return its id instead of failing the leg."""
        client = _make_client()
        with (
            patch.object(client._ae, "_request", side_effect=self._ambiguous()),
            patch.object(
                client._ae,
                "find_run_created_since",
                return_value=RunLookup(run_id="r-adopted", conclusive=True),
            ) as mock_find,
            patch(_SLEEP),
        ):
            assert client.submit_workflow({"any": "p"}, slug="slug-x") == "r-adopted"
        assert mock_find.call_args[0][0] == "slug-x"

    def test_does_not_resubmit_after_adopting(self):
        client = _make_client()
        with (
            patch.object(
                client._ae, "_request", side_effect=self._ambiguous()
            ) as mock_req,
            patch.object(
                client._ae,
                "find_run_created_since",
                return_value=RunLookup(run_id="r-adopted", conclusive=True),
            ),
            patch(_SLEEP),
        ):
            client.submit_workflow({"any": "p"}, slug="slug-x", retries=4)
        assert mock_req.call_count == 1

    def test_resubmits_when_ae_proves_nothing_landed(self):
        """Enabled form of the absence half. Gated off by default until a live
        leg confirms Heracles-submitted runs show up in AE's listing, so the
        gate is patched on here rather than the test asserting today's default."""
        client = _make_client()
        with (
            patch.object(
                client._ae,
                "_request",
                side_effect=[self._ambiguous(), (200, {"run_id": "r-2"})],
            ) as mock_req,
            patch.object(
                client._ae,
                "find_run_created_since",
                return_value=RunLookup(run_id=None, conclusive=True),
            ),
            patch(
                "application_sdk.testing.harness.automation_engine.client."
                "_RESUBMIT_WHEN_AE_REPORTS_NO_RUN",
                True,
            ),
            patch(_SLEEP),
        ):
            assert client.submit_workflow({"any": "p"}, slug="slug-x") == "r-2"
        assert mock_req.call_count == 2

    def test_absence_does_not_resubmit_while_the_gate_is_off(self):
        """Default posture: an unrecovered ambiguous submit fails exactly as it
        did before reconciliation existed. Adopting is the only enabled half."""
        client = _make_client()
        with (
            patch.object(
                client._ae, "_request", side_effect=self._ambiguous()
            ) as mock_req,
            patch.object(
                client._ae,
                "find_run_created_since",
                return_value=RunLookup(run_id=None, conclusive=True),
            ),
            patch(_SLEEP),
            pytest.raises(AtlanApiTimeoutError, match=r"after 1 attempt"),
        ):
            client.submit_workflow({"any": "p"}, slug="slug-x")
        assert mock_req.call_count == 1

    def test_inconclusive_reconcile_still_fails_fast(self):
        """Unknown is not absent. With no proof either way the old never-repost
        behaviour stands, rather than risking a duplicate run."""
        client = _make_client()
        with (
            patch.object(
                client._ae, "_request", side_effect=self._ambiguous()
            ) as mock_req,
            patch.object(
                client._ae,
                "find_run_created_since",
                return_value=RunLookup(run_id=None, conclusive=False),
            ),
            patch(_SLEEP),
            pytest.raises(AtlanApiTimeoutError, match=r"after 1 attempt") as excinfo,
        ):
            client.submit_workflow({"any": "p"}, slug="slug-x")
        assert mock_req.call_count == 1
        assert "not re-issued" in str(excinfo.value)

    def test_no_slug_means_no_reconcile(self):
        """A caller that cannot name the slug keeps the pre-existing fail-fast
        contract — the guard degrades, it does not guess."""
        client = _make_client()
        with (
            patch.object(
                client._ae, "_request", side_effect=self._ambiguous()
            ) as mock_req,
            patch.object(client._ae, "find_run_created_since") as mock_find,
            patch(_SLEEP),
            pytest.raises(AtlanApiTimeoutError),
        ):
            client.submit_workflow({"any": "p"})
        assert mock_req.call_count == 1
        mock_find.assert_not_called()

    def test_connect_failure_skips_the_reconcile_read(self):
        """A never-delivered request needs no proof: re-POST straight away
        instead of spending the reconcile window on a settled question."""
        client = _make_client()
        never_sent = AtlanApiTimeoutError(
            message="handshake never completed",
            operation="/submit",
            delivery=RequestDelivery.NOT_DELIVERED,
        )
        with (
            patch.object(
                client._ae,
                "_request",
                side_effect=[never_sent, (200, {"run_id": "r-3"})],
            ),
            patch.object(client._ae, "find_run_created_since") as mock_find,
            patch(_SLEEP),
        ):
            assert client.submit_workflow({"any": "p"}, slug="slug-x") == "r-3"
        mock_find.assert_not_called()


class TestPollNativeStatusCeilingStamp:
    """A poll that ends on its own ceiling must say so on the result it returns.

    Returning the last observation bare made every caller read the last-seen
    node states as a verdict, so a node that was never dispatched surfaced as a
    node failure with ``error=None`` (FND-708).
    """

    def test_ceiling_stamps_the_timeout_and_the_stall_window(self):
        """Nothing ever moves → the returned observation carries the ceiling and
        the full no-progress window."""
        client = _make_client()
        stuck = _result(DAGRunStatus.RUNNING, DAGNodeStatus.PENDING)

        with patch.object(client._ae, "get_native_status", return_value=stuck):
            with fake_clock():
                result = client.poll_native_status(
                    _RUN_ID,
                    interval_seconds=10,
                    timeout_seconds=30,
                    stall_grace_seconds=None,
                    progress_stall_seconds=None,
                )
        assert result.timed_out is True
        assert result.timed_out_after_seconds == 30.0
        assert result.seconds_since_last_progress is not None
        assert 0.0 <= result.seconds_since_last_progress <= 30.0

    def test_stall_window_is_measured_from_the_last_transition(self):
        """A DAG that progresses and *then* freezes reports the frozen window,
        not the whole poll — that is the number that names the wedge."""
        client = _make_client()
        pending = _result(DAGRunStatus.RUNNING, DAGNodeStatus.PENDING)
        running = _result(DAGRunStatus.RUNNING, DAGNodeStatus.RUNNING)

        with patch.object(
            client._ae, "get_native_status", side_effect=[pending] + [running] * 20
        ):
            with fake_clock():
                result = client.poll_native_status(
                    _RUN_ID,
                    interval_seconds=10,
                    timeout_seconds=60,
                    stall_grace_seconds=None,
                    progress_stall_seconds=None,
                )
        assert result.timed_out_after_seconds == 60.0
        # The transition happened on the second poll, so the frozen window is
        # strictly shorter than the poll itself.
        assert result.seconds_since_last_progress is not None
        assert 0.0 < result.seconds_since_last_progress < 60.0

    def test_terminal_result_is_not_stamped(self):
        """A run that finished is a verdict, not a ceiling — no stamp."""
        client = _make_client()

        with patch.object(
            client._ae, "get_native_status", return_value=_succeeded_result()
        ):
            with fake_clock():
                result = client.poll_native_status(
                    _RUN_ID, interval_seconds=10, timeout_seconds=600
                )
        assert result.timed_out is False
        assert result.timed_out_after_seconds is None
        assert result.seconds_since_last_progress is None


class TestNotStartedNodes:
    """Pending/Scheduled is a status, not a dispatch fact — and not a failure.

    AE holds a node at Pending whether nothing picked it up or its child
    workflow is running, so the set says the node has not *failed*; it does not
    say the node never started.
    """

    @pytest.mark.parametrize("status", [DAGNodeStatus.PENDING, DAGNodeStatus.SCHEDULED])
    def test_not_started_statuses(self, status: DAGNodeStatus):
        assert status.is_not_started is True

    @pytest.mark.parametrize(
        "status",
        [
            DAGNodeStatus.RUNNING,
            DAGNodeStatus.SUCCEEDED,
            DAGNodeStatus.FAILED,
            DAGNodeStatus.SKIPPED,
        ],
    )
    def test_started_or_finished_statuses(self, status: DAGNodeStatus):
        assert status.is_not_started is False

    def test_not_started_nodes_excludes_a_real_failure(self):
        """``failed_nodes`` stays the wide not-successful set; the new accessor
        isolates the never-dispatched ones so a message can tell them apart."""
        result = DAGRunResult(
            run_id=_RUN_ID,
            workflow_slug="slug",
            status=DAGRunStatus.RUNNING,
            nodes=[
                DAGNodeResult(
                    name="publish",
                    status=DAGNodeStatus.FAILED,
                    started_at_ms=None,
                    completed_at_ms=None,
                    error_message="boom",
                ),
                DAGNodeResult(
                    name="lineage-publish",
                    status=DAGNodeStatus.PENDING,
                    started_at_ms=None,
                    completed_at_ms=None,
                    error_message=None,
                ),
            ],
        )
        assert [n.name for n in result.failed_nodes] == ["publish", "lineage-publish"]
        assert [n.name for n in result.not_started_nodes] == ["lineage-publish"]
