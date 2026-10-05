"""How a preflight verdict is attributed and logged, for every surface that runs one.

Handler-side, so the ``/workflows/v1/check`` route can emit its outcome row
without importing worker code (FND-3280: imports run worker → handler only).
The injected gate (``execution/_temporal/preflight_gate.py``) and the SDR
activity import these from here; the gate re-exports them under their old names.

Holds the row vocabulary (:class:`PreflightSurface`, :class:`PreflightRowOutcome`,
the ``failure.*`` keys), the attribution ladder that turns a verdict into the one
:class:`~application_sdk.errors.wire.FailureDetails` a row names
(:func:`_primary_failure`, :func:`_proceeded_failure`, :func:`_failure_fields`),
the interactive-surface emitters, and the tier filter ``/check`` and the gate
share. Nothing here touches Temporal.
"""

from __future__ import annotations

import math
import warnings
from typing import Any

import orjson

from application_sdk.contracts.base import SerializableEnum
from application_sdk.errors.base import AppError, redact_and_cap, redact_secrets
from application_sdk.errors.categories import FailureCategory
from application_sdk.errors.leaves import PreconditionError
from application_sdk.errors.wire import FailureDetails
from application_sdk.handler.contracts import (
    CheckTier,
    PreflightCheck,
    PreflightOutput,
    PreflightStatus,
)
from application_sdk.observability.events import PREFLIGHT_CHECK_EVENT
from application_sdk.observability.logger_adaptor import (
    CHECK_MATRIX_KEY,
    PREFLIGHT_SURFACE_KEY,
    AtlanLoggerAdapter,
)

# Contract sentinel stamped as the primary FailureDetails.code on a fallback block
# (a handler that returned NOT_READY without a typed check error). It replaces the
# generic PRECONDITION code so the outcome event's ``reason`` distinguishes an
# un-migrated block from a typed one (whose reason is the handler error's own code,
# e.g. AUTH). category/audience/retryable are unchanged.
PREFLIGHT_FALLBACK_CODE = "PREFLIGHT_CHECK_FAILED"

# The check matrix for an outcome where no check ran — a skipped gate, or a
# fail-open the workflow reports without ever seeing the activity's result.
# Emitted rather than omitted so ``check_matrix`` is present on *every* outcome:
# a consumer can then parse it unconditionally instead of branching on presence,
# and a branch mishandled in the dropping direction is how a gate that never
# reached a verdict vanishes from the numerator it belongs in.
EMPTY_CHECK_MATRIX = "[]"


# Who must act, stamped on the outcome row (FND-901). Same key and values the log
# interceptor projects from raised AppErrors, riding the OTel ``failure.``
# passthrough — but stamped here explicitly, because the row is emitted before
# the block is raised and would otherwise carry no audience.
FAILURE_AUDIENCE_KEY = "failure.audience"

# The failing check's name and its human line. Conditional, like
# FAILURE_AUDIENCE_KEY and unlike GATE_OUTCOME_ROW_KEYS: a row with nothing
# failed carries neither. ``reason`` is a code so dashboards can separate fault
# classes, and ``check_matrix`` deliberately holds no messages, so without these
# the sentence lived only on the adjacent "Completing activity as failed" record
# under ``exception.message`` — one record away, under a key nobody searches.
# They use the ``failure.`` prefix the logger already passes through, so neither
# needs an entry in ``_KNOWN_EXTRA_KEYS``.
FAILURE_CHECK_KEY = "failure.check"
FAILURE_MESSAGE_KEY = "failure.message"
# The envelope's own remediation line, when the handler gave one. The
# escalation's search included the remediation text; keeping it a distinct
# key means "what happened" and "what to do" stay separately queryable.
FAILURE_SUGGESTED_ACTION_KEY = "failure.suggested_action"


