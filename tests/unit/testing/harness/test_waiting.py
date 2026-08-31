"""Unit tests for the two bounded-wait primitives.

Tenant-free and clock-free: every wait here runs under
:func:`~application_sdk.testing.harness._poll.fake_clock`, which fast-forwards
the poll helper's own sleeps without touching :func:`time.monotonic` — patching
that process-wide hands the asyncio loop a mock for its own timers and produces
spurious ``StopIteration`` flakes.

What these pin is the primitive's *contract*. That it is a faithful extraction
of ``poll_native_status``'s interleaved guards is a separate claim, pinned
separately and differentially in ``test_waiting_equivalence.py``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from unittest.mock import patch

import pytest

from application_sdk.testing.harness import (
    Budget,
    Expired,
    Indeterminate,
    NeverStarted,
    Settled,
    Stalled,
    hold_stable,
    poll_until,
)
from application_sdk.testing.harness._poll import FakeClock, fake_clock

# ---------------------------------------------------------------------------
# Scripting a probe
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Reading:
    """One scripted probe reading.

    A typed reading rather than a bare string or dict, so a test that means
    "started but not finished" says so in the type it constructs.

    Attributes:
        phase: What the reading says the work is doing.
        mark: The progress fingerprint this reading would produce. Two readings
            with the same mark are indistinguishable to the watchdog.
    """

    phase: str
    mark: str = "-"


class Blip(Exception):
    """A probe failure a classifier can absorb, optionally with a backoff.

    Attributes:
        retry_after: What the origin asked for, or ``None`` for "no request",
            mirroring an HTTP ``Retry-After`` that was absent.
    """

    def __init__(self, retry_after: timedelta | None = None) -> None:
        super().__init__(f"blip (retry_after={retry_after})")
        self.retry_after = retry_after


class Bug(Exception):
    """A probe failure no classifier absorbs — a deterministic bug in the probe."""


@dataclass
class Script:
    """A probe that replays scripted items, repeating the last one forever.

    Repeating rather than exhausting is deliberate: a wait that outlives its
    script should hit its *budget*, not a ``StopIteration`` that would surface
    as an unrelated ``RuntimeError`` from inside the coroutine.

    Attributes:
        items: Readings to return, and exceptions to raise, in order.
        calls: How many times the probe was called.
    """

    items: list[Reading | Exception]
    calls: int = field(default=0)

    async def __call__(self) -> Reading:
        item = self.items[min(self.calls, len(self.items) - 1)]
        self.calls += 1
        if isinstance(item, Exception):
            raise item
        return item


@dataclass
class NoneScript:
    """A ``Probe[None]``: its *successful* reading is ``None``.

    ``Probe[T]`` puts no bound on ``T``, so this is an ordinary probe, not a
    pathological one — a cluster read answering "no such deployment", a status
    field that is legitimately null. It exists here because it is the only shape
    that can tell "I read a value" apart from "I never read one": under a
    ``last_value is None`` test the two are identical.

    Attributes:
        readings: How many successful ``None`` readings before it starts failing.
        calls: How many times the probe was called.
    """

    readings: int
    calls: int = field(default=0)

    async def __call__(self) -> None:
        self.calls += 1
        if self.calls > self.readings:
            raise Blip()
        return None


def _budget(**overrides: object) -> Budget:
    """A 60s/5s budget — twelve polls — with the named fields overridden."""
    fields: dict[str, object] = {
        "timeout": timedelta(seconds=60),
        "poll_interval": timedelta(seconds=5),
        "heartbeat": None,
    }
    fields.update(overrides)
    return Budget(**fields)  # pyright: ignore[reportArgumentType]


def _settled(phase: str) -> object:
    return lambda reading: reading.phase == phase


def _absorb_all(exc: BaseException) -> timedelta | None:
    """Classify every :class:`Blip` as transient, honouring its request."""
    return exc.retry_after or timedelta(0) if isinstance(exc, Blip) else None


_MARK = lambda reading: reading.mark  # noqa: E731 — one expression, named once


# ---------------------------------------------------------------------------
# poll_until — settling
# ---------------------------------------------------------------------------


async def test_a_reading_that_is_already_settled_costs_one_probe() -> None:
    script = Script([Reading("done")])
    with fake_clock() as clock:
        outcome = await poll_until(
            script, settled=_settled("done"), budget=_budget(), label="the run"
        )
    assert isinstance(outcome, Settled)
    assert outcome.value == Reading("done")
    assert outcome.attempts == 1
    assert outcome.label == "the run"
    assert clock.slept == []


async def test_settling_late_reports_the_attempts_and_the_elapsed_it_took() -> None:
    script = Script([Reading("run"), Reading("run"), Reading("done")])
    with fake_clock():
        outcome = await poll_until(
            script, settled=_settled("done"), budget=_budget(), label="the run"
        )
    assert isinstance(outcome, Settled)
    assert outcome.attempts == 3
    # Two gaps of the 5s interval separate three probes.
    assert outcome.elapsed == timedelta(seconds=10)


async def test_a_wait_that_never_settles_expires_with_the_last_reading() -> None:
    script = Script([Reading("run")])
    with fake_clock() as clock:
        outcome = await poll_until(
            script, settled=_settled("done"), budget=_budget(), label="the run"
        )
    assert isinstance(outcome, Expired)
    assert outcome.budget == timedelta(seconds=60)
    assert outcome.last == Reading("run")
    assert sum(clock.slept) <= 60, "a bounded wait may not sleep past its own budget"


# ---------------------------------------------------------------------------
# poll_until — the start-grace latch
# ---------------------------------------------------------------------------


async def test_nothing_starting_inside_the_grace_window_is_never_started() -> None:
    """The diagnosis that separates dispatch failures from slow work."""
    script = Script([Reading("pending")])
    with fake_clock():
        outcome = await poll_until(
            script,
            settled=_settled("done"),
            started=_settled("running"),
            budget=_budget(start_grace=timedelta(seconds=20)),
            label="the run",
        )
    assert isinstance(outcome, NeverStarted)
    assert outcome.grace == timedelta(seconds=20)
    assert outcome.elapsed == timedelta(seconds=20)
    assert outcome.last == Reading("pending")


async def test_no_started_predicate_means_started_so_the_grace_cannot_fire() -> None:
    script = Script([Reading("pending")])
    with fake_clock():
        outcome = await poll_until(
            script,
            settled=_settled("done"),
            budget=_budget(start_grace=timedelta(seconds=20)),
            label="the run",
        )
    assert isinstance(outcome, Expired)


async def test_started_is_a_latch_not_a_level() -> None:
    """Work that starts and finishes between two polls still counts as started.

    Without the latch a node that ran to completion inside one interval would
    read as "never started" and be reported as a dispatch failure — the exact
    misdiagnosis the variant exists to prevent.
    """
    script = Script(
        [Reading("pending"), Reading("running"), Reading("pending"), Reading("done")]
    )
    with fake_clock():
        outcome = await poll_until(
            script,
            settled=_settled("done"),
            started=_settled("running"),
            budget=_budget(start_grace=timedelta(seconds=5)),
            label="the run",
        )
    assert isinstance(outcome, Settled)


async def test_the_grace_is_checked_only_after_a_reading_that_succeeded() -> None:
    """A probe that could not be read has not shown that nothing started."""
    script = Script([Blip(), Blip(), Reading("running"), Reading("done")])
    with fake_clock():
        outcome = await poll_until(
            script,
            settled=_settled("done"),
            started=_settled("running"),
            transient=_absorb_all,
            budget=_budget(start_grace=timedelta(seconds=5), max_transient_failures=5),
            label="the run",
        )
    assert isinstance(outcome, Settled)


# ---------------------------------------------------------------------------
# poll_until — the progress watchdog
# ---------------------------------------------------------------------------


async def test_a_frozen_fingerprint_stalls_and_carries_what_froze() -> None:
    script = Script([Reading("running", mark="✓·")])
    with fake_clock():
        outcome = await poll_until(
            script,
            settled=_settled("done"),
            fingerprint=_MARK,
            budget=_budget(stall_timeout=timedelta(seconds=20)),
            label="the run",
        )
    assert isinstance(outcome, Stalled)
    assert outcome.fingerprint == "✓·"
    assert outcome.stall_window == timedelta(seconds=20)
    assert outcome.elapsed == timedelta(seconds=20)


async def test_the_stall_window_is_the_quiet_gap_not_the_whole_wait() -> None:
    """Six polls of real progress, then it freezes.

    The two numbers are only the same when the fingerprint never moved at all,
    which is what makes this the one shape that tells them apart: reporting the
    total elapsed would say a run that worked for 20s and then wedged had been
    frozen from the start.
    """
    script = Script(
        [Reading("running", mark=f"m{n}") for n in range(5)]
        + [Reading("running", mark="frozen")]
    )
    with fake_clock():
        outcome = await poll_until(
            script,
            settled=_settled("done"),
            fingerprint=_MARK,
            budget=_budget(
                timeout=timedelta(seconds=120), stall_timeout=timedelta(seconds=20)
            ),
            label="the run",
        )
    assert isinstance(outcome, Stalled)
    assert outcome.fingerprint == "frozen"
    # Six distinct marks over the first 25s — the last one arrives at 25s and is
    # the one that then freezes — so the window closes at 45s.
    assert outcome.elapsed == timedelta(seconds=45)
    assert outcome.stall_window == timedelta(seconds=20)


async def test_a_changing_fingerprint_resets_the_watchdog() -> None:
    """Every poll moves the mark on, so a 20s window never closes inside 60s."""
    script = Script([Reading("running", mark=f"m{n}") for n in range(20)])
    with fake_clock():
        outcome = await poll_until(
            script,
            settled=_settled("done"),
            fingerprint=_MARK,
            budget=_budget(stall_timeout=timedelta(seconds=20)),
            label="the run",
        )
    assert isinstance(outcome, Expired)


async def test_no_fingerprint_disables_the_watchdog_whatever_the_budget_says() -> None:
    """There is nothing to compare, so the window cannot mean anything."""
    script = Script([Reading("running")])
    with fake_clock():
        outcome = await poll_until(
            script,
            settled=_settled("done"),
            budget=_budget(stall_timeout=timedelta(seconds=20)),
            label="the run",
        )
    assert isinstance(outcome, Expired)


async def test_the_watchdog_waits_for_something_to_have_started() -> None:
    """A run that has not started yet is the grace window's business, not the
    watchdog's — otherwise a slow dispatch is reported as a wedged node."""
    script = Script([Reading("pending", mark="··")])
    with fake_clock():
        outcome = await poll_until(
            script,
            settled=_settled("done"),
            started=_settled("running"),
            fingerprint=_MARK,
            budget=_budget(stall_timeout=timedelta(seconds=20)),
            label="the run",
        )
    assert isinstance(outcome, Expired), "no start, so no stall verdict"


