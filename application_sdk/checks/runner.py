"""Running a check — the one implementation, for every caller.

Lifted from the pre-run gate, which was the only path that got this right. The
things it does that the other three did not do at all are the whole point:

* **Enforces the budget itself**, net of credential resolution, rather than trusting
  the handler to self-police or letting an outer timeout kill the frame. If the
  caller's deadline fires first there is no frame left to classify the failure in —
  that was the CNCT-99 defect.
* **Separates "the source is not ready" from "we could not ask"**, so a slow secret
  store can never be reported to a customer as a bad credential.
* **Never awaits a handler it has abandoned.** ``asyncio.wait_for`` cancels and then
  *awaits*, so a handler that swallows ``CancelledError`` either returns a value
  (and the budget is never enforced) or runs past the outer deadline (and the
  classification is lost). Waiting on the task instead lets us classify *at* the
  deadline whatever the handler does.

What this module deliberately does not do: enforce. It returns a verdict and a
classification; whether a ``NOT_READY`` aborts anything is the caller's decision
(only the pre-run gate has a posture to apply). Verdict and enforcement stay
separate concerns, and the handler is never consulted about either.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from typing import TYPE_CHECKING

from application_sdk.checks import credentials as check_credentials
from application_sdk.checks.depth import CheckDepth, selected
from application_sdk.checks.projections import is_block
from application_sdk.checks.request import CheckRequest
from application_sdk.checks.verdict import CheckClassification, CheckVerdict
from application_sdk.errors.base import AppError, sanitize_cause_repr
from application_sdk.errors.categories import FailureCategory
from application_sdk.errors.leaves import (
    AppTimeoutError,
    DependencyUnavailableError,
    InternalError,
)
from application_sdk.errors.wire import FailureDetails
from application_sdk.handler.context import (
    HandlerContext,
    bind_handler_context,
    bind_invocation_context,
)
from application_sdk.handler.contracts import (
    HandlerCredential,
    PreflightCheck,
    PreflightOutput,
    PreflightStatus,
)
from application_sdk.infrastructure.context import get_infrastructure
from application_sdk.observability.logger_adaptor import get_logger

# Imported at module level, not lazily inside the augmentation, so the probe is a
# patchable seam: the SDR suite has stubbed it since before consolidation, and it is
# the only way to exercise the store-failure rows without a real object store.
from application_sdk.storage.preflight import check_object_store_access

if TYPE_CHECKING:
    from application_sdk.handler.base import Handler

logger = get_logger(__name__)

# Synthetic check name for a no-verdict outcome, so the check matrix and the red
# activity pane carry a row rather than showing zero checks.
UNVERIFIABLE_CHECK_NAME = "preflightVerdict"

# Floor on what's left after credential resolution. Below this there is no point
# calling the handler: resolution has eaten the budget, which is a plumbing problem,
# not evidence about the source. Without this floor a slow vault would hand the
# handler a sliver of budget, the handler would time out, and the resulting block
# would blame the source for the secret store being slow.
MIN_HANDLER_SECONDS = 1.0

# ``FailureCategory`` already draws the line this needs: DEPENDENCY_UNAVAILABLE is
# documented as Atlan-internal platform services while SOURCE_UNAVAILABLE is the
# customer's own system (see errors/categories.py). RATE_LIMITED joins the plumbing
# side because a 429 means "ask me later", not "the source is not ready" —
# collapsing it into a verdict would make hard mode fail *closed* on a transient.
_PLUMBING_CATEGORIES: frozenset[FailureCategory] = frozenset(
    {
        FailureCategory.DEPENDENCY_UNAVAILABLE,
        FailureCategory.RATE_LIMITED,
        FailureCategory.RESOURCE_EXHAUSTED,
        FailureCategory.CANCELLED,
    }
)

# UI-facing check-row names for the object-store access probes. "deployment" is the
# customer's own store; "upstream" is the Atlan upload proxy.
_OBJECT_STORE_CHECK_NAMES: dict[str, str] = {
    "deployment": "Object store access (deployment)",
    "upstream": "Object store access (Atlan upload)",
}


def is_plumbing_failure(exc: BaseException) -> bool:
    """Whether ``exc`` is our own infrastructure failing, not source evidence.

    Routed off the raised error's own ``FailureCategory`` rather than a list of
    exception classes, so an app raising any typed SDK error lands on the right side
    without this module knowing about it.

    An **untyped** exception is deliberately *not* treated as plumbing: a handler
    crash is an app fault we can attribute, and defaulting it to fail-open is what
    let hard mode mean nothing.
    """
    return isinstance(exc, AppError) and type(exc).category in _PLUMBING_CATEGORIES


def min_handler_seconds(budget_seconds: float) -> float:
    """The post-resolution floor, capped at half the budget.

    The floor exists to reject a *sliver* of remaining budget. It must never exceed
    half of what was granted, or it would reject budgets it was meant to accept and
    turn every run into a fail-open.
    """
    return min(MIN_HANDLER_SECONDS, budget_seconds / 2)


def unverifiable_output(exc: BaseException, app_name: str) -> PreflightOutput:
    """Build the ``NOT_READY`` verdict for a source we could not verify.

    Shaped as a normal handler verdict with one failed check so the existing
    block/emit machinery applies unchanged — a no-verdict outcome reports through
    the same surfaces as a real one instead of needing a parallel path.
    """
    if isinstance(exc, AppError):
        details = exc.to_failure_details()
        if details.app_name is None:
            details = details.model_copy(update={"app_name": app_name})
    else:
        # An untyped handler crash is an app fault, not a timeout — INTERNAL with
        # classification_pending is what the taxonomy has for "unclassified, needs a
        # typed leaf". Labelling it TIMEOUT would report every AttributeError to the
        # Automation Engine as a slow source.
        #
        # The raw message is sanitized, never interpolated: a driver exception
        # routinely carries the connection string (see credentials/errors.py), and
        # this text lands on FailureDetails.message, in Temporal history, and in
        # ClickHouse. to_failure_details() sanitizes cause_repr but not message.
        details = InternalError(
            message=f"Preflight could not be verified: {sanitize_cause_repr(exc)}",
            app_name=app_name,
            cause=exc,
            retryable=False,
            component="preflight_handler",
            classification_pending=True,
        ).to_failure_details()
    return PreflightOutput(
        status=PreflightStatus.NOT_READY,
        message=details.message,
        checks=[
            PreflightCheck(name=UNVERIFIABLE_CHECK_NAME, passed=False, error=details)
        ],
    )


async def append_object_store_checks(output: PreflightOutput) -> None:
    """Fold the object-store access probe into a handler's verdict.

    The app's own artifact-write path is a precondition for a run just as much as
    the source is: if the store is unwritable, every run fails deep inside a
    workflow instead of at the check that should have caught it. This used to run
    only on the interactive SDR path, so the pre-run gate — the one caller in a
    position to stop the doomed run — never saw it.

    Currently meaningful in SDR deployments only: the underlying probe returns ``[]``
    when ``ENABLE_ATLAN_UPLOAD`` is falsy. Extending it to cover the deployment
    store in non-SDR deployments is a change to the probe, not to this wiring.

    Never raises — the handler's own result must survive a broken augmentation.
    """
    try:
        start = time.monotonic()
        results = await check_object_store_access(get_infrastructure())
        elapsed_ms = (time.monotonic() - start) * 1000.0
        if not results:
            return

        any_failed = False
        for result in results:
            name = _OBJECT_STORE_CHECK_NAMES.get(
                result.label, f"Object store access ({result.label})"
            )
            error: FailureDetails | None = None
            if not result.passed:
                any_failed = True
                error = FailureDetails(
                    category=FailureCategory.DEPENDENCY_UNAVAILABLE,
                    code="OBJECT_STORE_ACCESS",
                    retryable=False,
                    message=result.message,
                    suggested_action=result.hint,
                )
            output.checks.append(
                PreflightCheck(
                    name=name,
                    passed=result.passed,
                    message=result.message,
                    error=error,
                    depth=CheckDepth.PERMISSIONS,
                )
            )

        output.total_duration_ms += elapsed_ms
        if any_failed and output.status == PreflightStatus.READY:
            output.status = PreflightStatus.NOT_READY
    except Exception:
        # Must never break the handler's own preflight result.
        logger.warning(
            "Object-store access check augmentation failed; leaving the handler "
            "result unchanged",
            exc_info=True,
        )


def apply_depth_cap(output: PreflightOutput, depth: CheckDepth) -> PreflightOutput:
    """Drop checks deeper than the caller asked for, and re-derive the status.

    A no-op at :attr:`CheckDepth.FULL`, which is every existing caller — so no
    established path changes shape. Above that it exists because a handler is free
    to ignore ``PreflightInput.depth``: an old handler asked for ``AUTH`` returns
    everything it always ran, and honouring the caller's cap has to be enforceable
    here rather than assumed there.

    The status is re-derived from the retained checks, since the handler's own
    verdict may have been ``NOT_READY`` because of a check that is now out of scope.
    That is the correct answer to the narrower question actually asked — "is the
    credential still good" is not answered "no" by an unrelated permission gap —
    and it is why capping is opt-in per caller rather than a default.
    """
    if depth is CheckDepth.FULL:
        return output
    keep = selected(depth, [c.depth for c in output.checks])
    retained = [c for c, k in zip(output.checks, keep, strict=True) if k]
    if len(retained) == len(output.checks):
        return output
    status = output.status
    if any(not c.passed for c in retained):
        status = PreflightStatus.NOT_READY
    elif status is PreflightStatus.NOT_READY:
        # Everything still in scope passed; the block was for a dropped check.
        status = PreflightStatus.READY
    return output.model_copy(update={"status": status, "checks": retained})


async def run_checks(
    handler: Handler,
    request: CheckRequest,
    *,
    enforced_deadline_seconds: float | None = None,
    attempt: int = 1,
    augment_object_store: bool = True,
    context: HandlerContext | None = None,
) -> CheckVerdict:
    """Resolve credentials, run the handler's checks, and classify the result.

    Args:
        handler: The app's handler. Called exactly once.
        request: The normalized request — see :class:`CheckRequest`.
        enforced_deadline_seconds: A deadline the *caller's* transport will enforce
            (a Temporal activity's ``start_to_close``). The budget is capped to sit
            inside it so this function's own timeout always wins the race; if the
            transport won, the frame would be killed before it could classify
            anything. ``None`` when nothing outside is holding a stopwatch.
        attempt: Which attempt this is, for reporting only.
        augment_object_store: Fold in the object-store probe. On by default; the
            caller can decline when it has already run one.
        context: An existing :class:`HandlerContext` to run inside, with the resolved
            credentials merged in. The HTTP path passes the context it already built
            so its ``request_id`` — which its own log lines quote — stays the one the
            handler sees; binding a fresh context there would silently break request
            correlation. ``None`` (worker paths) builds one per invocation.

    Returns:
        A :class:`CheckVerdict`, always — including for a source we could not
        verify, which comes back as ``NOT_READY`` classified
        :attr:`CheckClassification.SOURCE_UNVERIFIABLE`.

    Raises:
        Only plumbing failures propagate: our secret store, our transport, our
        rate limits. The caller is expected to fail open on these, because they are
        not evidence about the customer's source.
    """
    started = time.monotonic()
    budget = _capped_budget(request.budget_seconds, enforced_deadline_seconds)

    def _verdict(
        output: PreflightOutput,
        classification: CheckClassification,
        checks_run: int = 0,
    ) -> CheckVerdict:
        return CheckVerdict(
            output=output,
            classification=classification,
            app_name=request.app_name,
            entrypoint=request.entrypoint,
            trigger=request.trigger,
            duration_ms=(time.monotonic() - started) * 1000.0,
            budget_seconds=int(request.budget_seconds),
            attempt=attempt,
            checks_run=checks_run,
        )

    try:
        creds, creds_by_name = await check_credentials.resolve(
            request.credential_source
        )
    except Exception as e:
        # Resolution is our plumbing, so the default here is the opposite of the
        # handler path below: only a *provable* credential absence is a config fact
        # this run can be blamed for. Everything else — including the resolver's
        # collapsed "unexpected vault error" not-founds — propagates and fails open.
        if not check_credentials.is_definitive_credential_absence(e):
            raise
        return _verdict(
            unverifiable_output(e, request.app_name),
            CheckClassification.SOURCE_UNVERIFIABLE,
        )

    remaining = budget - (time.monotonic() - started)
    if remaining < min_handler_seconds(budget):
        # Resolution ate the budget. That is the secret store being slow, not the
        # source being unready — fail open rather than calling the handler with no
        # time and blaming it for the timeout.
        raise DependencyUnavailableError(
            message=(
                f"Credential resolution consumed the entire preflight budget "
                f"({budget:.0f}s); no time left to verify the source"
            ),
            service="secret_store",
        )

    # Floor of what's left, and the one number the handler is told — the timeout
    # message quotes it too, so what we enforce, what we report, and what we blame
    # are all the same value.
    handler_budget = max(1, int(remaining))
    preflight_input = request.to_preflight_input(creds, creds_by_name, handler_budget)

    all_creds = [
        *creds,
        *(c for group in creds_by_name.values() for c in group),
    ]
    # The overrun is handled *after* the try closes, not inside it: on the enforcing
    # caller's path the handling can raise, and a raise inside this block would be
    # re-caught below and reclassified — double-emitting the outcome row. Hence the
    # flag rather than an early return.
    output: PreflightOutput | None = None
    try:
        with _bound_context(request.app_name, all_creds, context):
            check = asyncio.ensure_future(handler.preflight_check(preflight_input))
            done, _ = await asyncio.wait({check}, timeout=remaining)
            if done:
                output = check.result()
            else:
                # Ask it to stop, but never await it — an uncooperative handler must
                # not be able to hold this frame open.
                check.cancel()
                # Abandoning a task without awaiting leaves any exception it later
                # raises unretrieved, which asyncio logs on GC. Consume it (same
                # fire-and-forget idiom as execution/heartbeat.py).
                check.add_done_callback(
                    lambda f: None if f.cancelled() else f.exception()
                )
    except Exception as e:
        # A handler that raises the caller's own block marker already carries a
        # verdict and its own emitted row; pass it straight through rather than
        # reinterpreting it as a source we could not verify.
        if is_plumbing_failure(e) or is_block(e):
            raise
        return _verdict(
            unverifiable_output(e, request.app_name),
            CheckClassification.SOURCE_UNVERIFIABLE,
        )

    if output is None:
        return _verdict(
            unverifiable_output(
                AppTimeoutError(
                    message=(
                        f"Preflight checks did not finish within the "
                        f"{handler_budget}s budget"
                    ),
                    app_name=request.app_name,
                    retryable=False,
                ),
                request.app_name,
            ),
            CheckClassification.SOURCE_UNVERIFIABLE,
        )

    if augment_object_store:
        await append_object_store_checks(output)
    output = apply_depth_cap(output, request.depth)
    return _verdict(output, CheckClassification.VERDICT, checks_run=len(output.checks))


@contextmanager
def _bound_context(
    app_name: str,
    credentials: list[HandlerCredential],
    existing: HandlerContext | None,
) -> Iterator[HandlerContext]:
    """Bind the context the handler will run inside.

    With no ``existing`` context this is exactly ``bind_invocation_context`` — the
    worker paths' behaviour, unchanged. With one, it rebinds *that* context carrying
    the resolved credentials, so the caller's identity fields (``request_id``,
    ``started_at``) survive: the HTTP routes log a request id and a handler reading
    ``self.context.request_id`` must see the same one, which a freshly built context
    would quietly break.
    """
    if existing is None:
        with bind_invocation_context(app_name, credentials) as ctx:
            yield ctx
        return
    merged = replace(existing, _credentials=list(credentials))
    with bind_handler_context(merged) as ctx:
        yield ctx


def _capped_budget(
    budget_seconds: float, enforced_deadline_seconds: float | None
) -> float:
    """The handler budget, capped by a deadline the caller's transport enforces.

    Two independent reads of the same declared budget can skew — during a rolling
    deploy, or when a worker cannot resolve the app class and falls back to the
    default. If the transport's deadline turns out to be the tighter one, it would
    kill this frame before the internal timeout fired and the classification would
    be lost. Taking the minimum makes "our timeout wins" true by construction.
    """
    if enforced_deadline_seconds is None or enforced_deadline_seconds <= 0:
        return budget_seconds
    return min(budget_seconds, enforced_deadline_seconds)