def _check_matrix_json(checks: list[PreflightCheck]) -> str:
    """Compact per-check matrix for the outcome event, as one JSON string.

    Lands as a single ``LogAttributes`` value in ClickHouse, so connector-pulse
    can pattern-match verdicts against workflow outcomes (``JSONExtract``) with
    no schema change. Small fixed fields only — messages and evidence stay in
    the Temporal activity result. Blocking intent is not a per-check field: it
    is observable from the outcome itself (``would_block``/``blocked`` means
    the aggregate was NOT_READY; a failed check on a ``proceeded`` run is
    advisory by the handler's own choice).

    ``tier`` appears only on a check whose handler set one, the same rule
    :meth:`PreflightCheck.to_wire` follows, so an app that never tiers its
    checks emits exactly the matrix it did before tiers existed.
    """
    rows = []
    for check in checks:
        row: dict[str, Any] = {
            "name": check.name,
            "passed": check.passed,
            "error_code": check.error.code if check.error else "",
            # Publish only a plausible elapsed time; nan/inf (orjson would
            # emit null) and negatives collapse to the -1.0 "not measured"
            # sentinel so the ClickHouse row stays numeric for JSONExtract
            # and garbage never reads as a real duration. Never raise — a
            # raise here fails the gate open and loses the whole event.
            "duration_ms": check.duration_ms
            if math.isfinite(check.duration_ms) and check.duration_ms >= 0
            else -1.0,
        }
        if check.tier is not None:
            row["tier"] = check.tier.value
        rows.append(row)
    return orjson.dumps(rows).decode()


def _primary_failure(result: PreflightOutput, app_name: str) -> FailureDetails:
    """The ``FailureDetails`` a NOT_READY-shaped verdict is attributed to.

    The handler's typed aggregate ``result.error`` when present, else the first
    failed check's typed ``error``, else the first failed check's message wrapped
    in ``PreconditionError`` and stamped with the ``PREFLIGHT_FALLBACK_CODE``
    sentinel ``code`` (so the outcome event's ``reason`` marks an un-migrated
    block). Prefers the aggregate because it is the reason the verdict is
    NOT_READY and stays pinned to the real cause even when a non-fatal row is
    inserted ahead of it in ``checks``.
    """
    failed = [c for c in result.checks if not c.passed]
    primary_error = result.error or next(
        (c.error for c in failed if c.error is not None), None
    )
    if primary_error is not None:
        return _stamped(primary_error, app_name)
    return _fallback_failure(_fallback_message(result), app_name)


def _fallback_message(result: PreflightOutput) -> str:
    """The sentence an untyped ``NOT_READY`` verdict is attributed to.

    The aggregate's own line first — a handler that sets ``result.message`` is
    describing the verdict, and the docstring on :attr:`PreflightOutput.error`
    has always promised this rung; every failed check's line joined next, so a
    multi-failure verdict loses none of them; a fixed line last. One source for
    two consumers: the untyped ``details[0].message`` (via
    :func:`_fallback_failure`) and the raised error's message in
    :func:`_gate_error`, so for an untyped verdict the row, the wire envelope,
    ``exception.message`` and the interceptor's ``Body`` line carry one string
    by construction. Not redacted here — each consumer redacts where it lands
    (the envelope validator; ``_gate_error`` explicitly).
    """
    failed = [c for c in result.checks if not c.passed]
    joined = "; ".join(m for m in (c.resolved_message for c in failed) if m)
    return result.resolved_message or joined or "Preflight check failed"


def _stamped(details: FailureDetails, app_name: str) -> FailureDetails:
    """``details`` with ``app_name`` filled in if the producer left it empty."""
    if details.app_name is None:
        return details.model_copy(update={"app_name": app_name})
    return details


def _fallback_failure(message: str, app_name: str) -> FailureDetails:
    """The synthesized primary for a failure nobody typed.

    ``PreconditionError`` carrying the untyped check's own line, stamped with
    the ``PREFLIGHT_FALLBACK_CODE`` sentinel so the row's ``reason`` marks an
    un-migrated handler rather than passing off a guess as a real code.
    """
    return (
        PreconditionError(
            message=message or "Preflight check failed",
            app_name=app_name,
            retryable=False,
        )
        .to_failure_details()
        .model_copy(update={"code": PREFLIGHT_FALLBACK_CODE})
    )


