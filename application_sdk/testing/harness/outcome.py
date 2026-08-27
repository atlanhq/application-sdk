"""What a bounded wait produced — returned, not raised.

Three requirements force a structured return rather than an exception:

**Accumulation.** A scenario reports everything wrong with a run, not just the
first thing. An exception aborts at the first failing step; a value can be
collected alongside the others.

**Naming the step.** "The run failed" is not actionable; "the ``publish`` node
stopped changing state after 4m12s, last fingerprint ``✓✓·-``" is.
:class:`Stalled` carries the label and the fingerprint that stopped changing.

**Infrastructure failure is not assertion failure.** An expired vcluster token,
a dropped tunnel or a 503 from Atlas is neither a pass nor a component
regression, and grading it as either is worse than reporting it as neither.
:class:`Indeterminate` is that third answer. It is the same rule as "a path
matching no rule is reported as no coverage, not treated as passing", and it is
what closes the fail-open reads catalogued as C4 on FND-224 — four count/sample
methods that return zeros on search error, so an Atlas outage currently reports
*"asset floor not met"* and points the reader at the connector.

**Two independent designs landed on the same narrowing.** The runtime scenario
suite's ``crd_schema()`` caches "this CRD is not installed here" on a **404
only**; a 403 from a restricted role, an expired token or a read timeout raises
rather than caching a false negative. Same rule, arrived at separately: a
failed read may not borrow the vocabulary of a successful one. That is the
strongest evidence available that the rule is not a local preference, and it is
why :class:`Indeterminate` is a variant here rather than a convention the
caller is asked to remember (FND-227's 2026-08-17 amendment).

:func:`assert_settled` converts back to the raise-on-failure style the connector
suites already use, so ``BaseE2ETest``'s observable behaviour is unchanged by
routing through this vocabulary. :func:`grade` is the other direction — the
precondition gate that turns a bag of accumulated outcomes and findings into the
one verdict a scenario reports, with the ordering that keeps both halves of the
C4 fix honest: a confirmed regression is never softened into "could not tell",
and a pass is never claimed over a read that did not happen.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum
from typing import Any, Generic, TypeAlias, TypeVar, Union

from application_sdk.testing.harness._errors import (
    WaitExpiredError,
    WaitIndeterminateError,
    WaitNeverStartedError,
    WaitStalledError,
)
from application_sdk.testing.harness.expectations import (
    UNREADABLE,
    CountRead,
    Finding,
    SampleRead,
    Unreadable,
)

__all__ = [
    "Expired",
    "Indeterminate",
    "NeverStarted",
    "Outcome",
    "Settled",
    "Stalled",
    "Verdict",
    "as_count",
    "as_counts",
    "as_samples",
    "assert_settled",
    "grade",
]

T = TypeVar("T")


@dataclass(frozen=True, slots=True, kw_only=True)
class _Waited(Generic[T]):
    """Fields every outcome carries, whatever the verdict.

    Attributes:
        label: What was being waited on, as a noun phrase naming the concrete
            target ("AE run 8412 native status"). Goes straight into the report,
            so it is written for whoever reads a red CI leg.
        attempts: How many times the probe ran, 1-based.
        elapsed: Wall-clock time the wait consumed, on a monotonic clock.
    """

    label: str
    attempts: int
    elapsed: timedelta


@dataclass(frozen=True, slots=True, kw_only=True)
class Settled(_Waited[T]):
    """The probe reached the settled state inside its budget.

    Attributes:
        value: The final probe reading.
    """

    value: T


@dataclass(frozen=True, slots=True, kw_only=True)
class NeverStarted(_Waited[T]):
    """Nothing ever started, so the budget was spent waiting for work to begin.

    Distinct from :class:`Expired` because the diagnosis is different: work that
    never starts points at dispatch (a queue-name mismatch, no poller on the
    task queue, a worker scaled to zero), not at the work being slow.

    Attributes:
        grace: The start-grace window that closed without a start.
        last: The last probe reading, when there was one.
    """

    grace: timedelta
    last: T | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class Stalled(_Waited[T]):
    """Work started, then stopped making observable progress.

    Also the verdict
    :func:`~application_sdk.testing.harness.waiting.hold_stable` returns when an
    invariant breaks. Not a reuse of convenience: both are "it stopped being
    what it should be", both are diagnosed from the same two facts — how long it
    was fine for, and what it looked like when it wasn't — and giving the
    negative assertion its own variant would make every consumer handle a sixth
    case to say the same thing.

    Attributes:
        stall_window: How long the fingerprint went unchanged before the
            watchdog fired. From ``hold_stable``, how long the invariant held
            before it broke.
        fingerprint: The progress fingerprint that stopped changing — the
            single most useful line in the report, because it says *what* froze.
            From ``hold_stable``, where there is no progress string to freeze,
            a short rendering of the reading that broke the invariant: same job,
            the one line that says what went wrong.
        last: The last probe reading. From ``hold_stable``, the violating one.
    """

    stall_window: timedelta
    fingerprint: str
    last: T | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class Expired(_Waited[T]):
    """The budget ran out while work was still progressing.

    Attributes:
        budget: The total budget the wait was given.
        last: The last probe reading.
    """

    budget: timedelta
    last: T | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class Indeterminate(_Waited[T]):
    """The wait could not reach a verdict — the probe itself failed.

    Never graded as a pass and never as a component failure. An expired
    vcluster token must not read as a regression in the thing under test.

    Attributes:
        cause: The exception that made the reading unavailable. Retained rather
            than stringified so a caller can classify it (which backend, which
            transport) without re-parsing a message.
        transient_failures: How many probe errors were absorbed before the wait
            gave up.
    """

    cause: BaseException
    transient_failures: int = 0
    last: T | None = None


#: Every verdict a bounded wait can return.
#:
#: ``Union`` rather than ``|`` because this alias is generic: ``Outcome[T]`` has
#: to stay subscriptable at runtime, and ``types.UnionType`` is not on 3.11.
Outcome: TypeAlias = Union[
    Settled[T], NeverStarted[T], Stalled[T], Expired[T], Indeterminate[T]
]


def assert_settled(outcome: Outcome[T]) -> T:
    """Return the settled value, or raise the typed leaf for the verdict.

    The adapter between the outcome vocabulary and the raise-on-failure style
    the connector suites already expect: ``BaseE2ETest`` methods keep raising, so
    re-expressing them over :func:`~application_sdk.testing.harness.waiting.poll_until`
    is not observable to a connector.

    Args:
        outcome: The verdict to unwrap.

    One leaf per failing variant, each carrying that variant's fields rather
    than a rendered sentence — see
    :mod:`application_sdk.testing.harness._errors`. These are the *generic*
    leaves; a caller that wants ``testing/e2e``'s connector remediation advice
    matches on the variant itself and raises its own.

    Args:
        outcome: The verdict to unwrap.

    Returns:
        :attr:`Settled.value`.

    Raises:
        WaitNeverStartedError: The start-grace window closed with no start.
        WaitStalledError: Progress froze, or a ``hold_stable`` invariant broke.
        WaitExpiredError: The budget ran out with work still moving.
        WaitIndeterminateError: The probe could not be read, chained from the
            cause so the original transport error stays reachable.
    """
    seconds = outcome.elapsed.total_seconds()
    if isinstance(outcome, Settled):
        return outcome.value
    if isinstance(outcome, NeverStarted):
        grace = outcome.grace.total_seconds()
        raise WaitNeverStartedError(
            message=(
                f"nothing started on {outcome.label} within {grace:.0f}s "
                f"({outcome.attempts} probe(s) over {seconds:.0f}s). Work that "
                "never starts points at dispatch, not at the work being slow."
            ),
            label=outcome.label,
            grace_seconds=grace,
            attempts=outcome.attempts,
            elapsed_seconds=seconds,
        )
    if isinstance(outcome, Stalled):
        window = outcome.stall_window.total_seconds()
        raise WaitStalledError(
            message=(
                f"{outcome.label} stopped changing for {window:.0f}s after "
                f"{seconds:.0f}s; last fingerprint {outcome.fingerprint!r}"
            ),
            label=outcome.label,
            fingerprint=outcome.fingerprint,
            stall_window_seconds=window,
            attempts=outcome.attempts,
            elapsed_seconds=seconds,
        )
    if isinstance(outcome, Expired):
        budget = outcome.budget.total_seconds()
        raise WaitExpiredError(
            message=(
                f"{outcome.label} did not settle within {budget:.0f}s "
                f"({outcome.attempts} probe(s), still progressing when the "
                "budget ran out)"
            ),
            label=outcome.label,
            attempts=outcome.attempts,
            timeout_seconds=budget,
            elapsed_seconds=seconds,
        )
    raise WaitIndeterminateError(
        message=(
            f"could not read {outcome.label} — no verdict on the thing under "
            f"test. {outcome.transient_failures} probe error(s) absorbed over "
            f"{outcome.attempts} probe(s) in {seconds:.0f}s: {outcome.cause!r}"
        ),
        label=outcome.label,
        attempts=outcome.attempts,
        elapsed_seconds=seconds,
        transient_failures=outcome.transient_failures,
    ) from outcome.cause


class Verdict(StrEnum):
    """How a whole scenario graded, once everything it observed is in.

    Three values rather than a boolean, for the reason
    :class:`Indeterminate` exists: a suite that can only say pass or fail has to
    spell an unreadable dependency as one of them, and both spellings are wrong.
    """

    #: Everything that was checked was readable, and met its expectation.
    PASSED = "passed"
    #: At least one readable observation did not meet its expectation.
    FAILED = "failed"
    #: Nothing was found wrong, but something could not be read, so "nothing
    #: wrong" is not the same claim as "right".
    INDETERMINATE = "indeterminate"


def grade(
    *,
    outcomes: Iterable[Outcome[Any]] = (),
    findings: Iterable[Finding] = (),
) -> Verdict:
    """Reduce everything a scenario accumulated to its one verdict.

    The precondition gate. Both halves of the C4 fix are orderings, and this is
    where they are enforced:

    * **A pass is never claimed over a read that did not happen.**
      :class:`Indeterminate`, and any :class:`~...expectations.Finding` carrying
      :data:`~application_sdk.testing.harness.expectations.UNREADABLE`, block
      :attr:`Verdict.PASSED`. Silence from a search that errored is not
      evidence.
    * **A confirmed regression is never softened into "could not tell."**
      :attr:`Verdict.FAILED` outranks :attr:`Verdict.INDETERMINATE`, so one
      unrelated dropped tunnel cannot bury a real finding that a *successful*
      read produced. The gate exists to stop a failed read being graded, not to
      discard evidence that was actually gathered.

    Args:
        outcomes: Every bounded wait the scenario performed, in any order.
        findings: Every finding the evaluators produced, in any order.

    Returns:
        ``FAILED`` if anything readable was wrong; else ``INDETERMINATE`` if
        anything could not be read; else ``PASSED``.
    """
    unreadable = False
    for finding in findings:
        if finding.expectation == UNREADABLE:
            unreadable = True
        else:
            # A finding from a read that succeeded is evidence, and evidence
            # settles the verdict — no later Indeterminate can downgrade it.
            return Verdict.FAILED
    for outcome in outcomes:
        if isinstance(outcome, Settled):
            continue
        if isinstance(outcome, Indeterminate):
            unreadable = True
            continue
        return Verdict.FAILED
    return Verdict.INDETERMINATE if unreadable else Verdict.PASSED


# ---------------------------------------------------------------------------
# The projection from a verdict into an expectation's reading
# ---------------------------------------------------------------------------
#
# Two vocabularies, deliberately. A *wait* has five verdicts because it can
# expire or stall; a *reading fed to an expectation* has two — you have the
# number, or you do not. Neither collapses into the other without losing
# something, so the seam between them is these three functions rather than an
# ``isinstance`` at every call site.
#
# They live here rather than beside the evaluators that consume their output
# because this module already imports
# :mod:`application_sdk.testing.harness.expectations` (``grade`` reduces
# findings alongside verdicts) and the dependency can only run one way.


def as_count(reading: Outcome[int]) -> CountRead:
    """Project a one-shot count read into the evaluator's vocabulary.

    Args:
        reading: What the reader answered.

    Returns:
        The number, or an
        :class:`~application_sdk.testing.harness.expectations.Unreadable`
        carrying the cause.
    """
    if isinstance(reading, Settled):
        return reading.value
    return Unreadable(cause=_cause_of(reading))


def as_counts(
    reading: Outcome[Mapping[str, int]], type_names: Sequence[str]
) -> Mapping[str, CountRead]:
    """Project a per-type count read, spreading an unreadable one over its types.

    Args:
        reading: What the reader answered. The Atlas reader answers per *batch*:
            one failed type makes the whole mapping unreadable, because a mapping
            with four real numbers and one silent zero is graded as a real
            reading and the zero is the one an expectation trips on.
        type_names: The types that were asked for, so an unreadable batch still
            produces an entry for each — a type simply *missing* from the mapping
            counts as zero, which is the fail-open this projection closes.

    Returns:
        Type name -> count or ``Unreadable``.
    """
    if isinstance(reading, Settled):
        return dict(reading.value)
    unreadable = Unreadable(cause=_cause_of(reading))
    return dict.fromkeys(type_names, unreadable)


def as_samples(
    reading: Outcome[Mapping[str, Sequence[str]]], type_names: Sequence[str]
) -> Mapping[str, SampleRead]:
    """Project a qualified-name sample read the same way :func:`as_counts` does.

    Args:
        reading: What the reader answered.
        type_names: The types that were asked for.

    Returns:
        Type name -> sampled names or ``Unreadable``. The distinction matters
        more here than for counts:
        :func:`~application_sdk.testing.harness.expectations.evaluate_locations`
        *skips* an empty sample, so a failed read spelled as an empty list is a
        silent pass.
    """
    if isinstance(reading, Settled):
        return {name: list(value) for name, value in reading.value.items()}
    unreadable = Unreadable(cause=_cause_of(reading))
    return dict.fromkeys(type_names, unreadable)


def _cause_of(reading: Outcome[Any]) -> BaseException:
    """The exception behind a non-settled reading, or a stand-in for it.

    Args:
        reading: The reading that did not settle.

    Returns:
        :attr:`Indeterminate.cause` when there is one. Every one-shot read in
        :mod:`application_sdk.testing.harness.atlas` answers :class:`Settled` or
        :class:`Indeterminate`, so the fallback exists only so a future verdict
        cannot make this raise while assembling a report about something else.
    """
    cause = getattr(reading, "cause", None)
    if isinstance(cause, BaseException):
        return cause
    return RuntimeError(f"{reading.label} answered {type(reading).__name__}")
