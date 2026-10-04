"""A scripted source that warms up, for gate and app tests with no warehouse.

An app that declares a warmup (``App.preflight_warmup_ceiling_seconds``) has
checks that cannot run until the source is made ready: a suspended warehouse
resumed, a job queue drained. Testing that against a real source means paying
for the resume and waiting minutes for it, and the interesting paths — queued
behind other work, never coming back — cannot be produced on demand at all.

:class:`WarmingSource` plays that source from a script instead. The script is
the sequence of states the source passes through, and each state is either a
:class:`SourceState`, a literal :class:`~application_sdk.handler.contracts.WarmupState`
for a case the vocabulary does not cover, or an exception to raise::

    source = WarmingSource(
        [SourceState.COLD, SourceState.WARMING, SourceState.QUEUED, SourceState.READY]
    )

The source behaves the way the warmup contract asks a real one to:

* Until :meth:`WarmingSource.start` is called it stays on the first state, and
  :meth:`WarmingSource.poll` reports that state without moving. ``warmup_state``
  must not start a warmup, so a cold source polled without a start stays cold.
* ``start`` moves to the next state. A second ``start`` reports where the
  source is and moves nothing — ``warmup_start`` must be idempotent.
* Each ``poll`` after the start moves one state on. The last state repeats once
  the script runs out, so a script ending in ``WARMING`` warms forever and
  reaches the gate's ceiling.

Two ways to use it:

* **Gate tests.** :class:`WarmingSourceHandler` is a complete handler around a
  source: its ``warmup_start`` / ``warmup_state`` drive the script and its
  ``preflight_check`` answers one ``FAST`` row and one ``WARMUP`` row. Hand it
  to a worker and the gate runs against it.
* **App tests.** Back the app's own source-client fake with a source, so the
  app's real ``warmup_start`` / ``warmup_state`` run against a scripted
  warehouse: stub the client's resume call with :meth:`WarmingSource.start` and
  its state call with :meth:`WarmingSource.poll`, then assert on the
  :class:`~application_sdk.handler.contracts.WarmupState` the hooks return.

Everything is in-process and synchronous underneath: no timers, no threads, no
sockets. A gate test's waiting comes from the gate's own durable timers, not
from this fake.
"""

from __future__ import annotations

from collections.abc import Sequence

from application_sdk.contracts.base import SerializableEnum
from application_sdk.errors.leaves import PreconditionError, SourceUnavailableError
from application_sdk.handler.base import DefaultHandler
from application_sdk.handler.contracts import (
    CheckTier,
    PreflightCheck,
    PreflightInput,
    PreflightOutput,
    PreflightStatus,
    WarmupState,
    WarmupStatus,
)
from application_sdk.testing._errors import WarmingScriptEmptyError

__all__ = [
    "SourceState",
    "WarmingSource",
    "WarmingSourceHandler",
    "WarmingStep",
]


class SourceState(SerializableEnum):
    """Where a warming source is, in the words a warehouse would use.

    :attr:`warmup_status` maps each onto the
    :class:`~application_sdk.handler.contracts.WarmupStatus` a handler reports.
    ``WARMING`` and ``QUEUED`` both map to ``RUNNING``: the gate waits on
    either the same way, and only the message (the state's name, which the
    gate puts on the run's health line) tells them apart.
    """

    COLD = "cold"
    """Suspended or stopped; nothing has asked it to resume."""

    WARMING = "warming"
    """Resuming: compute is being provisioned."""

    QUEUED = "queued"
    """Up, but the warmup is waiting behind other work for a slot."""

    READY = "ready"
    """Warm; ``WARMUP``-tier checks can run."""

    UNAVAILABLE = "unavailable"
    """Will not come back: decommissioned, out of credit, region down."""

    @property
    def warmup_status(self) -> WarmupStatus:
        """The ``WarmupStatus`` a handler reports for this state."""
        return _WARMUP_STATUS[self]


_WARMUP_STATUS: dict[SourceState, WarmupStatus] = {
    SourceState.COLD: WarmupStatus.NOT_STARTED,
    SourceState.WARMING: WarmupStatus.RUNNING,
    SourceState.QUEUED: WarmupStatus.RUNNING,
    SourceState.READY: WarmupStatus.READY,
    SourceState.UNAVAILABLE: WarmupStatus.FAILED,
}

WarmingStep = SourceState | WarmupState | BaseException
"""One entry in a :class:`WarmingSource` script."""