def _attributed_check(
    failed: list[PreflightCheck], primary: FailureDetails
) -> PreflightCheck | None:
    """The failed check ``primary`` describes, or ``None`` when that is a guess.

    Never identity. The workflow frame recovers its evidence off the failure
    chain, so the object it holds crossed the wire and is not the one any check
    carries; a handler may also hand one ``AppError`` to both the aggregate and
    a check, which ``PreflightOutput`` and ``PreflightCheck`` coerce separately
    into two distinct ``FailureDetails``. ``==`` fails both times, because
    :func:`_primary_failure` stamps ``app_name`` on the copy it returns.

    ``(code, message)`` survives both round-trips. A lone failed check is
    unambiguous whether or not it matches. Anything else — several failed
    checks and no match, or several matching equally — has no answer, and a
    wrong name is worse than none: it would contradict ``reason`` on the same
    row and send a reader after the wrong check.
    """
    matched = [
        c
        for c in failed
        if (
            c.error is not None
            and c.error.code == primary.code
            and c.error.message == primary.message
        )
        or (
            # An un-migrated check: the fallback primary was built from the
            # failed lines, so a check whose own line is that sentence is the
            # one it describes. Compared redacted, as the envelope stored it.
            c.error is None
            and bool(c.resolved_message)
            and redact_secrets(c.resolved_message) == primary.message
        )
    ]
    if len(matched) == 1:
        return matched[0]
    if not matched and len(failed) == 1:
        return failed[0]
    return None


def _failure_fields(
    checks: list[PreflightCheck], primary: FailureDetails | None
) -> dict[str, str]:
    """The attributed failure's human line, and the check it belongs to.

    ``primary`` is whatever the caller derived ``reason`` from —
    :func:`_primary_failure` for a block, :func:`_proceeded_failure` for a run
    that went ahead, the recovered evidence for a dead frame — or ``None`` when
    ``reason`` is just the status. Nothing is re-derived here, so the row cannot
    name a cause its own ``reason`` disagrees with.

    ``failure.message`` follows ``primary`` whenever there is one, checks or no
    checks: a lost frame has an empty check list and a fully populated primary,
    and it is the case a reader most needs the sentence for. The message is
    capped and redacted (the envelope already redacts; this is the cap, and a
    second pass costs nothing). ``failure.check`` is best-effort and may be
    absent — see :func:`_attributed_check`.
    """
    if primary is None:
        return {}
    fields: dict[str, str] = {}
    if primary.message:
        fields[FAILURE_MESSAGE_KEY] = redact_and_cap(primary.message)
    if primary.suggested_action:
        fields[FAILURE_SUGGESTED_ACTION_KEY] = redact_and_cap(primary.suggested_action)
    failed = [c for c in checks if not c.passed]
    named = _attributed_check(failed, primary) if failed else None
    if named is not None:
        fields[FAILURE_CHECK_KEY] = named.name
    return fields


def _proceeded_failure(result: PreflightOutput, app_name: str) -> FailureDetails | None:
    """The failure a proceeded row is attributed to, or ``None`` when none failed.

    A run that proceeds past a failed advisory check is the one the dashboards
    need to rank, and a reason of ``PARTIAL`` hides which check failed. So the
    *first* failed check is the attribution — its typed error, or the same
    fallback an untyped block gets — and the row's ``reason``, ``failure.check``
    and ``failure.message`` all come off this one object. Unlike
    :func:`_primary_failure` this does not prefer ``result.error``: a proceeded
    verdict's aggregate, when a handler sets one, describes why it proceeded,
    not which check failed.
    """
    failed = next((c for c in result.checks if not c.passed), None)
    if failed is None:
        return None
    if failed.error is not None:
        return _stamped(failed.error, app_name)
    return _fallback_failure(failed.resolved_message, app_name)


class PreflightRowOutcome(SerializableEnum):
    """The ``outcome`` vocabulary the SDK's own machinery stamps on preflight rows.

    Enumerated for the same reason :class:`PreflightSurface` is — dashboards
    filter on these wire strings, so the set must be discoverable and pinned,
    not scattered literals. The gate row (``Preflight gate outcome``) uses the
    first five; the interactive row (``Preflight check outcome``) uses
    :class:`PreflightStatus` values for verdicts and ``CRASHED`` for a handler
    that raised. Values are shipped wire strings and must not be reworded.
    """

    PROCEEDED = "proceeded"
    BLOCKED = "blocked"
    WOULD_BLOCK = "would_block"
    NO_VERDICT = "no_verdict"
    SKIPPED = "skipped"
    CRASHED = "crashed"
    CLIENT_FAULT = "client_fault"