# ---------------------------------------------------------------------------
# poll_until — transient probe errors
# ---------------------------------------------------------------------------


async def test_an_unclassified_probe_error_propagates() -> None:
    """A deterministic bug raises the same exception on every attempt, so
    waiting out the budget only delays the failure."""
    script = Script([Bug()])
    with fake_clock():
        with pytest.raises(Bug):
            await poll_until(
                script,
                settled=_settled("done"),
                transient=_absorb_all,
                budget=_budget(max_transient_failures=5),
                label="the run",
            )
    assert script.calls == 1


async def test_with_no_classifier_at_all_every_probe_error_propagates() -> None:
    script = Script([Blip()])
    with fake_clock():
        with pytest.raises(Blip):
            await poll_until(
                script,
                settled=_settled("done"),
                budget=_budget(max_transient_failures=5),
                label="the run",
            )


async def test_blips_below_the_streak_are_absorbed() -> None:
    script = Script([Blip(), Blip(), Reading("done")])
    with fake_clock():
        outcome = await poll_until(
            script,
            settled=_settled("done"),
            transient=_absorb_all,
            budget=_budget(max_transient_failures=5),
            label="the run",
        )
    assert isinstance(outcome, Settled)
    assert outcome.attempts == 3


async def test_the_streak_gives_up_on_the_nth_consecutive_error() -> None:
    """``max_transient_failures=N`` stops on error N, absorbing N-1.

    The boundary ``poll_native_status`` has today and
    ``test_gives_up_at_max_transient_failures`` pins. Preserved rather than
    corrected: drifting it here would change every connector's tolerance the
    moment child D rewires the loop, invisibly. Normalising it is on FND-240.
    """
    script = Script([Blip()])
    with fake_clock():
        outcome = await poll_until(
            script,
            settled=_settled("done"),
            transient=_absorb_all,
            budget=_budget(max_transient_failures=3),
            label="the run",
        )
    assert isinstance(outcome, Indeterminate)
    assert isinstance(outcome.cause, Blip)
    assert outcome.transient_failures == 3
    assert script.calls == 3


