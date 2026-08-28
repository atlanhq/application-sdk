"""The AE shape ``poll_native_status`` hands the primitive, and the verdict back.

``test_waiting_equivalence.py`` pins what the *whole* loop does, script in and
verdict out, against the numbers the hand-rolled loop produced. That is the
behaviour-preservation evidence, and it exercises only the states a scripted run
can reach.

This file covers the seams that survive underneath it: the three ways a caller
spells "guard off", the classifier's rounding, the progress ledger the ``L006``
per-poll line moved into, and — the reason the file exists — the branches of
:func:`_dag_run_verdict` where the verdict carries **no reading**. Those are
unreachable through the loop today, and that is exactly why they are asserted
directly: an ``outcome.last`` guard whose ``else`` nothing exercises is a guard
nobody has checked, and the first thing it would do on the day a verdict does
arrive empty is raise ``AttributeError`` from inside a diagnostic.
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

import pytest

from application_sdk.errors.base import AppError
from application_sdk.testing.harness._poll import fake_clock, monotonic
from application_sdk.testing.harness.automation_engine import client as _client_module
from application_sdk.testing.harness.automation_engine._errors import (
    AtlanApiHttpError,
    AtlanApiTimeoutError,
    AutomationEngineNotDispatchingError,
    DAGProgressStalledError,
    NoWorkerOnTaskQueueError,
)
from application_sdk.testing.harness.automation_engine.client import (
    _absorb_ae_blip,
    _any_node_started,
    _armed,
    _dag_run_verdict,
    _DAGProgress,
)
from application_sdk.testing.harness.automation_engine.wire import (
    DAGNodeResult,
    DAGNodeStatus,
    DAGRunResult,
    DAGRunStatus,
)
from application_sdk.testing.harness.outcome import (
    Expired,
    Indeterminate,
    NeverStarted,
    Settled,
    Stalled,
)

_RUN_ID = "run-1"


def _result(
    run_status: DAGRunStatus = DAGRunStatus.RUNNING,
    *node_statuses: DAGNodeStatus,
) -> DAGRunResult:
    return DAGRunResult(
        run_id=_RUN_ID,
        workflow_slug="slug",
        status=run_status,
        nodes=[
            DAGNodeResult(
                name=f"node{index}",
                status=status,
                started_at_ms=None,
                completed_at_ms=None,
                error_message=None,
            )
            for index, status in enumerate(node_statuses)
        ],
    )


def _verdict(outcome, **overrides):  # type: ignore[no-untyped-def]
    """Call ``_dag_run_verdict`` with the AE_RUN-shaped defaults."""
    kwargs = {
        "run_id": _RUN_ID,
        "progress": _DAGProgress(),
        "stall_grace_seconds": 180,
        "stall_task_queue": "",
        "progress_stall_seconds": 300,
        "timeout_seconds": 600,
        "max_transient_failures": 5,
    }
    kwargs.update(overrides)
    return _dag_run_verdict(outcome, **kwargs)  # type: ignore[arg-type]


class TestArmed:
    """Three spellings of "off" collapse to the one ``Budget`` uses."""

    @pytest.mark.parametrize("seconds", [None, 0, -1, -300])
    def test_non_positive_disables_the_guard(self, seconds: int | None) -> None:
        """A negative is not a short window.

        Left as a duration it would make ``elapsed >= grace`` true on the first
        poll and fire the guard before anything could have started — which is
        the whole reason the hand-rolled loop tested ``> 0`` rather than
        ``is not None``.
        """
        assert _armed(seconds) is None

    def test_a_positive_window_becomes_a_timedelta(self) -> None:
        assert _armed(180) == timedelta(seconds=180)


class TestAnyNodeStarted:
    """The latch is node-level, and that is load-bearing."""

    @pytest.mark.parametrize("status", [DAGNodeStatus.PENDING, DAGNodeStatus.SCHEDULED])
    def test_the_not_started_set_is_not_a_start(self, status: DAGNodeStatus) -> None:
        assert not _any_node_started(_result(DAGRunStatus.RUNNING, status))

    def test_a_live_run_over_pending_nodes_has_not_started(self) -> None:
        """The one case a run-level latch would get wrong.

        The parent AE workflow runs on the always-on automation-engine queue, so
        the top level flips to ``Running`` while the connector's own node sits
        unpolled. Keying the latch on the run status would report every unpolled
        task queue as started, and the no-worker verdict would be unreachable.
        """
        run = _result(
            DAGRunStatus.RUNNING, DAGNodeStatus.PENDING, DAGNodeStatus.PENDING
        )
        assert run.status is DAGRunStatus.RUNNING
        assert not _any_node_started(run)

    def test_one_started_node_is_enough(self) -> None:
        assert _any_node_started(
            _result(DAGRunStatus.RUNNING, DAGNodeStatus.PENDING, DAGNodeStatus.RUNNING)
        )

    def test_a_run_with_no_nodes_has_not_started(self) -> None:
        assert not _any_node_started(_result(DAGRunStatus.PENDING))


class TestAbsorbAEBlip:
    """What counts as a blip, and how long it buys."""

    def test_a_non_apperror_is_terminal(self) -> None:
        """A deterministic bug in the probe raises identically on every attempt,
        so waiting out the budget only delays the failure."""
        assert _absorb_ae_blip(TypeError("wrong signature")) is None

    def test_a_plain_apperror_is_absorbed_with_no_backoff(self) -> None:
        """Zero, not ``None``: absorb it, and the loop's own interval is the gap.
        ``None`` would re-raise, which is the opposite instruction."""
        assert _absorb_ae_blip(AppError(message="blip")) == timedelta(0)

    @pytest.mark.parametrize("requested", [None, 0, -5])
    def test_an_unusable_hint_leaves_the_interval_in_place(
        self, requested: float | None
    ) -> None:
        error = AtlanApiHttpError(
            message="500", target="native-status", retry_after_seconds=requested
        )
        assert _absorb_ae_blip(error) == timedelta(0)

    def test_a_fractional_hint_rounds_up(self) -> None:
        """A hint of 30.4s means "at least 30.4", so truncating to 30 would ask
        for less than the origin did. Rounding is the classifier's job because
        the primitive is handed a ``timedelta`` and has no business quantising
        someone else's duration."""
        error = AtlanApiHttpError(
            message="500", target="native-status", retry_after_seconds=30.4
        )
        assert _absorb_ae_blip(error) == timedelta(seconds=31)