class PreflightSurface(SerializableEnum):
    """Which surface ran ``Handler.preflight_check`` outside a gated run.

    Stamped as ``preflight_surface`` on the interactive outcome row, and the
    input to the level policy below — so this is an enumerated vocabulary, not
    free text. Values are the wire strings already shipped in that attribute
    and must not be reworded: dashboards filter on them.
    """

    #: The ``/workflows/v1/check`` endpoint behind the setup form.
    HTTP = "http"
    #: The ``sdr:preflight_check`` Temporal activity (test-connection).
    SDR = "sdr"


#: Per surface: is the outcome row the customer's *only* sight of the verdict?
#:
#: The level policy turns on this, not on how expected the verdict is. HTTP
#: returns the verdict as the response body the setup form renders, so its row
#: is a duplicate and stays INFO. An SDR failure travels back through a workflow
#: whose run log the customer reads at the default ERROR filter, so that row
#: mirrors the gate's levels or the failure is invisible — the hole FND-901
#: exists to close.
#:
#: A table rather than a branch so the policy is *enumerable*:
#: ``test_every_surface_has_a_level_policy`` asserts these keys cover
#: ``PreflightSurface``, which fails CI for a new member nobody routed. An
#: exhaustive ``if``/``assert_never`` would only warn here — this repo sets
#: ``reportArgumentType = "warning"``, so pyright flags the gap without failing
#: on it.
_LOG_ROW_IS_ONLY_CHANNEL: dict[PreflightSurface, bool] = {
    PreflightSurface.HTTP: False,
    PreflightSurface.SDR: True,
}


def _log_row_is_only_channel(surface: PreflightSurface) -> bool:
    """Look up ``surface``'s level policy, defaulting an unrouted one to loud.

    Never raises on a miss: this is the emit path, and losing the row entirely
    is strictly worse than logging it one level too loud. The test above is what
    keeps the miss from happening.
    """
    return _LOG_ROW_IS_ONLY_CHANNEL.get(surface, True)


def warn_if_partial(result: PreflightOutput) -> None:
    """Emit the deprecation signal where the SDK acts on a ``PARTIAL`` verdict."""
    if result.status is PreflightStatus.PARTIAL:
        warnings.warn(
            PreflightStatus.__deprecated_members__["PARTIAL"],
            DeprecationWarning,
            stacklevel=3,
        )


def emit_preflight_check_outcome(
    log: AtlanLoggerAdapter,
    app_name: str,
    result: PreflightOutput,
    *,
    surface: PreflightSurface,
    entrypoint: str | None = None,
    request_id: str | None = None,
) -> None:
    """Emit the interactive-surface sibling of the gate's outcome row (FND-901).

    One attribute schema for every surface that runs ``Handler.preflight_check``,
    so the setup funnel (HTTP form check, SDR test-connection) is queryable next
    to run-time gate verdicts. The level follows whether the log is the delivery
    channel — see :func:`_log_row_is_only_channel`. A surface whose row is the
    only channel mirrors the gate's map (``not_ready`` at ERROR, a passed
    verdict carrying a failed advisory check at WARNING, clean at INFO); one
    that returns the verdict by another route stays INFO throughout. Handler
    crashes additionally emit a crash-marked row via
    :func:`emit_preflight_crash_outcome`. Callers pass their module logger so
    the row keeps the surface's source.
    """
    warn_if_partial(result)
    failed = [c for c in result.checks if not c.passed]
    # The same two ladders the gate row uses, so the two surfaces attribute a
    # verdict identically: a block to _primary_failure (the aggregate wins —
    # SDR inserts a non-fatal row ahead of the real failure and pins the real
    # one on result.error), a run that went ahead to _proceeded_failure.
    primary: FailureDetails | None
    if result.status is PreflightStatus.NOT_READY:
        primary = _primary_failure(result, app_name)
    else:
        primary = _proceeded_failure(result, app_name)
    # Off the same object as failure.check / failure.message / failure.audience,
    # never re-derived — the row's four attributed fields cannot disagree. A
    # partial used to report the status here while the gate row reported the
    # failed check's code for the identical verdict; the argument _proceeded_failure
    # makes ("a reason of PARTIAL hides which check failed") is not surface-specific,
    # and `outcome` carries the status on the same row either way. Only a row with a
    # failed check changes: with nothing failed there is no primary and the status
    # stands.
    reason = primary.code if primary is not None else result.status.value
    extra: dict[str, Any] = {}
    if primary is not None:
        extra[FAILURE_AUDIENCE_KEY] = primary.audience.value
    if request_id is not None:
        extra["request_id"] = request_id
    extra.update(_failure_fields(result.checks, primary))
    if not _log_row_is_only_channel(surface):
        emit = log.info
    elif result.status is PreflightStatus.NOT_READY:
        emit = log.error
    elif failed:
        emit = log.warning
    else:
        emit = log.info
    emit(
        PREFLIGHT_CHECK_EVENT,
        outcome=result.status.value,
        reason=reason,
        app_name=app_name,
        entrypoint=entrypoint or "<implicit>",
        checks=len(result.checks),
        **{
            CHECK_MATRIX_KEY: _check_matrix_json(result.checks),
            PREFLIGHT_SURFACE_KEY: surface.value,
        },
        **extra,
    )