async def test_a_success_resets_the_streak() -> None:
    """A streak, not a total: a twenty-minute wait is not bounded by the sum of
    the blips it survived."""
    script = Script(
        [Blip(), Blip(), Reading("running"), Blip(), Blip(), Reading("done")]
    )
    with fake_clock():
        outcome = await poll_until(
            script,
            settled=_settled("done"),
            transient=_absorb_all,
            budget=_budget(max_transient_failures=3),
            label="the run",
        )
    assert isinstance(outcome, Settled)


async def test_zero_tolerance_ends_the_wait_on_the_first_error() -> None:
    script = Script([Blip()])
    with fake_clock():
        outcome = await poll_until(
            script,
            settled=_settled("done"),
            transient=_absorb_all,
            budget=_budget(max_transient_failures=0),
            label="the run",
        )
    assert isinstance(outcome, Indeterminate)
    assert script.calls == 1


async def test_a_budget_spent_without_one_reading_is_indeterminate() -> None:
    """ "It did not finish in time" is a claim about the thing under test, and a
    wait that never read it is not entitled to make one."""
    script = Script([Blip()])
    with fake_clock():
        outcome = await poll_until(
            script,
            settled=_settled("done"),
            transient=_absorb_all,
            budget=_budget(max_transient_failures=999),
            label="the run",
        )
    assert isinstance(outcome, Indeterminate)
    assert isinstance(outcome.cause, Blip)


