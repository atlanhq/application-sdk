"""What makes an AE write safe to re-issue, and how long to wait before doing it.

Lifted from ``testing/e2e/client.py`` with the AE half (child F on FND-224).
Every function here is pure: it reads a status code, a response body or an
exception and answers one question about it. The loops that act on those answers
live in :mod:`application_sdk.testing.harness.automation_engine.client`.

Grouped here rather than left next to the loop because the submit is the one
non-idempotent write in the harness, and the reasoning that keeps it safe —
*can the origin have seen this request?*, *did it leave a trace?*, *is this
conflict terminal or transient?* — is the part worth reading on its own.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx

from application_sdk.observability.logger_adaptor import get_logger
from application_sdk.testing.harness._errors import MissingHarnessClassAttrError
from application_sdk.testing.harness.automation_engine._errors import RequestDelivery

logger = get_logger(__name__)

__all__ = [
    "RunLookup",
    "WriteRecovery",
    "cold_start_submit_kwargs",
]


# An overloaded tenant answers a retryable 5xx with its own estimate of how
# long to wait before trying again:
#
#     {"retryable": true, "retry_after": 120,
#      "what_you_should_do": "Wait and retry. Back off for at least 120 s."}
#
# The fixed inter-attempt gap used to ignore that, so a 5-attempt / 5 s budget
# expired ~20 s into a 120 s window: the retry could not succeed by
# construction whenever the origin was genuinely slow (it only ever helped for
# blips shorter than the whole budget). We now honour the origin's number,
# bounded two ways so a pathological value cannot hang a CI leg:
#
# * each individual wait is capped at ``MAX_RETRY_AFTER_SECONDS``;
# * the total honoured-above-the-fixed-gap wait within one retry loop is capped
#   at ``RETRY_AFTER_BUDGET_SECONDS``, after which the remaining attempts fall
#   back to the caller's fixed gap.
#
# The attempt counts stay as they were — the gap was the wrong part.
#
# Both are also the ``max_retry_after`` / ``retry_after_budget`` fields of
# ``CONNECTOR_CI``'s budgets, which read them from here.
MAX_RETRY_AFTER_SECONDS = 120
RETRY_AFTER_BUDGET_SECONDS = 300

# Keys an origin may use for the backoff hint, and the one-level envelopes we
# have seen it wrapped in. Heracles puts it at the top level; AE's generic
# error shape nests the detail under ``data`` / ``error``.
_RETRY_AFTER_KEYS = ("retry_after", "retryAfter")
_RETRY_AFTER_ENVELOPE_KEYS = ("data", "error", "detail")


def requested_retry_after(body: dict[str, Any] | str | None) -> int | None:
    """Seconds the origin asked us to wait, or ``None`` when it didn't ask.

    Reads ``retry_after`` from the response body — top level, or nested one
    level inside a ``data`` / ``error`` / ``detail`` envelope. Fractional
    values round up (a hint of 0.5 s means "at least half a second", so
    truncating it to 0 would be wrong). Anything non-numeric or non-positive
    is treated as absent: a hint we cannot act on must leave the caller's
    fixed gap in place rather than collapse the wait to zero.
    """
    if not isinstance(body, dict):
        return None
    candidates: list[Any] = [body.get(key) for key in _RETRY_AFTER_KEYS]
    for envelope_key in _RETRY_AFTER_ENVELOPE_KEYS:
        envelope = body.get(envelope_key)
        if isinstance(envelope, dict):
            candidates.extend(envelope.get(key) for key in _RETRY_AFTER_KEYS)
    for value in candidates:
        # bool is an int subclass — `retryable: true` must not read as 1 s.
        if isinstance(value, bool) or not isinstance(value, (int, float, str)):
            continue
        try:
            seconds = math.ceil(float(value))
        except (ValueError, OverflowError):
            # Non-numeric string, or inf/nan. Benign: an unreadable hint just
            # leaves the caller's fixed gap in place, so probe the next
            # candidate key rather than failing the retry.
            logger.debug(
                "ignoring unusable retry_after value %r in response body",
                value,
                exc_info=True,
            )
            continue
        if seconds > 0:
            return seconds
    return None


@dataclass(frozen=True)
class RetryGap:
    """How long to wait before the next attempt, and what the origin asked for."""

    seconds: int
    # What the body requested, before capping. ``None`` when it carried no
    # hint, in which case ``seconds`` is the caller's fixed gap.
    requested: int | None

    @property
    def origin_note(self) -> str:
        """Log fragment naming the origin's request, empty when it made none."""
        if self.requested is None:
            return ""
        if self.requested == self.seconds:
            return " (origin asked for this)"
        if self.requested < self.seconds:
            # The wait is longer than the origin asked for: either the fixed
            # gap floored it (small request) or the per-loop budget did. Never
            # a cap — a cap can only shorten the wait, this one lengthened it.
            return f" (origin asked for {self.requested}s; floored at {self.seconds}s)"
        return f" (origin asked for {self.requested}s, capped)"