class TestDAGProgress:
    """The per-poll line, and the quiet-gap ledger behind it."""

    def test_the_quiet_gap_grows_while_the_summary_is_unchanged(self) -> None:
        """The number stamped onto a timed-out observation.
        :class:`~application_sdk.testing.harness.outcome.Expired` does not carry
        it, so the ledger is the only thing that can answer it."""
        frozen = _result(DAGRunStatus.RUNNING, DAGNodeStatus.RUNNING)
        with fake_clock() as clock:
            progress = _DAGProgress()
            progress.observe(frozen)
            assert progress.quiet_seconds == 0.0
            clock.sleep(70)
            progress.observe(frozen)
            assert progress.quiet_seconds == 70.0

    def test_a_changed_summary_resets_the_gap(self) -> None:
        with fake_clock() as clock:
            progress = _DAGProgress()
            progress.observe(_result(DAGRunStatus.RUNNING, DAGNodeStatus.PENDING))
            clock.sleep(40)
            progress.observe(_result(DAGRunStatus.RUNNING, DAGNodeStatus.RUNNING))
            assert progress.quiet_seconds == 0.0

    def test_the_line_is_logged_on_a_change_and_then_throttled(self) -> None:
        """One line per node-state change, plus a heartbeat — not per poll.

        The throttle is the ``L006`` waiver's whole justification, so it is
        asserted rather than described: three identical readings inside the
        heartbeat window produce one line.
        """
        frozen = _result(DAGRunStatus.RUNNING, DAGNodeStatus.RUNNING)
        with fake_clock() as clock, patch.object(_client_module, "logger") as logged:
            progress = _DAGProgress()
            for _ in range(3):
                progress.observe(frozen)
                clock.sleep(1)
        assert logged.info.call_count == 1

    def test_a_frozen_summary_still_gets_a_heartbeat(self) -> None:
        """A lineage stage sits unchanged for minutes; without this an operator
        cannot tell "still polling" from "harness wedged"."""
        frozen = _result(DAGRunStatus.RUNNING, DAGNodeStatus.RUNNING)
        with fake_clock() as clock, patch.object(_client_module, "logger") as logged:
            progress = _DAGProgress()
            progress.observe(frozen)
            clock.sleep(31)
            progress.observe(frozen)
        assert logged.info.call_count == 2
        rendered = [call.args[0] % call.args[1:] for call in logged.info.call_args_list]
        # The elapsed stamp is what makes the second line readable as a
        # heartbeat rather than a repeat.
        assert "[  0s]" in rendered[0] and "[ 31s]" in rendered[1]

    def test_elapsed_comes_from_the_polls_own_clock(self) -> None:
        """Read through :func:`monotonic`, so a ``fake_clock`` test sees one
        timeline rather than a ledger that thinks no time has passed."""
        with fake_clock(start=5.0) as clock:
            assert monotonic() == 5.0
            clock.sleep(2.5)
            assert monotonic() == 7.5


