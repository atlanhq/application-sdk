"""Typed error leaves for the shared test harness.

Private module: leaves that are public surface are re-exported from
:mod:`application_sdk.testing.harness`. Mirrors
:mod:`application_sdk.testing.e2e._errors`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from application_sdk.errors.leaves import (
    AppTimeoutError,
    DependencyUnavailableError,
    InvalidInputError,
    PreconditionError,
    UnimplementedError,
)

__all__ = [
    "FixtureNotConfiguredError",
    "HarnessNotBuiltError",
    "MissingHarnessClassAttrError",
    "MissingTenantEnvError",
    "PreconditionsFailedError",
    "PreconditionsIndeterminateError",
    "SubstrateHasNoClusterError",
    "SyncBridgeInAsyncContextError",
    "WaitExpiredError",
    "WaitIndeterminateError",
    "WaitNeverStartedError",
    "WaitStalledError",
]


@dataclass(kw_only=True)
class SyncBridgeInAsyncContextError(PreconditionError):
    """:func:`~application_sdk.testing.harness.run_sync` was called from a running loop.

    The bridge owns its thread's event loop, so it cannot be re-entered from
    inside one: ``run_until_complete`` on a running loop raises, and standing up
    a second loop on the same thread would fragment every client the harness
    caches per loop. The caller wants the ``_async`` twin of whatever it called.
    """

    code: ClassVar[str] = "PRECONDITION_SYNC_BRIDGE_IN_ASYNC_CONTEXT"
    component: str | None = "harness_sync_bridge"


@dataclass(kw_only=True)
class HarnessNotBuiltError(UnimplementedError, NotImplementedError):
    """A scaffolded harness function whose implementation has not landed yet.

    Inherits :class:`NotImplementedError` alongside the SDK's
    :class:`~application_sdk.errors.leaves.UnimplementedError` leaf so both hold:
    it is a typed leaf carrying a category and an audience, *and* it is still
    what Python's convention — and any reader's ``except`` — expects from a
    function that has not been written.

    :attr:`issue` names the child issue that fills the function in, as a field
    rather than as a substring of the message, so an audit of what is left in the
    scaffold can enumerate it instead of grepping prose.

    Attributes:
        issue: Identifier of the issue that lands the implementation.
        component: Which part of the harness the gap is in.
    """

    code: ClassVar[str] = "UNIMPLEMENTED_HARNESS_NOT_BUILT"
    issue: str | None = None
    component: str | None = "test_harness"


@dataclass(kw_only=True)
class MissingHarnessClassAttrError(InvalidInputError):
    """A required class-level attribute was not set on the test harness.

    Moved here from ``testing/e2e/_errors.py`` with the AE half of
    ``client.py`` (child F on FND-224): ``cold_start_submit_kwargs`` raises it,
    and a harness module cannot import from the package child H re-expresses
    over it. Same class, same ``code`` — ``testing/e2e/_errors`` re-exports it,
    so every existing import and ``except`` clause is unchanged.
    """

    code: ClassVar[str] = "INVALID_INPUT_HARNESS_CLASS_ATTR"


@dataclass(kw_only=True)
class MissingTenantEnvError(InvalidInputError):
    """The environment carries no tenant for the harness to run against.

    Named for the tenant rather than for the harness because that is what is
    missing: ``application_sdk.testing.e2e._errors.MissingHarnessEnvError`` is
    the pre-harness leaf covering the same gap, and it stays until child H
    re-expresses ``testing/e2e`` over this package. Two leaves with one name and
    two codes is the confusion this avoids.

    Attributes:
        field: The variable names that were absent, comma-separated.
    """

    code: ClassVar[str] = "INVALID_INPUT_HARNESS_TENANT_ENV"
    field: str | None = "ATLAN_BASE_URL,ATLAN_API_KEY"


@dataclass(kw_only=True)
class FixtureNotConfiguredError(PreconditionError):
    """A composer requested a harness fixture without declaring what it needs.

    The three points of variance the harness deliberately does *not* assume are
    shared — tenant wiring, app wiring and execution substrate (FND-244) — are
    published as fixtures a composer overrides. Where there is no defensible
    default, the default raises this instead of guessing: a fixture that guessed
    would reach whichever cluster the developer's ``kubectl`` last pointed at, or
    invent a connection type that teardown then purges under.

    Raised only when the dependent fixture is actually requested, so a suite that
    never asks for a connection identity never has to declare a connection type.

    Attributes:
        fixture: Name of the fixture to override, so the message names the fix
            rather than describing it.
    """

    code: ClassVar[str] = "PRECONDITION_HARNESS_FIXTURE_NOT_CONFIGURED"
    component: str | None = "harness_fixtures"
    fixture: str | None = None


@dataclass(kw_only=True)
class SubstrateHasNoClusterError(PreconditionError):
    """A cluster read was requested on a substrate that has no cluster.

    The connector harness runs against a docker-compose worker on ``localhost``;
    there is no Kubernetes API to read, and no kubeconfig that would be the right
    one. Answering with a reader that fails on first use, or silently reading
    whichever cluster the ambient kubeconfig names, are both worse than saying so
    at the seam.

    Attributes:
        substrate: The declared substrate, so the message says which choice
            produced the refusal.
    """

    code: ClassVar[str] = "PRECONDITION_HARNESS_SUBSTRATE_HAS_NO_CLUSTER"
    component: str | None = "harness_fixtures"
    substrate: str | None = None


@dataclass(kw_only=True)
class PreconditionsFailedError(PreconditionError):
    """The scenario's starting state was read, and it was not fit to test on.

    Deliberately **not** an ``AssertionError``: under pytest that makes an unmet
    precondition an *error* rather than a *failure*, which is the whole point of
    the gate — a red leg that reads as "the thing under test regressed" when the
    environment was never prepared costs a diagnosis, and the gate exists to
    split those two.

    Attributes:
        checks: Labels of the checks that were not met, comma-separated. A field
            rather than only a message fragment so a report can group by check.
        verdict: The graded verdict, for a caller routing on it without
            re-grading.
    """

    code: ClassVar[str] = "PRECONDITION_HARNESS_PRECONDITIONS_FAILED"
    component: str | None = "harness_preconditions"
    checks: str | None = None
    verdict: str | None = None


@dataclass(kw_only=True)
class PreconditionsIndeterminateError(DependencyUnavailableError):
    """The scenario's starting state could not be read, so nothing was dispatched.

    The gate's third answer, as a leaf. ``DEPENDENCY_UNAVAILABLE`` rather than
    ``PRECONDITION`` for the same reason
    :class:`WaitIndeterminateError` is: the category's own definition is "the
    same call would work once the dependency recovers", so a CI lane that reruns
    on infrastructure failure can act on the category without parsing prose — and
    an expired vcluster token must never be graded as a regression.

    Attributes:
        checks: Labels of the checks that could not be read, comma-separated.
        verdict: The graded verdict.
    """

    code: ClassVar[str] = "DEPENDENCY_UNAVAILABLE_HARNESS_PRECONDITIONS_INDETERMINATE"
    component: str | None = "harness_preconditions"
    checks: str | None = None
    verdict: str | None = None


# ---------------------------------------------------------------------------
# The four failing verdicts, as the leaves ``assert_settled`` raises
# ---------------------------------------------------------------------------
#
# One leaf per non-settled :mod:`application_sdk.testing.harness.outcome`
# variant, carrying that variant's fields rather than stringifying them into a
# message: the whole reason the primitive returns a structured verdict is that a
# report can group and count without re-parsing prose, and an adapter that
# raises must not throw that away on the way out.
#
# These are the *generic* leaves. ``testing/e2e``'s own
# ``NoWorkerOnTaskQueueError`` / ``AutomationEngineNotDispatchingError`` /
# ``DAGProgressStalledError`` stay exactly as they are, because their value is
# the connector remediation advice in their messages — which queue to check,
# which agent name resolves to it — and that advice is precisely what could not
# come along into a shared primitive. A caller that wants it matches on the
# outcome variant and raises its own leaf; a caller that just wants the wait to
# fail calls
# :func:`~application_sdk.testing.harness.outcome.assert_settled` and gets these.


@dataclass(kw_only=True)
class WaitNeverStartedError(PreconditionError):
    """A bounded wait's start-grace window closed with nothing having started.

    ``PreconditionError`` rather than a timeout, matching the two e2e leaves
    that guard the same moment: the budget did not run out, the state that had
    to exist before the work could begin never did (no poller on the task queue,
    a queue-name mismatch, a worker scaled to zero). Retrying the same call
    without changing that state is not expected to help, which is the
    category's own litmus test.

    Attributes:
        label: What was being waited on, verbatim from the outcome.
        grace_seconds: The start-grace window that closed without a start.
        attempts: How many times the probe ran.
        elapsed_seconds: Wall-clock time the wait consumed.
    """

    code: ClassVar[str] = "PRECONDITION_WAIT_NEVER_STARTED"
    component: str | None = "harness_waiting"
    label: str | None = None
    grace_seconds: float | None = None
    attempts: int | None = None
    elapsed_seconds: float | None = None


@dataclass(kw_only=True)
class WaitStalledError(AppTimeoutError):
    """Work started, then stopped making observable progress.

    A ``TIMEOUT``-category leaf, following :class:`TaskStalledError`'s reasoning
    (ADR-0018): a stall *is* a bounded wait that elapsed, just a wait for the
    next change rather than for the end. ``testing/e2e``'s
    ``DAGProgressStalledError`` calls the same condition a ``PRECONDITION``; it
    predates the ADR and keeps its category, and FND-240 declined to normalise
    the pair — see that class for why a ``code`` change is an error-contract
    change rather than a refactor.

    Attributes:
        label: What was being waited on, verbatim from the outcome.
        fingerprint: The progress fingerprint that stopped changing — the single
            most useful field, because it says *what* froze.
        stall_window_seconds: How long the fingerprint went unchanged before the
            watchdog fired.
        attempts: How many times the probe ran.
    """

    code: ClassVar[str] = "TIMEOUT_WAIT_STALLED"
    component: str | None = "harness_waiting"
    label: str | None = None
    fingerprint: str | None = None
    stall_window_seconds: float | None = None
    attempts: int | None = None


@dataclass(kw_only=True)
class WaitExpiredError(AppTimeoutError):
    """A bounded wait spent its whole budget while work was still progressing.

    The plain timeout, and the only one of the four that says nothing about
    *why*: the work was moving, it was simply not finished. ``timeout_seconds``
    and ``elapsed_seconds`` come from :class:`AppTimeoutError`.

    Attributes:
        label: What was being waited on, verbatim from the outcome.
        attempts: How many times the probe ran.
    """

    code: ClassVar[str] = "TIMEOUT_WAIT_EXPIRED"
    component: str | None = "harness_waiting"
    label: str | None = None
    attempts: int | None = None


@dataclass(kw_only=True)
class WaitIndeterminateError(DependencyUnavailableError):
    """The wait reached no verdict, because the probe itself could not be read.

    ``DEPENDENCY_UNAVAILABLE`` is the load-bearing choice here, not a category
    of convenience: an expired vcluster token, a dropped tunnel or a 503 from
    Atlas is neither a pass nor a regression in the thing under test, and this
    is the category whose own definition is "the same call would work once the
    dependency recovers". A caller grading a suite can therefore separate
    "could not tell" from "told, and it was bad" on the category alone. See
    :mod:`application_sdk.testing.harness.outcome` for why the verdict exists
    at all.

    The cause is preserved as ``__cause__`` (``raise ... from``) as well as
    summarised in the message, so a caller can still classify which backend and
    which transport failed.

    Attributes:
        label: What was being waited on, verbatim from the outcome.
        attempts: How many times the probe ran.
        elapsed_seconds: Wall-clock time the wait consumed.
        transient_failures: How many probe errors were absorbed before the wait
            gave up.
    """

    code: ClassVar[str] = "DEPENDENCY_UNAVAILABLE_WAIT_INDETERMINATE"
    component: str | None = "harness_waiting"
    label: str | None = None
    attempts: int | None = None
    elapsed_seconds: float | None = None
    transient_failures: int | None = None