def retry_gap(
    requested: float | None,
    *,
    default_seconds: int,
    budget_left: float,
) -> RetryGap:
    """Pick the wait before the next attempt, honouring the origin's request.

    Returns the caller's ``default_seconds`` when the origin asked for nothing
    usable. Otherwise returns the requested wait clamped to
    :data:`MAX_RETRY_AFTER_SECONDS` and to ``budget_left`` — and never below
    ``default_seconds``, so honouring a hint can only ever lengthen the gap,
    never shorten it below what the loop already guaranteed.

    :func:`~application_sdk.testing.harness.waiting._honoured_gap` is the same
    rule expressed over :class:`~application_sdk.testing.harness.budgets.Budget`
    for the generic primitive; this one stays because the AE loops read integer
    seconds off an HTTP body and round on the way in.

    Args:
        requested: Seconds the origin asked for, from
            :func:`requested_retry_after` or an error's
            ``retry_after_seconds``. ``None`` / non-positive means no request.
        default_seconds: The loop's own fixed gap — the floor, and the answer
            when there is no request.
        budget_left: Seconds of above-the-floor waiting still permitted in
            this loop. Zero (or less) degrades cleanly to ``default_seconds``.
            ``float`` because the poll loop accumulates its spend as one — an
            ``int`` here would have the caller round before the clamp rather
            than after it.
    """
    if requested is None or requested <= 0:
        return RetryGap(seconds=default_seconds, requested=None)
    wanted = math.ceil(requested)
    allowed = min(wanted, MAX_RETRY_AFTER_SECONDS, max(budget_left, 0.0))
    return RetryGap(seconds=int(max(default_seconds, allowed)), requested=wanted)


@dataclass(frozen=True)
class RunLookup:
    """What a read of AE's run list learned about a run we expected to exist.

    Absence and ignorance are different answers and the caller acts on them
    differently, so they are not both spelled ``None``.
    """

    run_id: str | None = None
    """The matching run's id, when one was found."""

    conclusive: bool = False
    """True when AE actually answered. ``run_id=None, conclusive=True`` is
    proof the run does not exist; ``conclusive=False`` means the read never
    got through and nothing was established either way."""


@dataclass(frozen=True)
class WriteRecovery:
    """What a follow-up read learned about a write whose response was lost.

    Returned by
    :meth:`~application_sdk.testing.harness.automation_engine.client.AEClient._post_with_retry`'s
    ``recover_ambiguous`` hook. The default — no body, not proven absent — is
    the "learned nothing" case, which leaves the write as ambiguous as it was.
    """

    body: dict[str, Any] | None = None
    """The response the origin would have sent, when the write is found to
    have landed. Returned to the caller in place of the lost response."""

    proven_absent: bool = False
    """True only when the read positively established that the write left no
    trace. This — not the mere absence of a body — is what licenses re-issuing
    a non-idempotent write."""