async def test_a_read_none_is_not_the_same_as_never_having_read() -> None:
    """One successful ``None`` reading, then absorbed blips to the ceiling.

    The wait *did* read its target, so the budget running out is ``Expired`` —
    a claim about the thing under test that the wait has earned. Deciding on
    ``last_value is None`` would report ``Indeterminate`` instead, disowning a
    reading it actually took, because a legitimate ``None`` is spelled exactly
    like "nothing was ever observed".
    """
    script = NoneScript(readings=1)
    with fake_clock():
        outcome = await poll_until(
            script,
            settled=lambda _reading: False,
            transient=_absorb_all,
            budget=_budget(max_transient_failures=999),
            label="the run",
        )
    assert isinstance(outcome, Expired)
    assert script.calls > 1, "the blips after the reading are what trip the bug"


async def test_a_probe_that_never_read_is_still_indeterminate() -> None:
    """The other half of the pair: zero readings keeps the verdict honest, so the
    fix above cannot have been "always report Expired"."""
    script = NoneScript(readings=0)
    with fake_clock():
        outcome = await poll_until(
            script,
            settled=lambda _reading: False,
            transient=_absorb_all,
            budget=_budget(max_transient_failures=999),
            label="the run",
        )
    assert isinstance(outcome, Indeterminate)


async def test_an_indeterminate_still_carries_the_last_good_reading() -> None:
    script = Script([Reading("running"), Blip()])
    with fake_clock():
        outcome = await poll_until(
            script,
            settled=_settled("done"),
            transient=_absorb_all,
            budget=_budget(max_transient_failures=2),
            label="the run",
        )
    assert isinstance(outcome, Indeterminate)
    assert outcome.last == Reading("running")


async def test_a_cancellation_is_not_a_failed_read() -> None:
    """``CancelledError`` must reach the task that sent it, even under a
    classifier that would absorb anything it was offered."""

    async def cancelling() -> Reading:
        raise KeyboardInterrupt

    with fake_clock():
        with pytest.raises(KeyboardInterrupt):
            await poll_until(
                cancelling,
                settled=_settled("done"),
                transient=lambda _exc: timedelta(0),
                budget=_budget(max_transient_failures=5),
                label="the run",
            )


# ---------------------------------------------------------------------------
# poll_until — honouring origin backoff
# ---------------------------------------------------------------------------


async def _blip_wait(clock_holder: list[FakeClock], **budget_kwargs: object) -> None:
    script = Script([Blip(timedelta(seconds=30)), Reading("done")])
    with fake_clock() as clock:
        clock_holder.append(clock)
        await poll_until(
            script,
            settled=_settled("done"),
            transient=_absorb_all,
            budget=_budget(max_transient_failures=5, **budget_kwargs),
            label="the run",
        )


async def test_an_origin_backoff_replaces_the_poll_interval() -> None:
    """An overloaded origin answering "retry_after: 30" must not burn the whole
    failure streak inside its own wait window."""
    clocks: list[FakeClock] = []
    await _blip_wait(clocks, retry_after_budget=timedelta(seconds=300))
    assert clocks[0].slept == [30.0]


async def test_origin_backoff_is_ignored_without_a_retry_after_budget() -> None:
    """``retry_after_budget=None`` is "honour no origin backoff" — one rule, so
    it degrades to the fixed interval rather than needing a second branch."""
    clocks: list[FakeClock] = []
    await _blip_wait(clocks)
    assert clocks[0].slept == [5.0]