class WarmingSource:
    """A source whose warmup follows a script. See the module docstring.

    Args:
        script: The states the source passes through, first one included.
            Must not be empty.
        pending_checks: Names of the ``WARMUP``-tier checks waiting on this
            source. Every state short of ``READY`` reports them as
            ``WarmupState.pending_checks``, which ``/check`` passes on to the UI.
    """

    def __init__(
        self,
        script: Sequence[WarmingStep],
        *,
        pending_checks: Sequence[str] = (),
    ) -> None:
        if not script:
            raise WarmingScriptEmptyError(
                message="A WarmingSource script needs at least one state."
            )
        self._script: tuple[WarmingStep, ...] = tuple(script)
        self._pending_checks = list(pending_checks)
        self._cursor = 0
        self.started = False
        """Whether :meth:`start` has been called."""
        self.calls: list[str] = []
        """``"start"`` / ``"poll"`` per call, in call order."""
        self.reported: list[WarmupStatus] = []
        """The status every call returned, in call order. A call that raised
        adds nothing."""

    @property
    def polls(self) -> int:
        """How many times :meth:`poll` was called."""
        return self.calls.count("poll")

    @property
    def current(self) -> WarmingStep:
        """The step the source is on now."""
        return self._script[self._cursor]

    def start(self) -> WarmupState:
        """Start the warmup: move one state on, once. Later calls only report.

        Raises:
            BaseException: The script's step, when it is an exception.
        """
        self.calls.append("start")
        if not self.started:
            self.started = True
            self._advance()
        return self._play()

    def poll(self) -> WarmupState:
        """Report the state, moving one state on if the warmup was started.

        Raises:
            BaseException: The script's step, when it is an exception.
        """
        self.calls.append("poll")
        if self.started:
            self._advance()
        return self._play()

    def _advance(self) -> None:
        self._cursor = min(self._cursor + 1, len(self._script) - 1)

    def _play(self) -> WarmupState:
        step = self.current
        if isinstance(step, BaseException):
            raise step
        state = step if isinstance(step, WarmupState) else self._state_for(step)
        self.reported.append(state.status)
        return state

    def _state_for(self, source_state: SourceState) -> WarmupState:
        status = source_state.warmup_status
        pending = [] if status is WarmupStatus.READY else list(self._pending_checks)
        if source_state is SourceState.UNAVAILABLE:
            return WarmupState(
                status=status,
                pending_checks=pending,
                error=SourceUnavailableError(
                    message="Source is unavailable and did not warm up."
                ),
            )
        return WarmupState(
            status=status, message=source_state.name, pending_checks=pending
        )


class WarmingSourceHandler(DefaultHandler):
    """A handler around a :class:`WarmingSource`, for tests of the gate itself.

    ``warmup_start`` / ``warmup_state`` drive the source. ``preflight_check``
    answers one ``FAST`` row (:attr:`fast_check`, always passing) and one
    ``WARMUP`` row (:attr:`warmup_check`) whatever tier it is asked for, so the
    SDK's own tier filter is exercised too. :attr:`calls` records every gate
    call in order — ``"start"``, ``"state"``, ``"check:<tier>"`` (``"all"``
    for an untiered check) — so a test can assert what ran and in what order.

    Subclasses ``DefaultHandler`` for the auth and metadata no-ops only; a
    worker treats it as a real handler because its type is not
    ``DefaultHandler`` itself.

    Args:
        source: The scripted source.
        warmup_check_passes: Whether the ``WARMUP`` row passes. When it fails
            it carries a typed ``PreconditionError`` and the verdict is
            ``NOT_READY``.
        warmup_check_message: The failing ``WARMUP`` row's message.
    """

    fast_check = "reachable"
    warmup_check = "catalogScan"

    def __init__(
        self,
        source: WarmingSource,
        *,
        warmup_check_passes: bool = True,
        warmup_check_message: str = "catalog scan found no schemas",
    ) -> None:
        self.source = source
        self._warmup_check_passes = warmup_check_passes
        self._warmup_check_message = warmup_check_message
        self.calls: list[str] = []

    @property
    def polls(self) -> int:
        """How many times ``warmup_state`` was called."""
        return self.calls.count("state")

    @property
    def check_tiers(self) -> list[str]:
        """The tier of every ``preflight_check`` call, in call order."""
        return [c.split(":", 1)[1] for c in self.calls if c.startswith("check:")]

    async def warmup_start(self, input: PreflightInput) -> WarmupState:
        self.calls.append("start")
        return self.source.start()

    async def warmup_state(self, input: PreflightInput) -> WarmupState:
        self.calls.append("state")
        return self.source.poll()

    async def preflight_check(self, input: PreflightInput) -> PreflightOutput:
        self.calls.append(f"check:{input.tier.value if input.tier else 'all'}")
        passes = self._warmup_check_passes
        error = (
            None
            if passes
            else PreconditionError(
                message=self._warmup_check_message,
                suggested_action="Grant the connection access to at least one schema.",
            ).to_failure_details()
        )
        warmup_row = PreflightCheck(
            name=self.warmup_check, passed=passes, tier=CheckTier.WARMUP, error=error
        )
        return PreflightOutput(
            status=PreflightStatus.READY
            if passes or input.tier is CheckTier.FAST
            else PreflightStatus.NOT_READY,
            checks=[
                PreflightCheck(name=self.fast_check, passed=True, tier=CheckTier.FAST),
                warmup_row,
            ],
        )