class TestVerdictMapping:
    """Each generic verdict, and the AE answer it becomes."""

    def test_settled_returns_the_reading_unstamped(self) -> None:
        run = _result(DAGRunStatus.SUCCEEDED, DAGNodeStatus.SUCCEEDED)
        returned = _verdict(
            Settled(label="AE run", attempts=1, elapsed=timedelta(0), value=run)
        )
        assert returned is run
        assert not returned.stopped_watching

    def test_a_pending_run_that_never_started_blames_ae(self) -> None:
        with pytest.raises(AutomationEngineNotDispatchingError, match="never dis"):
            _verdict(
                NeverStarted(
                    label="AE run",
                    attempts=19,
                    elapsed=timedelta(seconds=180),
                    grace=timedelta(seconds=180),
                    last=_result(DAGRunStatus.PENDING, DAGNodeStatus.PENDING),
                )
            )

    def test_a_live_run_that_never_started_names_the_queue(self) -> None:
        with pytest.raises(NoWorkerOnTaskQueueError, match="atlan-openapi-e2e"):
            _verdict(
                NeverStarted(
                    label="AE run",
                    attempts=19,
                    elapsed=timedelta(seconds=180),
                    grace=timedelta(seconds=180),
                    last=_result(DAGRunStatus.RUNNING, DAGNodeStatus.PENDING),
                ),
                stall_task_queue="atlan-openapi-e2e-full-ci",
            )

    def test_the_stall_verdict_stamps_the_observed_gap_on_the_reading(self) -> None:
        """Two different numbers, and both belong on the result: the *configured*
        window says which guard fired, the *observed* gap says how long it had
        actually been frozen. They differ whenever a probe overran its interval.
        """
        run = _result(DAGRunStatus.RUNNING, DAGNodeStatus.RUNNING)
        with pytest.raises(DAGProgressStalledError) as excinfo:
            _verdict(
                Stalled(
                    label="AE run",
                    attempts=31,
                    elapsed=timedelta(seconds=310),
                    stall_window=timedelta(seconds=307),
                    fingerprint="🔄 node0",
                    last=run,
                )
            )
        stamped = excinfo.value.result
        assert stamped is not None
        assert stamped.progress_stalled_after_seconds == 300.0
        assert stamped.seconds_since_last_progress == 307.0
        assert stamped.progress_stalled and stamped.stopped_watching

    def test_the_ceiling_returns_the_reading_stamped_not_raised(self) -> None:
        """A caller needs the node breakdown to say which node was where when the
        harness stopped watching, and ``timed_out`` is what stops it reading the
        breakdown as a verdict."""
        run = _result(DAGRunStatus.RUNNING, DAGNodeStatus.RUNNING)
        progress = _DAGProgress()
        with fake_clock() as clock:
            progress.observe(run)
            clock.sleep(120)
            progress.observe(run)
        returned = _verdict(
            Expired(
                label="AE run",
                attempts=60,
                elapsed=timedelta(seconds=590),
                budget=timedelta(seconds=600),
                last=run,
            ),
            progress=progress,
        )
        assert returned.timed_out_after_seconds == 600.0
        assert returned.seconds_since_last_progress == 120.0


