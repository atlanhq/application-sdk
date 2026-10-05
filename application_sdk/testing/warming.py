"""A scripted source that warms up, for gate and app tests with no warehouse.

An app with a warmup (it overrides ``Handler.warmup``) has checks that cannot
run until the source's compute is ready: a suspended warehouse resumed, a job
queue drained. Testing that against a real source means paying for the resume
and waiting minutes for it, and the interesting paths — queued behind other
work, never coming back — cannot be produced on demand at all.

:class:`WarmingSource` plays that source from a script instead. The script is
the sequence of answers the source gives, one per probe, and each step is a
:class:`~application_sdk.handler.contracts.WarmupState`, a full
:class:`~application_sdk.handler.contracts.WarmupObservation` (to script a
``source_state``, queue depth or poll hint), or an exception to raise::

    source = WarmingSource(
        [WarmupState.COLD, WarmupState.WARMING, WarmupState.QUEUED, WarmupState.READY]
    )

The source behaves the way the warmup contract asks a real one to: stateless,
each :meth:`WarmingSource.probe` both pushes the warmup forward and reports
where it is, so each probe answers the next step. The last step repeats once
the script runs out, so a script ending in ``WARMING`` warms forever and reaches
the gate's ceiling.

Two ways to use it:

* **Gate tests.** :class:`WarmingSourceHandler` is a complete handler around a
  source: its ``warmup`` probes the script and its ``preflight_check`` answers
  one ``PREFLIGHT`` row and one ``WARMUP`` row, each only when its tier is
  requested. Hand it to a worker and the gate runs against it.
* **App tests.** Back the app's own source-client fake with a source, so the
  app's real ``warmup`` runs against a scripted warehouse: stub the client's
  probe query with :meth:`WarmingSource.probe`, then assert on the
  :class:`~application_sdk.handler.contracts.WarmupObservation` the app returns.

Everything is in-process and synchronous underneath: no timers, no threads, no
sockets. A gate test's waiting comes from the gate's own durable timers, not
from this fake.
"""

from __future__ import annotations

from collections.abc import Sequence

from application_sdk.errors.leaves import PreconditionError
from application_sdk.handler.base import DefaultHandler
from application_sdk.handler.contracts import (
    CheckTier,
    PreflightCheck,
    PreflightInput,
    PreflightOutput,
    PreflightStatus,
    WarmupInput,
    WarmupObservation,
    WarmupState,
)
from application_sdk.testing._errors import WarmingScriptEmptyError

__all__ = [
    "WarmingSource",
    "WarmingSourceHandler",
    "WarmingStep",
]

WarmingStep = WarmupState | WarmupObservation | BaseException
"""One entry in a :class:`WarmingSource` script."""


class WarmingSource:
    """A source whose warmup follows a script. See the module docstring.

    Args:
        script: The answers the source gives, one per probe. Must not be empty.
    """

    def __init__(self, script: Sequence[WarmingStep]) -> None:
        if not script:
            raise WarmingScriptEmptyError(
                message="A WarmingSource script needs at least one state."
            )
        self._script: tuple[WarmingStep, ...] = tuple(script)
        self._cursor = 0
        self.probes = 0
        """How many times :meth:`probe` was called."""
        self.reported: list[WarmupState] = []
        """The state every probe returned, in call order. A probe that raised
        adds nothing."""

    @property
    def current(self) -> WarmingStep:
        """The step the next probe answers."""
        return self._script[self._cursor]

    def probe(self) -> WarmupObservation:
        """Answer the current step, then move one step on.

        Raises:
            BaseException: The script's step, when it is an exception.
        """
        self.probes += 1
        step = self.current
        self._cursor = min(self._cursor + 1, len(self._script) - 1)
        if isinstance(step, BaseException):
            raise step
        observation = (
            step
            if isinstance(step, WarmupObservation)
            else WarmupObservation(state=step, source_state=step.name)
        )
        self.reported.append(observation.state)
        return observation


class WarmingSourceHandler(DefaultHandler):
    """A handler around a :class:`WarmingSource`, for tests of the gate itself.

    ``warmup`` probes the source. ``preflight_check`` answers one ``PREFLIGHT``
    row (:attr:`preflight_check_name`, always passing) and one ``WARMUP`` row
    (:attr:`warmup_check_name`), each only when ``input.tiers`` asks for its
    tier — unless ``ignores_tiers``, which returns both whatever was asked, to
    exercise the SDK's post-call tier check. :attr:`calls` records every gate
    call in order — ``"probe"`` and ``"check:<tiers>"`` (``"preflight+warmup"``
    for every tier) — so a test can assert what ran and in what order.

    Subclasses ``DefaultHandler`` for the auth and metadata no-ops only; a
    worker treats it as a real handler because its type is not
    ``DefaultHandler`` itself.

    Args:
        source: The scripted source.
        warmup_check_passes: Whether the ``WARMUP`` row passes. When it fails
            it carries a typed ``PreconditionError`` and the verdict is
            ``NOT_READY``.
        warmup_check_message: The failing ``WARMUP`` row's message.
        ignores_tiers: Return both rows whatever tiers were requested.
    """

    preflight_check_name = "reachable"
    warmup_check_name = "catalogScan"

    def __init__(
        self,
        source: WarmingSource,
        *,
        warmup_check_passes: bool = True,
        warmup_check_message: str = "catalog scan found no schemas",
        ignores_tiers: bool = False,
    ) -> None:
        self.source = source
        self._warmup_check_passes = warmup_check_passes
        self._warmup_check_message = warmup_check_message
        self._ignores_tiers = ignores_tiers
        self.calls: list[str] = []

    @property
    def probes(self) -> int:
        """How many times ``warmup`` was called."""
        return self.calls.count("probe")

    @property
    def check_tiers(self) -> list[str]:
        """The requested tiers of every ``preflight_check`` call, in call order."""
        return [c.split(":", 1)[1] for c in self.calls if c.startswith("check:")]

    async def warmup(self, input: WarmupInput) -> WarmupObservation:
        self.calls.append("probe")
        return self.source.probe()

    async def preflight_check(self, input: PreflightInput) -> PreflightOutput:
        self.calls.append(
            "check:" + "+".join(sorted(tier.value for tier in input.tiers))
        )
        wanted = frozenset(CheckTier) if self._ignores_tiers else frozenset(input.tiers)
        checks: list[PreflightCheck] = []
        status = PreflightStatus.READY
        if CheckTier.PREFLIGHT in wanted:
            checks.append(PreflightCheck(name=self.preflight_check_name, passed=True))
        if CheckTier.WARMUP in wanted:
            passes = self._warmup_check_passes
            checks.append(
                PreflightCheck(
                    name=self.warmup_check_name,
                    passed=passes,
                    tier=CheckTier.WARMUP,
                    error=None
                    if passes
                    else PreconditionError(
                        message=self._warmup_check_message,
                        suggested_action=(
                            "Grant the connection access to at least one schema."
                        ),
                    ).to_failure_details(),
                )
            )
            if not passes:
                status = PreflightStatus.NOT_READY
        return PreflightOutput(status=status, checks=checks)