#: Categories the HTTP boundary answers with a 4xx (``service.py``'s
#: ``_CATEGORY_TO_HTTP``): the response working as designed, not a handler
#: crash. A wrong password (AUTH → 401) is the single most common preflight
#: failure; counting it as a crash would let setup-form typos dominate the
#: crash series and the metric would stop measuring handler health. A
#: consistency test pins this set against the HTTP mapping so the two
#: judgements cannot drift.
_CLIENT_FAULT_CATEGORIES = frozenset(
    {
        FailureCategory.AUTH,
        FailureCategory.PERMISSION,
        FailureCategory.NOT_FOUND,
        FailureCategory.ALREADY_EXISTS,
        FailureCategory.INVALID_INPUT,
        FailureCategory.PRECONDITION,
        FailureCategory.RATE_LIMITED,
        FailureCategory.CANCELLED,
    }
)


#: Per ``(outcome, row-is-only-channel)``: is the interactive row loud (ERROR)?
#:
#: A table rather than a branch for the same reason as
#: ``_LOG_ROW_IS_ONLY_CHANNEL`` above — the policy has to be *enumerable*, so
#: ``test_every_interactive_outcome_has_a_level_policy`` fails CI for an
#: outcome added to ``INTERACTIVE_RAISE_OUTCOMES`` without both surface
#: entries. A real crash is evidence about handler health and is
#: loud everywhere; a client fault is the response working as designed, so it
#: stays quiet where the response carries it (HTTP) and goes loud only where
#: the row is the customer's only sight of it (SDR).
_INTERACTIVE_ROW_IS_LOUD: dict[tuple[PreflightRowOutcome, bool], bool] = {
    (PreflightRowOutcome.CRASHED, False): True,
    (PreflightRowOutcome.CRASHED, True): True,
    (PreflightRowOutcome.CLIENT_FAULT, False): False,
    (PreflightRowOutcome.CLIENT_FAULT, True): True,
}

#: The outcomes :func:`emit_preflight_crash_outcome` can attribute a raise to.
#: The gate's own verdict values never reach that site.
INTERACTIVE_RAISE_OUTCOMES: tuple[PreflightRowOutcome, ...] = (
    PreflightRowOutcome.CRASHED,
    PreflightRowOutcome.CLIENT_FAULT,
)


def _is_client_fault(exc: BaseException) -> bool:
    """Whether ``exc`` is a client-facing input error, not a handler crash.

    An explicit sub-500 ``http_status`` (a ``HandlerError`` the caller already
    judged client-facing) or a typed category the HTTP boundary maps to a 4xx
    means the failure is the response working as designed.
    """
    status = getattr(exc, "http_status", None)
    if isinstance(status, int) and status < 500:
        return True
    return isinstance(exc, AppError) and type(exc).category in _CLIENT_FAULT_CATEGORIES