def parse_run_timestamp(raw: object) -> datetime | None:
    """Best-effort read of an AE ``created_at`` into an aware UTC datetime.

    AE models the field as a ``datetime``, which FastAPI serialises to ISO-8601
    — but this value decides whether we adopt a run or re-POST a
    non-idempotent submit, so a shape this function fails to recognise must
    read as "unknown" (``None``), never as "old enough" or "new enough".
    Numeric forms are accepted too: the Atlan-mode registry populates the field
    from a metastore entity whose create time is epoch milliseconds.
    """
    if isinstance(raw, bool):  # bool is an int; never a timestamp
        return None
    if isinstance(raw, int | float):
        # Disambiguate by magnitude: anything past ~year 5138 in seconds is
        # milliseconds. Both AE backends are well clear of that boundary.
        seconds = raw / 1000 if raw > 1e11 else float(raw)
        try:
            return datetime.fromtimestamp(seconds, tz=UTC)
        except (OverflowError, OSError, ValueError):
            return None
    if not isinstance(raw, str) or not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    # A naive timestamp is UTC by convention on both AE backends
    # (``datetime.now(timezone.utc)`` server-side, epoch-derived in Atlan mode).
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def newest_run_since(body: dict[str, Any], floor: datetime) -> str | None:
    """Run id of the newest run in *body* created at or after *floor*.

    ``body`` is AE's ``BulkResponse[WorkflowRun]``: ``{"data": [...]}``, newest
    first. A row whose ``created_at`` cannot be parsed is skipped rather than
    assumed recent — adopting the wrong run would make the harness poll a run
    that is not the one it submitted.
    """
    rows = body.get("data")
    if not isinstance(rows, list):
        return None
    for row in rows:
        if not isinstance(row, dict):
            continue
        run_id = row.get("guid") or row.get("run_id")
        if not isinstance(run_id, str) or not run_id:
            continue
        created = parse_run_timestamp(row.get("created_at"))
        if created is not None and created >= floor:
            return run_id
    return None


def classify_delivery(exc: Exception) -> RequestDelivery:
    """Can the origin have seen the request that raised *exc*?

    ``httpx`` separates the connect phase from everything after it, which
    ``urllib`` collapsed into a bare ``TimeoutError``. A failure to *establish*
    the connection proves the request bytes never left the client, so even a
    non-idempotent write is safe to re-issue. Anything later — a read timeout,
    a reset mid-flight, a protocol error — may already have been processed by
    the origin.

    Deliberately conservative: only the three exception types that cannot
    coexist with a delivered request are classified
    :attr:`RequestDelivery.NOT_DELIVERED`; everything else, including any
    future ``httpx`` transport error this function has not been taught about,
    falls through to :attr:`RequestDelivery.AMBIGUOUS` and keeps the old
    never-repost behaviour.

    * :class:`httpx.ConnectTimeout` — the TCP/TLS handshake never completed.
      This is the shape a blackholed public-FQDN hairpin takes on the CI
      runners, and the case this classification exists to recover.
    * :class:`httpx.ConnectError` — DNS failure, connection refused, no route.
    * :class:`httpx.PoolTimeout` — no connection was ever acquired from the
      pool. Reachable now that the AE client pools connections across a run,
      where the single-use ``httpx.Client`` it replaced could not hit it.
    """
    if isinstance(exc, httpx.ConnectTimeout | httpx.ConnectError | httpx.PoolTimeout):
        return RequestDelivery.NOT_DELIVERED
    return RequestDelivery.AMBIGUOUS


def is_credential_name_conflict(status: int, body: dict[str, Any] | str) -> bool:
    """True iff *body* is AE's unique-constraint violation on the credential name.

    AE (Heracles) creates the submit credential non-idempotently, keyed on a
    UNIQUE ``credentials_name_key``. A submit retried after a transient (a 5xx,
    or a timeout that AE actually committed) re-sends the same credential name
    and trips this constraint as an HTTP 400 — recoverable by rotating the name
    (see :func:`rotate_submit_credential_name`) and retrying, rather than
    surfacing as a hard failure.

    NOTE: detection is a substring match for the literal constraint name
    ``credentials_name_key`` in AE's error body. If AE ever renames that
    constraint the match silently stops firing and the conflict resurfaces as a
    hard failure — safe (never a false retry), but revisit this if AE's error
    shape changes.
    """
    if status < 400:
        return False
    text = body if isinstance(body, str) else repr(body)
    return "credentials_name_key" in text