async def test_a_single_pathological_backoff_is_capped() -> None:
    clocks: list[FakeClock] = []
    script = Script([Blip(timedelta(seconds=900)), Reading("done")])
    with fake_clock() as clock:
        clocks.append(clock)
        await poll_until(
            script,
            settled=_settled("done"),
            transient=_absorb_all,
            budget=_budget(
                timeout=timedelta(seconds=600),
                max_transient_failures=5,
                retry_after_budget=timedelta(seconds=300),
                max_retry_after=timedelta(seconds=120),
            ),
            label="the run",
        )
    assert clocks[0].slept == [120.0]


async def test_the_retry_after_budget_bounds_the_total_honoured_waiting() -> None:
    """Three 60s requests against a 100s above-the-interval allowance.

    Only the *above-the-interval* part is charged, so a 60s gap spends 55: the
    first request is honoured whole, the second is clamped to the 45 left, and
    by the third there are 5 — exactly the fixed interval — so the honouring
    degrades to the gap the loop already guaranteed rather than to nothing.
    """
    script = Script(
        [
            Blip(timedelta(seconds=60)),
            Blip(timedelta(seconds=60)),
            Blip(timedelta(seconds=60)),
            Reading("done"),
        ]
    )
    with fake_clock() as clock:
        await poll_until(
            script,
            settled=_settled("done"),
            transient=_absorb_all,
            budget=_budget(
                timeout=timedelta(seconds=600),
                max_transient_failures=5,
                retry_after_budget=timedelta(seconds=100),
            ),
            label="the run",
        )
    assert clock.slept == [60.0, 45.0, 5.0]


async def test_honouring_a_backoff_can_only_lengthen_the_gap() -> None:
    """A 1s request against a 5s interval keeps the 5s the loop guaranteed."""
    script = Script([Blip(timedelta(seconds=1)), Reading("done")])
    with fake_clock() as clock:
        await poll_until(
            script,
            settled=_settled("done"),
            transient=_absorb_all,
            budget=_budget(
                max_transient_failures=5, retry_after_budget=timedelta(seconds=300)
            ),
            label="the run",
        )
    assert clock.slept == [5.0]


async def test_an_honoured_backoff_is_still_clamped_to_the_deadline() -> None:
    """A 120s request against a 30s residual budget may not block for 120s."""
    script = Script([Blip(timedelta(seconds=120))])
    with fake_clock() as clock:
        await poll_until(
            script,
            settled=_settled("done"),
            transient=_absorb_all,
            budget=_budget(
                timeout=timedelta(seconds=30),
                max_transient_failures=99,
                retry_after_budget=timedelta(seconds=300),
            ),
            label="the run",
        )
    assert sum(clock.slept) <= 30


# ---------------------------------------------------------------------------
# poll_until — the heartbeat
# ---------------------------------------------------------------------------


async def test_the_heartbeat_is_silent_when_the_budget_silences_it() -> None:
    script = Script([Reading("running")])
    with (
        patch("application_sdk.testing.harness._poll._log_heartbeat") as beat,
        fake_clock(),
    ):
        await poll_until(
            script, settled=_settled("done"), budget=_budget(), label="the run"
        )
    beat.assert_not_called()


async def test_the_heartbeat_narrates_a_wait_that_says_nothing_else() -> None:
    script = Script([Reading("running")])
    with (
        patch("application_sdk.testing.harness._poll._log_heartbeat") as beat,
        fake_clock(),
    ):
        await poll_until(
            script,
            settled=_settled("done"),
            budget=_budget(
                timeout=timedelta(seconds=120), heartbeat=timedelta(seconds=30)
            ),
            label="the run",
        )
    # 120s of waiting at a 30s cadence, and the first poll is at zero elapsed so
    # it does not count as one: 30s, 60s, 90s.
    assert beat.call_count == 3
    assert [call.args[0] for call in beat.call_args_list] == ["the run"] * 3


# ---------------------------------------------------------------------------
# hold_stable
# ---------------------------------------------------------------------------


async def test_a_hold_that_is_never_violated_spends_its_whole_budget() -> None:
    """Success *is* the budget expiring with nothing having gone wrong."""
    script = Script([Reading("two replicas")])
    with fake_clock() as clock:
        outcome = await hold_stable(
            script,
            invariant=lambda reading: reading.phase == "two replicas",
            budget=_budget(),
            label="worker replicas while the extract activity runs",
        )
    assert isinstance(outcome, Settled)
    assert outcome.value == Reading("two replicas")
    assert sum(clock.slept) == 55.0, "twelve probes, eleven gaps, inside 60s"