def emit_preflight_crash_outcome(
    log: AtlanLoggerAdapter,
    app_name: str,
    exc: BaseException,
    *,
    surface: PreflightSurface,
    entrypoint: str | None = None,
    request_id: str | None = None,
) -> None:
    """Emit the outcome row for a raise on an interactive surface.

    A raise (HTTP form check, SDR test connection) produces no verdict body
    anywhere, so without this row the failure is invisible to the setup-funnel
    metrics built on the event — the case drops out of the denominator. Every
    raise is therefore *counted*, but attribution is split so the crash series
    keeps measuring handler health:

    - a real crash (untyped, or a 5xx-class typed error) emits
      ``outcome="crashed"`` at ERROR on every surface;
    - a client-input error (:func:`_is_client_fault` — an explicit sub-500
      ``http_status`` or a typed 4xx-class category, e.g. a wrong password)
      emits ``outcome="client_fault"`` instead, at the level
      ``_INTERACTIVE_ROW_IS_LOUD`` routes it to: ERROR where the row is the
      only channel (SDR), INFO where the response already carries the failure
      (HTTP). Dropping these entirely would
      re-open the denominator hole for the boundary steps (secret-store probe,
      credential resolution) the SDR surface wraps.

    Emitted in addition to (never instead of) each surface's own boundary
    error handling. ``reason`` is the typed wire code for an ``AppError``, the
    class name otherwise. The split lives here, at the single emit site, so
    every surface applies the same judgement.
    """
    outcome = (
        PreflightRowOutcome.CLIENT_FAULT
        if _is_client_fault(exc)
        else PreflightRowOutcome.CRASHED
    )
    # Default-loud on a table miss, mirroring :func:`_log_row_is_only_channel`:
    # one level too loud beats losing the row.
    loud = _INTERACTIVE_ROW_IS_LOUD.get(
        (outcome, _log_row_is_only_channel(surface)), True
    )
    emit = log.error if loud else log.info
    extra: dict[str, Any] = {}
    if isinstance(exc, AppError):
        extra[FAILURE_AUDIENCE_KEY] = type(exc).audience.value
    if request_id is not None:
        extra["request_id"] = request_id
    emit(
        PREFLIGHT_CHECK_EVENT,
        outcome=outcome.value,
        reason=exc.code if isinstance(exc, AppError) else type(exc).__name__,
        app_name=app_name,
        entrypoint=entrypoint or "<implicit>",
        checks=0,
        **{
            CHECK_MATRIX_KEY: EMPTY_CHECK_MATRIX,
            PREFLIGHT_SURFACE_KEY: surface.value,
        },
        **extra,
    )


def filter_checks_to_tier(result: PreflightOutput, tier: CheckTier) -> PreflightOutput:
    """``result`` keeping only the checks in ``tier``.

    One rule for ``/check`` and the gate: a handler that ignores
    ``PreflightInput.tier`` still answers a tiered request with the right rows,
    and a ``WARMUP`` probe it ran anyway cannot decide the ``FAST`` dispatch.

    So a ``NOT_READY`` aggregate is re-derived when the reason for it was a
    dropped check. That is the case when the aggregate ``error`` describes a
    dropped failed check (same ``code`` and ``message``), or when there is no
    aggregate ``error`` and the ``message`` is empty or is a dropped failed
    check's own line. The verdict then follows the kept rows: still
    ``NOT_READY``, attributed to them, if any kept check failed, else
    ``READY``. An aggregate reason that matches no dropped check is the
    handler's own verdict about the source, so it stands. ``READY`` and
    ``PARTIAL`` are left alone.
    """
    kept = [check for check in result.checks if check.effective_tier is tier]
    if len(kept) == len(result.checks):
        return result
    update: dict[str, object] = {"checks": kept}
    dropped_failed = [
        c for c in result.checks if c.effective_tier is not tier and not c.passed
    ]
    if result.status is PreflightStatus.NOT_READY and _reason_is_among(
        result, dropped_failed
    ):
        kept_failed = any(not c.passed for c in kept)
        update.update(
            status=PreflightStatus.NOT_READY if kept_failed else PreflightStatus.READY,
            error=None,
            message="",
        )
    return result.model_copy(update=update)


def _reason_is_among(result: PreflightOutput, checks: list[PreflightCheck]) -> bool:
    """Whether ``result``'s aggregate reason is one of ``checks``' failures."""
    if not checks:
        return False
    if result.error is not None:
        return any(
            c.error is not None
            and c.error.code == result.error.code
            and c.error.message == result.error.message
            for c in checks
        )
    return not result.message or any(
        result.message in (c.message, c.error.message if c.error else None)
        for c in checks
    )