def rotate_submit_credential_name(body: dict[str, Any] | None) -> None:
    """Give the submit payload's credential a fresh name, in place.

    Invoked before each submit retry so the re-sent request can't collide on
    ``credentials_name_key``. No-op when the payload carries no credential
    (public sources). AE resolves ``{{credentialGuid}}`` to whichever credential
    it creates, so any unique name stays self-consistent. Names rotate as
    ``<name>-retry1``, ``-retry2``, … so orphans are traceable to the run.
    """
    if not isinstance(body, dict):
        return
    items = body.get("payload")
    if not isinstance(items, list) or not items:
        return
    cred = items[0]
    if not (isinstance(cred, dict) and isinstance(cred.get("body"), dict)):
        return
    name = cred["body"].get("name")
    if not name:
        return
    base, _, tail = str(name).rpartition("-retry")
    n = int(tail) + 1 if base and tail.isdigit() else 1
    cred["body"]["name"] = f"{base or name}-retry{n}"


# One mustache token, e.g. ``{{credentialGuid}}``, capturing the name. Applied
# with fullmatch below, so the brace-free inner class is what keeps a value like
# ``{{a}}{{b}}`` from reading as a single token.
_MUSTACHE_TOKEN_RE = re.compile(r"\{\{([^{}]+)\}\}")


def unsubstituted_parameter_tokens(body: Any) -> dict[str, str]:
    """Map ``parameter name -> mustache token`` for values AE left unresolved.

    The harness submits ``{{credentialGuid}}`` as a *deliberate* literal: the
    ``payload[]`` block asks AE to create a credential and substitute the token
    in the request's own Argo parameters (see ``build_ae_payload``). When that
    substitution silently does not happen, AE still answers 2xx with a run_id,
    so the harness polls happily and the literal only surfaces ~2min later as a
    worker-side ``[AAF-CRD-005] Invalid credential GUID`` on the extract node —
    a connector-looking error for a control-plane fault. This reads the submit
    response so the fault can be named where it happens.

    Returns parameter names and token names only, never values: an Argo
    parameter value can carry a source credential, and this feeds a log line.

    Only reports when a value is EXACTLY one token. A value that merely embeds
    braces (a JSON blob, a regex) is not evidence of failed substitution.
    """
    found: dict[str, str] = {}

    def _walk(node: Any) -> None:
        if isinstance(node, dict):
            name, value = node.get("name"), node.get("value")
            if isinstance(name, str) and isinstance(value, str):
                m = _MUSTACHE_TOKEN_RE.fullmatch(value.strip())
                if m:
                    found[name] = m.group(1)
            for v in node.values():
                _walk(v)
        elif isinstance(node, list):
            for v in node:
                _walk(v)

    _walk(body)
    return found


# AE returns "a run for workflow '<slug>' is already active" (code AE-WF-409-03)
# when a submit collides with an in-flight run. Heracles (the tenant-facing
# proxy in front of Automation Engine) masks that 409 as an HTTP 500 with the
# original 409 text embedded in the message, so we detect the conflict by its
# stable error code regardless of the outer status. We match on the code alone
# (not the generic "already active" prose): the code is unambiguous, whereas the
# phrase could appear in an unrelated, genuinely-transient 5xx and wrongly mark
# it terminal.
_ALREADY_ACTIVE_CODE = "AE-WF-409-03"


def is_already_active_run(status: int, body: Any) -> bool:
    """True when a submit response signals an already-active run (masked or not).

    A submit is not idempotent: retrying one that AE already accepted spawns a
    duplicate run AE marks ``Skipped``. This conflict is therefore terminal, not
    transient — callers use it to stop retrying even when AE surfaces it as a
    5xx.
    """
    if status < 400:
        return False
    haystack = body if isinstance(body, str) else repr(body)
    return _ALREADY_ACTIVE_CODE.casefold() in haystack.casefold()


