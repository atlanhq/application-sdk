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

:func:`assert_settled` converts back to the raise-on-failure style the connector
suites already use, so ``BaseE2ETest``'s observable behaviour is unchanged by
routing through this vocabulary.

Finding accumulation and the precondition gate that sit on top of these types
are FND-227 (child C) — this module scaffolds the vocabulary they return.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Generic, TypeAlias, TypeVar, Union

from application_sdk.testing.harness._errors import HarnessNotBuiltError

__all__ = [
    "Expired",
    "Indeterminate",
    "NeverStarted",
    "Outcome",
    "Settled",
    "Stalled",
    "assert_settled",
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

    Attributes:
        stall_window: How long the fingerprint went unchanged before the
            watchdog fired.
        fingerprint: The progress fingerprint that stopped changing — the
            single most useful line in the report, because it says *what* froze.
        last: The last probe reading.
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

    Returns:
        :attr:`Settled.value`.

    Raises:
        HarnessNotBuiltError: Always — the leaf mapping lands with
            :mod:`application_sdk.testing.harness.waiting` in FND-227 (child C).
    """
    raise HarnessNotBuiltError(
        message="assert_settled is not implemented yet",
        operation="assert_settled",
        reason="lands with waiting.poll_until, child C on FND-224 (= FND-227)",
        issue="FND-227",
        component="harness_outcome",
    )