async def test_the_first_violation_ends_the_hold_and_names_the_reading() -> None:
    script = Script(
        [Reading("two replicas"), Reading("two replicas"), Reading("scaled away")]
    )
    with fake_clock():
        outcome = await hold_stable(
            script,
            invariant=lambda reading: reading.phase == "two replicas",
            budget=_budget(),
            label="worker replicas",
        )
    assert isinstance(outcome, Stalled)
    assert outcome.last == Reading("scaled away")
    assert "scaled away" in outcome.fingerprint
    # How long it held before it broke — a flap at 10s and a flap at 19m are
    # not the same report.
    assert outcome.stall_window == timedelta(seconds=10)


async def test_a_violating_reading_is_rendered_as_one_line() -> None:
    script = Script([Reading("x" * 400)])
    with fake_clock():
        outcome = await hold_stable(
            script, invariant=lambda _r: False, budget=_budget(), label="replicas"
        )
    assert isinstance(outcome, Stalled)
    assert len(outcome.fingerprint) <= 120
    assert outcome.fingerprint.endswith("…")


async def test_an_unclassified_error_ends_a_hold_by_propagating() -> None:
    script = Script([Bug()])
    with fake_clock():
        with pytest.raises(Bug):
            await hold_stable(
                script, invariant=lambda _r: True, budget=_budget(), label="replicas"
            )


async def test_a_hold_absorbs_blips_the_way_a_poll_does() -> None:
    """The scenarios this exists for run over a VPN plus a vcluster tunnel,
    where a reset connection mid-hold is routine."""
    script = Script([Blip(), Blip(), Reading("steady")])
    with fake_clock():
        outcome = await hold_stable(
            script,
            invariant=lambda reading: reading.phase == "steady",
            transient=_absorb_all,
            budget=_budget(max_transient_failures=5),
            label="replicas",
        )
    assert isinstance(outcome, Settled)


async def test_a_hold_nobody_could_observe_is_not_a_hold_that_passed() -> None:
    """ "Nothing went wrong" and "I did not look" are the same silence."""
    script = Script([Blip()])
    with fake_clock():
        outcome = await hold_stable(
            script,
            invariant=lambda _r: True,
            transient=_absorb_all,
            budget=_budget(max_transient_failures=3),
            label="replicas",
        )
    assert isinstance(outcome, Indeterminate)
    assert outcome.transient_failures == 3


async def test_a_hold_over_none_readings_holds() -> None:
    """Every reading is a legitimate ``None`` and the invariant accepts it.

    So the hold held, for the whole budget, and the verdict is ``Settled`` with
    ``None`` as its value. Deciding observation on ``last_value is None`` would
    call this window unobservable and attach a synthetic ``RuntimeError`` to a
    hold in which every single probe succeeded.
    """
    script = NoneScript(readings=1_000)
    with fake_clock():
        outcome = await hold_stable(
            script,
            invariant=lambda reading: reading is None,
            budget=_budget(),
            label="no deployment while the app is uninstalled",
        )
    assert isinstance(outcome, Settled)
    assert outcome.value is None
    assert outcome.attempts == 12, "it spent the whole budget rather than bailing"


async def test_a_hold_over_none_readings_still_catches_a_violation() -> None:
    """The pair to the above: an invariant that rejects ``None`` still stalls, so
    the fix cannot have been "treat a None reading as always acceptable"."""
    script = NoneScript(readings=1_000)
    with fake_clock():
        outcome = await hold_stable(
            script,
            invariant=lambda reading: reading is not None,
            budget=_budget(),
            label="a deployment exists",
        )
    assert isinstance(outcome, Stalled)
    assert outcome.last is None
    assert outcome.fingerprint == "None"


async def test_a_hold_whose_every_probe_blipped_never_reports_settled() -> None:
    """Tolerance high enough to absorb every blip must still not manufacture a
    pass out of a window in which nothing was ever read."""
    script = Script([Blip()])
    with fake_clock():
        outcome = await hold_stable(
            script,
            invariant=lambda _r: True,
            transient=_absorb_all,
            budget=_budget(max_transient_failures=999),
            label="replicas",
        )
    assert isinstance(outcome, Indeterminate)