# A tenant app pod that LM reports as reconciled can still be tens of seconds
# from serving HTTP on :8000. At submit, Heracles POSTs the credential config to
# http://<conn>.<conn>-app.svc.cluster.local:8000/workflows/v1/config/... against
# that pod and echoes a refused dial back as an HTTP 500 carrying the Go net
# error — e.g. "dial tcp 10.x.x.x:8000: connect: connection refused" (FND-402:
# s3/gcs/mongodbatlas failed this way while cloudsql/iceberg passed in the same
# window).
#
# Unlike _ALREADY_ACTIVE_CODE this has no stable error code to match — the
# string originates in Go's net package, passes through Heracles unstructured,
# and never acquires one — so this is necessarily a prose match. Same caveat as
# is_credential_name_conflict: if the wire text changes the match silently
# stops firing and the race resurfaces as a generic retryable 5xx, which is the
# pre-FND-402 behaviour (safe, just slower to diagnose) rather than a new
# failure mode.
#
# This does NOT gate whether the submit is retried — submit_workflow's
# `retryable` predicate already retries any non-already-active 5xx, this shape
# included. It exists only to (a) justify the long cold-start budget and (b)
# name the terminal failure AppNotReadyError instead of a bare 500.
#
# The match requires the refused-dial-to-:8000 sequence, not the bare
# `connection refused` substring: a genuine terminal 5xx whose body merely
# mentions a refused connection (e.g. an upstream DB dial surfaced through
# Heracles) must not be mis-named AppNotReadyError. Requiring `dial tcp` +
# `:8000` + `connection refused` together restricts the match to a refused dial
# to the tenant app pod.
_APP_NOT_READY_MARKERS = ("dial tcp", ":8000", "connection refused")


def is_app_not_ready(status: int, body: Any) -> bool:
    """True when a submit response reads as a not-yet-serving tenant app pod.

    Deliberately narrow (refused dial to :8000 only): a refused dial never
    reached the pod, so no run was created and the wait is unambiguously a cold
    start. A genuine 5xx, a 4xx, the already-active conflict, or a 5xx that only
    mentions a refused connection in passing must not read as this.
    """
    # 5xx only. Heracles reports the refused dial as a 500, and a 4xx is a
    # request-side rejection AE decided without dialling the pod — so a 4xx
    # whose body happens to carry the markers (e.g. echoed-back config) is a
    # terminal AtlanApiHttpError, not this race.
    if status < 500:
        return False
    haystack = body if isinstance(body, str) else repr(body)
    haystack = haystack.casefold()
    return all(marker in haystack for marker in _APP_NOT_READY_MARKERS)


def cold_start_submit_kwargs(
    timeout_seconds: int,
    poll_interval_seconds: int,
) -> dict[str, int]:
    """Re-size the AE submit's retry to a cold start.

    Shared by both full-DAG harnesses (``BaseE2ETest.run_full_dag`` and the
    deprecated ``BaseFullDAGE2ETest.run_full_dag``) so a DIRECT-mode submit
    against a freshly-installed tenant app gets the same budget in either.

    A refused dial to a still-booting tenant app pod arrives as a generic
    retryable 5xx, so the submit already retries it (see
    :func:`is_app_not_ready`) — only its default 4x5s budget is too short for a
    pod cold start. Widening that existing loop keeps ONE retry path for the
    submit, which matters because the submit is non-idempotent: a second loop
    wrapped around it would re-enter the inner retry per outer attempt (5x the
    POSTs it reports) and would bypass ``retry_after`` honouring and the
    credential-name rotation that make each re-POST safe.

    Args:
        timeout_seconds: Total cold-start budget (the harness'
            ``app_ready_timeout_seconds``). 0 or negative returns no overrides,
            leaving the submit's own defaults in place.
        poll_interval_seconds: Gap between submit attempts (the harness'
            ``app_ready_poll_interval_seconds``).

    Returns:
        ``retries`` / ``retry_sleep_seconds`` kwargs for ``submit_workflow``,
        or an empty dict when the budget is disabled.

    Raises:
        MissingHarnessClassAttrError: when ``timeout_seconds`` is positive but
            ``poll_interval_seconds`` is not — the retry count integer-divides
            by the interval, so a zero or negative interval would crash with
            ``ZeroDivisionError`` rather than gate the submit.
    """
    if timeout_seconds <= 0:
        return {}
    if poll_interval_seconds <= 0:
        raise MissingHarnessClassAttrError(
            message=(
                "app_ready_poll_interval_seconds must be > 0 when "
                f"app_ready_timeout_seconds={timeout_seconds} is set; got "
                f"app_ready_poll_interval_seconds={poll_interval_seconds}"
            ),
            field="app_ready_poll_interval_seconds",
        )
    return {
        "retries": timeout_seconds // poll_interval_seconds,
        "retry_sleep_seconds": poll_interval_seconds,
    }
