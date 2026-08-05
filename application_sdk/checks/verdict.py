"""The result of a check run — deliberately secret-free.

Separate from the handler's ``PreflightOutput`` because a caller needs to know two
different things: what the source said (the handler's verdict) and whether we were
in a position to ask at all (the classification). Collapsing those is how a broken
secret store came to be reported as an unready source.

Carries no credentials and no request envelope, only scalars and the handler's own
output, so it is safe to log, serialize into a Temporal payload, or attach to an
error without a redaction pass.
"""

from __future__ import annotations

from pydantic import BaseModel

from application_sdk.checks.request import CheckTrigger
from application_sdk.contracts.base import SerializableEnum
from application_sdk.handler.contracts import (
    PreflightCheck,
    PreflightOutput,
    PreflightStatus,
)


class CheckClassification(SerializableEnum):
    """Why the run ended where it did — the thing enforcement keys on.

    The distinction that makes hard mode safe: only :attr:`VERDICT` and
    :attr:`SOURCE_UNVERIFIABLE` are statements about the customer's source.
    :attr:`GATE_BROKEN` is a statement about us, and must never block a run.
    """

    VERDICT = "verdict"
    """The handler reached a real conclusion. Stamped explicitly rather than left
    absent, because "field missing" would otherwise have to mean both "genuine
    verdict" and "this row predates the classification"."""

    SOURCE_UNVERIFIABLE = "source_unverifiable"
    """We could not get an answer *about the source*: it overran the budget, the
    handler crashed, or the credential is provably absent. Attributable, and so
    subject to gate mode."""

    GATE_BROKEN = "gate_broken"
    """Our own plumbing failed — secret store down, rate limited, cancelled. Never
    attributable to the source, so the caller fails open regardless of posture."""


class CheckVerdict(BaseModel):
    """A completed check run, with everything needed to report it."""

    output: PreflightOutput
    """The handler's own verdict — status, per-check results, message."""

    classification: CheckClassification = CheckClassification.VERDICT
    """Whether this reflects the source or our ability to reach it."""

    app_name: str = ""
    entrypoint: str = ""
    trigger: CheckTrigger = CheckTrigger.UI_PREFLIGHT

    duration_ms: float = 0.0
    """Measured by the runner, not summed from per-check durations: those are
    handler-authored, and a handler abandoned at its deadline keeps running and
    reports a duration past the budget."""

    budget_seconds: int = 0
    """The budget this run was granted, as enforced."""

    attempt: int = 1
    """Which attempt produced this, for callers with a retry policy."""

    checks_run: int = 0
    """How many checks the handler was asked for after depth/name filtering.

    Distinct from ``len(checks)``: a handler that ignores the filter returns more
    than were requested, and that difference is worth being able to see.
    """

    @property
    def status(self) -> PreflightStatus:
        """The handler's verdict."""
        return self.output.status

    @property
    def checks(self) -> list[PreflightCheck]:
        """Per-check results, in handler order."""
        return self.output.checks

    @property
    def failed_checks(self) -> list[PreflightCheck]:
        """Only the checks that did not pass, in handler order."""
        return [c for c in self.output.checks if not c.passed]

    @property
    def is_ready(self) -> bool:
        """Whether the run may proceed on this verdict.

        ``PARTIAL`` counts as ready: it means an advisory check failed and the
        handler decided the run can continue anyway.
        """
        return self.output.status is not PreflightStatus.NOT_READY
