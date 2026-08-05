"""When to look again — a recommendation derived from the verdict alone.

The scheduler that runs proactive drift detection (today the Automation Engine)
owns the timer and the history. What it does not own is the *meaning* of a verdict:
whether a failure is the kind that usually clears on its own, whether an all-green
full sweep earns a long rest, whether a partial result deserves another look sooner.
That is check semantics, and it belongs with the checks.

So this module answers one question — "given this verdict, how long until the next
check is worth running?" — and the caller applies it. Deliberately **stateless**: no
consecutive-green counter, no per-connection memory. A pure function of one verdict
cannot drift out of sync with a history it does not hold, and the caller already has
that history.

The adaptive behaviour comes from applying it repeatedly: a failing connection keeps
earning short intervals until it passes, then earns the long one. A caller wanting a
gentler ramp can lengthen its own interval up to this ceiling; a caller wanting the
recommendation ignored can ignore it.
"""

from __future__ import annotations

from application_sdk.checks.depth import CheckDepth
from application_sdk.checks.verdict import CheckClassification, CheckVerdict
from application_sdk.handler.contracts import PreflightStatus

# A credential or a grant that has actually broken is worth re-checking soon: the
# customer is likely fixing it right now, and the sooner a green verdict lands the
# sooner the connection stops being flagged.
RECHECK_AFTER_FAILURE_SECONDS = 15 * 60

# We could not reach a verdict — our own probe timed out, or the handler crashed.
# Shorter than a real failure: this says nothing about the source, so the priority is
# getting an actual answer rather than reacting to a non-answer.
RECHECK_AFTER_NO_VERDICT_SECONDS = 5 * 60

# Something advisory failed but the run may proceed. Between the two: worth watching,
# not worth hammering.
RECHECK_AFTER_PARTIAL_SECONDS = 60 * 60

# Everything the handler knows how to check passed. The long rest — this is the
# interval that keeps proactive checking affordable across a fleet, since it is the
# one the overwhelming majority of connections sit at.
RECHECK_AFTER_FULL_PASS_SECONDS = 24 * 60 * 60

# Everything passed, but the run was capped below FULL, so the parts that were not
# examined are still unknown. Much shorter than a full pass earns — a green AUTH
# check is not evidence that permissions are intact.
RECHECK_AFTER_SHALLOW_PASS_SECONDS = 4 * 60 * 60


def recheck_after_seconds(verdict: CheckVerdict) -> int:
    """How long to wait before checking this connection again.

    A handler that sets ``PreflightOutput.recheck_after_seconds`` itself wins: it
    knows things about its own source that this cannot (a token with a known expiry,
    a source with a documented maintenance window).
    """
    declared = verdict.output.recheck_after_seconds
    if declared is not None and declared > 0:
        return declared
    if verdict.classification is not CheckClassification.VERDICT:
        return RECHECK_AFTER_NO_VERDICT_SECONDS
    if verdict.status is PreflightStatus.NOT_READY:
        return RECHECK_AFTER_FAILURE_SECONDS
    if verdict.status is PreflightStatus.PARTIAL:
        return RECHECK_AFTER_PARTIAL_SECONDS
    # A pass only earns the long interval if it was a *full* pass; anything
    # shallower left questions unasked.
    return (
        RECHECK_AFTER_FULL_PASS_SECONDS
        if _was_full(verdict)
        else RECHECK_AFTER_SHALLOW_PASS_SECONDS
    )


def _was_full(verdict: CheckVerdict) -> bool:
    """Whether this run actually examined everything.

    Read off the checks the handler returned rather than the depth requested: a
    handler that returns nothing has not verified anything, whatever was asked of it,
    and treating that as a full pass would hand a silent no-op connection the longest
    possible interval.
    """
    if not verdict.checks:
        return False
    return any(c.depth is CheckDepth.FULL or c.depth is None for c in verdict.checks)