class TestVerdictMappingWithoutAReading:
    """The guards whose ``else`` the loop cannot reach today.

    Every one of these is a verdict arriving with ``last=None``. They are
    unreachable through ``poll_until`` as it stands — ``NeverStarted`` and
    ``Stalled`` are only returned after a successful reading — which is the
    reason to assert them here rather than to leave them to the loop: the
    alternative to a tested fallback is an ``AttributeError`` raised from inside
    a diagnostic, on the day the primitive grows a sixth caller.
    """

    def test_a_never_started_with_no_reading_says_so(self) -> None:
        """No reading means the top-level status is unknown, and the message says
        ``unknown`` rather than guessing ``Pending`` — which would blame AE for a
        dispatch it may well have made."""
        with pytest.raises(NoWorkerOnTaskQueueError, match="status=unknown"):
            _verdict(
                NeverStarted(
                    label="AE run",
                    attempts=1,
                    elapsed=timedelta(seconds=180),
                    grace=timedelta(seconds=180),
                )
            )

    @pytest.mark.parametrize("configured", [None, 0])
    def test_a_stall_with_no_configured_window_reports_the_observed_gap(
        self, configured: int | None
    ) -> None:
        """Not zero. Unreachable today — ``_armed`` disarms the watchdog for a
        non-positive window, so ``poll_until`` cannot return ``Stalled`` — but a
        stamped 0 would claim the watchdog window was zero seconds, which is a
        lie that sends the reader hunting a phantom. The observed quiet gap is
        true whatever the configuration was.
        """
        run = _result(DAGRunStatus.RUNNING, DAGNodeStatus.RUNNING)
        with pytest.raises(DAGProgressStalledError) as excinfo:
            _verdict(
                Stalled(
                    label="AE run",
                    attempts=31,
                    elapsed=timedelta(seconds=310),
                    stall_window=timedelta(seconds=307),
                    fingerprint="🔄 node0",
                    last=run,
                ),
                progress_stall_seconds=configured,
            )
        stamped = excinfo.value.result
        assert stamped is not None
        assert stamped.progress_stalled_after_seconds == 307.0

    def test_a_stall_with_no_reading_has_nothing_to_attach(self) -> None:
        with pytest.raises(AtlanApiTimeoutError, match="with no response"):
            _verdict(
                Stalled(
                    label="AE run",
                    attempts=31,
                    elapsed=timedelta(seconds=310),
                    stall_window=timedelta(seconds=300),
                    fingerprint="",
                )
            )

    def test_a_ceiling_with_no_reading_raises_rather_than_inventing_one(
        self,
    ) -> None:
        with pytest.raises(AtlanApiTimeoutError, match="with no response") as excinfo:
            _verdict(
                Expired(
                    label="AE run",
                    attempts=60,
                    elapsed=timedelta(seconds=590),
                    budget=timedelta(seconds=600),
                )
            )
        assert excinfo.value.timeout_seconds == 600.0


class TestIndeterminateSplitsInTwo:
    """One verdict, two failures — told apart by the streak length."""

    def test_a_streak_that_gave_up_re_raises_the_origins_own_error(self) -> None:
        """The operator sees AE's message, not a wrapper around it."""
        cause = AtlanApiHttpError(
            message="AE-COMMON-500-01: An unexpected error occurred",
            target="native-status",
        )
        with pytest.raises(AtlanApiHttpError, match="AE-COMMON-500-01"):
            _verdict(
                Indeterminate(
                    label="AE run",
                    attempts=5,
                    elapsed=timedelta(seconds=40),
                    cause=cause,
                    transient_failures=5,
                )
            )

    def test_a_budget_that_expired_mid_streak_reports_the_timeout(self) -> None:
        """The wait only gives up at ``max_transient_failures``, so a shorter
        streak means the deadline ended the loop — and there is no observation to
        stamp, which is a different thing to tell the operator."""
        cause = AtlanApiHttpError(message="500", target="native-status")
        with pytest.raises(AtlanApiTimeoutError, match="with no response"):
            _verdict(
                Indeterminate(
                    label="AE run",
                    attempts=3,
                    elapsed=timedelta(seconds=20),
                    cause=cause,
                    transient_failures=3,
                ),
                max_transient_failures=99,
            )

    @pytest.mark.parametrize("configured", [0, 1])
    def test_the_degenerate_pair_both_give_up_on_the_first_error(
        self, configured: int
    ) -> None:
        """``max_transient_failures`` of 0 and 1 are the same instruction — end on
        the first error — because the Nth consecutive error is the one that gives
        up. The split keys on ``max(1, configured)`` so a caller passing 0 still
        gets the origin's error rather than a timeout that never happened.
        """
        cause = AtlanApiHttpError(message="500", target="native-status")
        with pytest.raises(AtlanApiHttpError):
            _verdict(
                Indeterminate(
                    label="AE run",
                    attempts=1,
                    elapsed=timedelta(0),
                    cause=cause,
                    transient_failures=1,
                ),
                max_transient_failures=configured,
            )
