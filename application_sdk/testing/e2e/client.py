"""HTTP client for Atlan tenant endpoints used by tier-4/5 full-DAG tests.

Wraps three endpoints:

* ``POST /api/service/package-workflows?submit=true`` — AE submit.
* ``GET  /api/service/package-workflows/native-status/<run_id>`` — DAG
  run status with per-node breakdown (one entry per node in
  ``manifest.json``'s DAG).
* ``GET  /api/meta/entity/uniqueAttribute/type/Connection?attr:qualifiedName=<qn>``
  — Atlas-side check that the resulting Connection asset is queryable.

The shape of the native-status response (captured against devex with a
real workflow run):

.. code-block:: json

    {
      "status": "Running",
      "run_id": "7fd7b893-...",
      "workflow_slug": "mysql-oUUCLfTn",
      "temporal_run_id": "...",
      "dag_nodes": {
        "extract":         {"status": "Succeeded", "started_at": ..., "completed_at": ..., "error_message": null},
        "qi":              {"status": "Succeeded", ...},
        "publish":         {"status": "Succeeded", ...},
        "lineage-app":     {"status": "Running",   ...},
        "lineage-publish": {"status": "Pending",   ...}
      }
    }

We treat ``"Succeeded" | "Failed" | "Error" | "Cancelled"`` as terminal
node statuses, ``"Running" | "Pending" | "Scheduled"`` as in-flight.
"""

from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from enum import Enum
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

import httpx
import orjson

if TYPE_CHECKING:
    from pyatlan.client.aio.client import AsyncAtlanClient

from application_sdk.errors.base import AppError
from application_sdk.observability.logger_adaptor import get_logger
from application_sdk.testing.e2e._errors import (
    AppNotReadyError,
    AtlanAEWorkflowAlreadyActiveError,
    AtlanApiHttpError,
    AtlanApiResponseInvariantError,
    AtlanApiTimeoutError,
    DAGProgressStalledError,
    MissingHarnessClassAttrError,
    NoWorkerOnTaskQueueError,
    RequestDelivery,
)
from application_sdk.testing.e2e._poll import _HEARTBEAT_SECONDS, until_deadline

logger = get_logger(__name__)


# A default client User-Agent (``Python-urllib/<ver>``, ``python-httpx/<ver>``)
# is blocked by Cloudflare on most Atlan tenants (Error 1010 — browser
# signature banned). Spoofing a real UA keeps the request flowing through.
_USER_AGENT = "atlan-sdk-full-dag-e2e/1.0 (+https://github.com/atlanhq/application-sdk)"

# Timeout budget for individual HTTP calls. Polls run inside outer
# while-loops so the overall budget is driven by ``poll_native_status``
# / ``poll_atlas_for_connection``; the per-request timeout just keeps
# any one call from hanging the whole loop.
_HTTP_TIMEOUT = 60
# AE create and submit can take >60 s on the first call — the origin
# server may be slow to respond, causing Cloudflare to return HTTP 504
# at its own gateway timeout before a 60 s client window closes.  Using
# 120 s lets Cloudflare's 504 arrive as an HTTP error (which the
# existing 5xx retry loop handles) rather than a raw TimeoutError.
_SUBMIT_TIMEOUT = 120

# Transient network-layer errors (DNS blips, read timeouts, connection resets)
# are common during multi-minute polls over a VPN/loft tunnel to a tenant. Retry
# each HTTP call a few times before surfacing the failure, so a single blip in a
# 10-15 min poll doesn't fail the whole run.
_REQUEST_MAX_ATTEMPTS = 4
_REQUEST_BACKOFF_SECONDS = 3

# Reconciling an ambiguous submit: how long to keep asking AE whether the run
# it may or may not have accepted actually exists.
#
# AE persists the run record BEFORE it calls Temporal and before it answers the
# submit, so any submit that got far enough to have an effect leaves a row we
# can find. In production that row is read back through Elasticsearch, which is
# near-real-time rather than real-time — a run committed microseconds before the
# connection died can take a few seconds to become searchable. A single probe
# would read that lag as "no run exists" and re-POST into a duplicate, so we
# poll for a window instead. The window only ever elapses on a path that today
# fails the whole leg outright, so it is sized for confidence, not for speed.
_RECONCILE_TIMEOUT_SECONDS = 90
_RECONCILE_INTERVAL_SECONDS = 10

# How far before the submit to start looking for "our" run. Absorbs clock skew
# between the CI runner and the tenant, whose timestamps we are comparing
# directly. Safe to be generous: the workflow slug is unique per CI leg
# (``<connector>-e2e-full-ci-<run_id>-<hash>``), so the only runs this window
# can possibly match are the leg's own submits of the same payload.
_RECONCILE_CLOCK_SKEW_SECONDS = 120

# AE caps ``page_size`` at 100 and returns runs newest-first. One page is
# ample: a single leg's unique slug has one run, or a small handful if an
# earlier attempt in the same call already landed.
_RECONCILE_PAGE_SIZE = 20

# Whether "AE lists no run under this slug" may authorise re-POSTing an
# ambiguous submit. Deliberately off.
#
# Reconciliation has two halves with very different risk profiles. *Adopting* a
# run the read finds is pure upside: if the listing works we recover a leg that
# would otherwise have failed, and if it doesn't we find nothing and behave
# exactly as before. *Re-POSTing* because the read found nothing is only safe
# if an empty list genuinely means "no run exists" — and that has not been
# established end-to-end, because the harness submits through Heracles
# (``/api/service/package-workflows``) rather than through AE's own
# ``/api/v1/workflows/{slug}/submit``. AE's run listing filters out runs
# lacking automation-engine fields, so a Heracles-originated run being invisible
# here would turn every empty read into a false "nothing landed" and produce the
# duplicate run this whole mechanism exists to prevent.
#
# Flip this on once a live e2e leg has shown a Heracles-submitted run appearing
# under GET /automation/api/v1/runs?workflow_slug=<slug>. Until then the
# absence half stays disabled and an unrecovered ambiguous submit fails exactly
# as it did before. The machinery below is complete and tested either way.
_RESUBMIT_WHEN_AE_REPORTS_NO_RUN = False

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
# * each individual wait is capped at ``_MAX_RETRY_AFTER_SECONDS``;
# * the total honoured-above-the-fixed-gap wait within one retry loop is capped
#   at ``_RETRY_AFTER_BUDGET_SECONDS``, after which the remaining attempts fall
#   back to the caller's fixed gap.
#
# The attempt counts stay as they were — the gap was the wrong part.
_MAX_RETRY_AFTER_SECONDS = 120
_RETRY_AFTER_BUDGET_SECONDS = 300

# ``_HEARTBEAT_SECONDS`` (imported from ``_poll``) is the cadence for "still
# polling" heartbeat log lines in ``poll_native_status`` — lineage stages take
# 2-5 min on small datasets and the status string doesn't change during that
# time, so the loop would otherwise look wedged in CI output. That loop throttles
# its own richer progress line to the same cadence and disables the generic
# heartbeat, rather than emitting two "still waiting" lines.

# Per-status glyphs for the poll-loop log line — gives the operator a
# quick visual scan of "what's done / what's running" without parsing
# a long ``a=Succeeded; b=Running; c=Pending`` string. Used by
# :func:`_node_glyph` (per-node) and :data:`_RUN_GLYPHS` (top-level).
# Colour emoji rather than monochrome glyphs: GH Actions logs render
# them inline and the colour signals status faster than the shape.
_NODE_GLYPHS = {
    "Succeeded": "✅",
    "Failed": "❌",
    "Running": "🔄",
    "Pending": "🟡",
    "Cancelled": "🚫",
    "TimedOut": "⏰",
    "Skipped": "⏭️",
    "Omitted": "⊘",
}
_RUN_GLYPHS = {
    "Succeeded": "✅",
    "Failed": "❌",
    "Running": "🔄",
    "Pending": "🟡",
    "Cancelled": "🚫",
    "TimedOut": "⏰",
    "Skipped": "⏭️",
}


# Keys an origin may use for the backoff hint, and the one-level envelopes we
# have seen it wrapped in. Heracles puts it at the top level; AE's generic
# error shape nests the detail under ``data`` / ``error``.
_RETRY_AFTER_KEYS = ("retry_after", "retryAfter")
_RETRY_AFTER_ENVELOPE_KEYS = ("data", "error", "detail")


def _requested_retry_after(body: dict[str, Any] | str | None) -> int | None:
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
class _RetryGap:
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


def _retry_gap(
    requested: float | None,
    *,
    default_seconds: int,
    budget_left: int,
) -> _RetryGap:
    """Pick the wait before the next attempt, honouring the origin's request.

    Returns the caller's ``default_seconds`` when the origin asked for nothing
    usable. Otherwise returns the requested wait clamped to
    ``_MAX_RETRY_AFTER_SECONDS`` and to ``budget_left`` — and never below
    ``default_seconds``, so honouring a hint can only ever lengthen the gap,
    never shorten it below what the loop already guaranteed.

    Args:
        requested: Seconds the origin asked for, from
            :func:`_requested_retry_after` or an error's
            ``retry_after_seconds``. ``None`` / non-positive means no request.
        default_seconds: The loop's own fixed gap — the floor, and the answer
            when there is no request.
        budget_left: Seconds of above-the-floor waiting still permitted in
            this loop. Zero (or less) degrades cleanly to ``default_seconds``.
    """
    if requested is None or requested <= 0:
        return _RetryGap(seconds=default_seconds, requested=None)
    wanted = math.ceil(requested)
    allowed = min(wanted, _MAX_RETRY_AFTER_SECONDS, max(budget_left, 0))
    return _RetryGap(seconds=max(default_seconds, allowed), requested=wanted)


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

    Returned by :meth:`AEWorkflowClient._post_with_retry`'s
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


def _parse_run_timestamp(raw: object) -> datetime | None:
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


def _newest_run_since(body: dict[str, Any], floor: datetime) -> str | None:
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
        created = _parse_run_timestamp(row.get("created_at"))
        if created is not None and created >= floor:
            return run_id
    return None


def _classify_delivery(exc: Exception) -> RequestDelivery:
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
      pool. Unreachable today (``_request`` builds a single-use client) but
      classified for correctness rather than left to the ambiguous default.
    """
    if isinstance(exc, httpx.ConnectTimeout | httpx.ConnectError | httpx.PoolTimeout):
        return RequestDelivery.NOT_DELIVERED
    return RequestDelivery.AMBIGUOUS


def _is_credential_name_conflict(status: int, body: dict[str, Any] | str) -> bool:
    """True iff *body* is AE's unique-constraint violation on the credential name.

    AE (Heracles) creates the submit credential non-idempotently, keyed on a
    UNIQUE ``credentials_name_key``. A submit retried after a transient (a 5xx,
    or a timeout that AE actually committed) re-sends the same credential name
    and trips this constraint as an HTTP 400 — recoverable by rotating the name
    (see :func:`_rotate_submit_credential_name`) and retrying, rather than
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


def _rotate_submit_credential_name(body: dict[str, Any] | None) -> None:
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


def _node_glyph(node) -> str:
    """Format one node as ``glyph name`` for the poll-loop summary."""
    g = _NODE_GLYPHS.get(node.status.value, "❔")
    # Trim long node names so the per-poll line stays scannable
    name = node.name.replace("lineage-publish", "lin-pub").replace(
        "lineage-app", "lin-app"
    )
    # Space between glyph and name — colour emoji renders wider than
    # the monochrome glyphs we used before, so the previous tight
    # "✓extract" lost legibility.
    return f"{g} {name}"


# AE returns "a run for workflow '<slug>' is already active" (code AE-WF-409-03)
# when a submit collides with an in-flight run. Heracles (the tenant-facing
# proxy in front of Automation Engine) masks that 409 as an HTTP 500 with the
# original 409 text embedded in the message, so we detect the conflict by its
# stable error code regardless of the outer status. We match on the code alone
# (not the generic "already active" prose): the code is unambiguous, whereas the
# phrase could appear in an unrelated, genuinely-transient 5xx and wrongly mark
# it terminal.
_ALREADY_ACTIVE_CODE = "AE-WF-409-03"


def _is_already_active_run(status: int, body: Any) -> bool:
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
# _is_credential_name_conflict: if the wire text changes the match silently
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


def _is_app_not_ready(status: int, body: Any) -> bool:
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
    """Re-size :meth:`AEWorkflowClient.submit_workflow`'s retry to a cold start.

    Shared by both full-DAG harnesses (``BaseE2ETest.run_full_dag`` and the
    deprecated ``BaseFullDAGE2ETest.run_full_dag``) so a DIRECT-mode submit
    against a freshly-installed tenant app gets the same budget in either.

    A refused dial to a still-booting tenant app pod arrives as a generic
    retryable 5xx, so ``submit_workflow`` already retries it (see
    :func:`_is_app_not_ready`) — only its default 4x5s budget is too short for a
    pod cold start. Widening that existing loop keeps ONE retry path for the
    submit, which matters because the submit is non-idempotent: a second loop
    wrapped around it would re-enter the inner retry per outer attempt (5x the
    POSTs it reports) and would bypass ``retry_after`` honouring and the
    credential-name rotation that make each re-POST safe.

    Args:
        timeout_seconds: Total cold-start budget (the harness'
            ``app_ready_timeout_seconds``). 0 or negative returns no overrides,
            leaving ``submit_workflow``'s own defaults in place.
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


class DAGNodeStatus(str, Enum):
    """Status values returned by ``native-status`` per DAG node."""

    PENDING = "Pending"
    SCHEDULED = "Scheduled"
    RUNNING = "Running"
    SUCCEEDED = "Succeeded"
    FAILED = "Failed"
    ERROR = "Error"
    CANCELLED = "Cancelled"
    # AE reports Skipped/Omitted for DAG nodes it intentionally did not run —
    # e.g. an opted-out DAG leg, the qi + lineage nodes when a crawl runs with
    # lineage disabled, or every downstream node once an upstream one fails.
    # These are terminal and NOT failures; the skip-tolerant gate
    # (BaseE2ETest._core_dag_ok) treats them as acceptable when lineage isn't
    # expected. Kept as explicit members so they no longer fall through
    # _safe_node_status to PENDING (which would hang the poll's "not started"
    # reasoning and false-fail all_nodes_succeeded). A skipped node will not run
    # without re-submission.
    SKIPPED = "Skipped"
    OMITTED = "Omitted"

    @property
    def is_terminal(self) -> bool:
        """True if this status will not change without re-submission."""
        return self in {
            DAGNodeStatus.SUCCEEDED,
            DAGNodeStatus.FAILED,
            DAGNodeStatus.ERROR,
            DAGNodeStatus.CANCELLED,
            DAGNodeStatus.SKIPPED,
            DAGNodeStatus.OMITTED,
        }

    @property
    def is_success(self) -> bool:
        """True when the node completed without error."""
        return self is DAGNodeStatus.SUCCEEDED

    @property
    def is_skipped(self) -> bool:
        """True when AE intentionally did not run the node (not a failure)."""
        return self in {DAGNodeStatus.SKIPPED, DAGNodeStatus.OMITTED}

    @property
    def is_not_started(self) -> bool:
        """True while AE has not handed the node to a worker.

        A node in this set has *not failed* — it was never dispatched. The
        distinction matters in every diagnostic: a ``Pending`` node at the poll
        ceiling means nothing was polling its task queue, whereas a ``Failed``
        one ran and errored. Reporting the first as the second sends the
        operator looking for a bug in code that never executed.
        """
        return self in {DAGNodeStatus.PENDING, DAGNodeStatus.SCHEDULED}


class DAGRunStatus(str, Enum):
    """Top-level status of an AE workflow run."""

    PENDING = "Pending"
    RUNNING = "Running"
    SUCCEEDED = "Succeeded"
    FAILED = "Failed"
    ERROR = "Error"
    CANCELLED = "Cancelled"
    # A run AE never executed — e.g. deduplicated against an in-flight run, or
    # every node opted out. Terminal: recognising it lets poll_native_status
    # return the true outcome immediately instead of mapping the unknown value
    # to PENDING and waiting out the full stall grace, which surfaces as a
    # misleading NoWorkerOnTaskQueueError.
    SKIPPED = "Skipped"

    @property
    def is_terminal(self) -> bool:
        return self in {
            DAGRunStatus.SUCCEEDED,
            DAGRunStatus.FAILED,
            DAGRunStatus.ERROR,
            DAGRunStatus.CANCELLED,
            DAGRunStatus.SKIPPED,
        }


@dataclass(frozen=True)
class DAGNodeResult:
    """One row of the per-node breakdown returned by ``native-status``."""

    name: str
    status: DAGNodeStatus
    started_at_ms: int | None
    completed_at_ms: int | None
    error_message: str | None

    @property
    def duration_seconds(self) -> float | None:
        """Wall time if both endpoints are populated."""
        if self.started_at_ms is None or self.completed_at_ms is None:
            return None
        return (self.completed_at_ms - self.started_at_ms) / 1000.0


@dataclass(frozen=True)
class DAGRunResult:
    """Full result returned by :meth:`AEWorkflowClient.poll_native_status`."""

    run_id: str
    workflow_slug: str
    status: DAGRunStatus
    nodes: list[DAGNodeResult]
    # Set only on the observation ``poll_native_status`` returns because it hit
    # its own ceiling, so callers can say "the DAG did not complete in Xs"
    # instead of reporting the last-seen node states as node failures. ``None``
    # on every result that came back from a terminal run.
    timed_out_after_seconds: float | None = None
    # Elapsed poll time since the last node-state transition, at the moment the
    # ceiling was hit. This is the DAG-wide watchdog clock (the same quantity
    # ``dag_progress_stall_seconds`` bounds), not a per-node age: it answers
    # "how long has this DAG been frozen in the state printed below".
    seconds_since_last_progress: float | None = None

    @property
    def all_nodes_succeeded(self) -> bool:
        return bool(self.nodes) and all(n.status.is_success for n in self.nodes)

    @property
    def timed_out(self) -> bool:
        """True when this observation is the poll loop's ceiling, not a verdict."""
        return self.timed_out_after_seconds is not None

    @property
    def failed_nodes(self) -> list[DAGNodeResult]:
        """Every node that did not succeed — failed *and* never-started alike.

        Kept as the wide "not successful" set the success gates gate on. Use
        :attr:`not_started_nodes` when the message needs to tell the operator
        which of the two happened.
        """
        return [n for n in self.nodes if not n.status.is_success]

    @property
    def not_started_nodes(self) -> list[DAGNodeResult]:
        """Nodes AE never dispatched (``Pending`` / ``Scheduled``)."""
        return [n for n in self.nodes if n.status.is_not_started]


class AEWorkflowClient:
    """Thin wrapper over the three Atlan endpoints used by full-DAG tests.

    Stateless aside from caching the auth token. Methods are idempotent
    and safe to retry.

    Args:
        tenant_url: Base URL of the tenant (e.g. ``https://devex.atlan.com``).
            Trailing slash is stripped if present.
        api_token: Bearer token used for AE / Atlas REST calls. Accepts
            either a long-lived API key or a short-lived OAuth
            ``client_credentials`` access token.
        oauth_client_id / oauth_client_secret: Optional OAuth client pair.
            When supplied, the lazily-constructed pyatlan ``AtlanClient``
            (used for asset search + role-cache lookups) authenticates via
            OAuth ``client_credentials`` instead of the bearer api_token.
            This yields a *different* service-account identity than the
            API key — useful when the API key's service account isn't on
            an asset's admin ACL but the OAuth client is.
    """

    def __init__(
        self,
        tenant_url: str,
        api_token: str,
        *,
        oauth_client_id: str | None = None,
        oauth_client_secret: str | None = None,
    ) -> None:
        self.tenant_url = tenant_url.rstrip("/")
        self._api_token = api_token
        self._oauth_client_id = oauth_client_id
        self._oauth_client_secret = oauth_client_secret

    # ------------------------------------------------------------------
    # Low-level HTTP
    # ------------------------------------------------------------------

    def _request(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        timeout: int = _HTTP_TIMEOUT,
        retry_network_errors: bool = True,
    ) -> tuple[int, dict[str, Any] | str]:
        """HTTP request returning ``(status_code, parsed_body_or_text)``.

        A 4xx/5xx is a real response, not a failure: it is returned to the
        caller so the caller's status-based retry/handling applies. Only a
        transport-layer failure (DNS blip, connect/read timeout, reset) is
        retried here, and a sustained one surfaces as
        :class:`AtlanApiTimeoutError` (an :class:`AppError`) so callers'
        transient-tolerance — e.g. the poll loop — handles it instead of a raw
        crash.

        ``retry_network_errors=False`` disables that retry entirely: the caller
        is a non-idempotent write (submit) whose re-POST decision belongs to
        the single retry loop in :meth:`_post_with_retry`, not to a nested one
        here. The raised error carries a
        :class:`~application_sdk.testing.e2e._errors.RequestDelivery` telling
        that loop whether the origin can have seen the request — which is what
        makes a connect-phase failure safely retryable there.
        """
        url = f"{self.tenant_url}{path}"
        content = orjson.dumps(body) if body is not None else None
        headers = {
            "Authorization": f"Bearer {self._api_token}",
            "Accept": "application/json",
            "User-Agent": _USER_AGENT,
        }
        if body is not None:
            headers["Content-Type"] = "application/json"
        last_exc: Exception | None = None
        delivery = RequestDelivery.AMBIGUOUS
        for attempt in range(1, _REQUEST_MAX_ATTEMPTS + 1):
            try:
                # A fresh client per call, matching the previous urllib
                # behaviour of one connection per request: a pooled connection
                # that is already half-dead would otherwise be reused across
                # the retry, which is the exact condition we retry to escape.
                # follow_redirects preserves urlopen's redirect handling, which
                # httpx disables by default.
                with httpx.Client(
                    timeout=httpx.Timeout(timeout), follow_redirects=True
                ) as http:
                    resp = http.request(method, url, content=content, headers=headers)
                raw = resp.content
                try:
                    return resp.status_code, orjson.loads(raw)
                except orjson.JSONDecodeError:
                    logger.warning(
                        "Response body is not JSON; returning raw text",
                        exc_info=True,
                    )
                    return resp.status_code, raw.decode(errors="replace")
            except (httpx.TransportError, OSError) as e:
                # Transport-layer error — common on multi-minute polls over a
                # VPN/loft tunnel, and on CI runners whose node-local egress
                # SNAT intermittently blackholes a public-FQDN hairpin.
                last_exc = e
                delivery = _classify_delivery(e)
                if not retry_network_errors:
                    # Non-idempotent caller (submit): never re-issue from here.
                    # _post_with_retry owns that decision and now has the
                    # delivery classification it needs to make it safely.
                    break
                if attempt < _REQUEST_MAX_ATTEMPTS:
                    logger.warning(
                        "transient network error on %s %s (attempt %d/%d, "
                        "delivery=%s): %s — retrying in %ds",
                        method,
                        path,
                        attempt,
                        _REQUEST_MAX_ATTEMPTS,
                        delivery.value,
                        e,
                        _REQUEST_BACKOFF_SECONDS,
                        exc_info=True,
                    )
                    time.sleep(_REQUEST_BACKOFF_SECONDS)
        raise AtlanApiTimeoutError(
            message=(
                # `attempt` is the actual count made — 1 when retries are
                # disabled (submit), up to _REQUEST_MAX_ATTEMPTS otherwise — so
                # a submit timeout doesn't misreport 4 tries when it made 1.
                f"{method} {path} failed after {attempt} attempt(s) "
                f"[delivery={delivery.value}]: {last_exc!r}"
            ),
            operation=path,
            delivery=delivery,
        ) from last_exc

    # ------------------------------------------------------------------
    # Endpoints
    # ------------------------------------------------------------------

    def _post_with_retry(
        self,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        total_attempts: int,
        sleep_seconds: int,
        retryable: Callable[[int, dict[str, Any] | str], bool],
        op_name: str,
        mutate_before_retry: Callable[[dict[str, Any] | None], None] | None = None,
        retry_network_errors: bool = True,
        recover_ambiguous: Callable[[], WriteRecovery] | None = None,
    ) -> tuple[int, dict[str, Any] | str]:
        """POST *path* with unified timeout + retry, returning ``(status, body)``.

        Centralises the _SUBMIT_TIMEOUT budget, TimeoutError/OSError retry,
        and HTTP-status-based retry that every AE write endpoint shares.
        The caller inspects the returned ``(status, body)`` to extract the
        expected value or raise an endpoint-specific error.

        Args:
            path: URL path relative to ``self.tenant_url``.
            body: Optional JSON-serialisable request body.
            total_attempts: Maximum number of calls (1 initial + N retries).
            sleep_seconds: Seconds to sleep between attempts. A response that
                asks for a longer wait via ``retry_after`` overrides it for
                that attempt, bounded by ``_MAX_RETRY_AFTER_SECONDS`` and the
                per-call ``_RETRY_AFTER_BUDGET_SECONDS`` (see
                :func:`_retry_gap`). A timeout has no body to read, so it
                always uses the fixed gap.
            retryable: Called with ``(status, body)`` after each response.
                Return True to retry — works for both non-2xx status codes
                and 2xx responses with an unexpected body shape.  Return
                False to accept the response and return it to the caller.
            op_name: Human-readable label used in log / error messages.
            mutate_before_retry: Optional callback invoked with ``body`` just
                before each retry (mutates it in place). Lets a caller make a
                retry idempotent — e.g. rotate a non-idempotent credential name
                so a re-sent submit can't collide on a unique constraint.
            retry_network_errors: When False, an *ambiguous* network failure is
                never re-POSTed (nor re-issued at the ``_request`` layer) —
                required for non-idempotent writes like submit, where a retry
                after the server already accepted the request spawns a
                duplicate run. A failure classified
                :attr:`~application_sdk.testing.e2e._errors.RequestDelivery.NOT_DELIVERED`
                is re-POSTed regardless: the connection was never established,
                so the origin provably never saw the request and no duplicate
                is possible. Those re-POSTs are counted against the same
                ``total_attempts`` budget as every other retry, keeping the
                single-retry-loop invariant that
                :func:`cold_start_submit_kwargs` depends on.
            recover_ambiguous: Optional read of the origin's own state, called
                when a network failure leaves it unknown whether the write took
                effect. Return the response body the origin *would* have sent
                if the write is found to have landed — it is returned to the
                caller as a 200 and no retry happens. Return ``None`` if the
                write provably left no trace, which reclassifies the failure as
                :attr:`~application_sdk.testing.e2e._errors.RequestDelivery.NOT_APPLIED`
                and makes a re-POST safe even for a non-idempotent write. Only
                consulted for a genuinely ambiguous failure: a connect-phase
                one needs no read, and an idempotent caller needs no proof.
        """
        last: tuple[int, dict[str, Any] | str] = (0, {})
        # Seconds spent waiting *beyond* the fixed gap because the origin asked
        # for longer. Bounds the total honoured backoff across the whole call.
        honoured_seconds = 0
        for attempt in range(1, total_attempts + 1):
            try:
                status, resp_body = self._request(
                    "POST",
                    path,
                    body=body,
                    timeout=_SUBMIT_TIMEOUT,
                    retry_network_errors=retry_network_errors,
                )
            except (TimeoutError, OSError, AtlanApiTimeoutError) as exc:
                # Only _request classifies delivery; a bare TimeoutError/OSError
                # reaching here (a patched _request in tests, or a caller that
                # raised before classification) keeps the conservative default.
                delivery = (
                    exc.delivery
                    if isinstance(exc, AtlanApiTimeoutError)
                    else RequestDelivery.AMBIGUOUS
                )
                if (
                    delivery is RequestDelivery.AMBIGUOUS
                    and recover_ambiguous is not None
                ):
                    # Ask the origin what actually happened rather than
                    # guessing from the transport error.
                    recovered = recover_ambiguous()
                    if recovered.body is not None:
                        logger.info(
                            "%s attempt %d/%d: the connection died but the "
                            "write had already landed — adopting the origin's "
                            "own record instead of re-issuing",
                            op_name,
                            attempt,
                            total_attempts,
                        )
                        return 200, recovered.body
                    if recovered.proven_absent:
                        delivery = RequestDelivery.NOT_APPLIED
                # A request the origin never received, or never acted on, is
                # safe to re-POST even when the caller forbids retrying
                # genuinely ambiguous ones.
                may_repost = retry_network_errors or delivery in (
                    RequestDelivery.NOT_DELIVERED,
                    RequestDelivery.NOT_APPLIED,
                )
                if may_repost and attempt < total_attempts:
                    logger.warning(
                        "%s attempt %d/%d: timeout (delivery=%s) (%s) — "
                        "retrying in %ds",
                        op_name,
                        attempt,
                        total_attempts,
                        delivery.value,
                        exc,
                        sleep_seconds,
                        exc_info=True,
                    )
                    if mutate_before_retry is not None:
                        mutate_before_retry(body)
                    time.sleep(sleep_seconds)
                    continue
                # Name why we stopped. "after 1 attempt(s)" on its own reads
                # like broken retry logic; it is in fact the non-idempotency
                # guard declining to re-issue a request the origin may have
                # already processed.
                halted = (
                    " — not re-issued: this write is non-idempotent and the "
                    "connection had already been established when it failed, "
                    "so the origin may already have processed it"
                    if not may_repost
                    else ""
                )
                raise AtlanApiTimeoutError(
                    # `attempt` is the actual count made — 1 when an ambiguous
                    # failure halts a non-idempotent write, up to total_attempts
                    # otherwise — so it doesn't misreport total_attempts.
                    message=(
                        f"{op_name} timed out after {attempt} attempt(s) "
                        f"[delivery={delivery.value}]{halted}"
                    ),
                    operation=path,
                    delivery=delivery,
                ) from exc
            last = (status, resp_body)
            is_retry = retryable(status, resp_body)
            if not is_retry and status < 300:
                if attempt > 1:
                    # conformance: ignore[L006] fires at most once per call (guarded by attempt>1, then returns), not per-iteration volume — a meaningful success-after-retry event
                    logger.info(
                        "%s succeeded on attempt %d/%d",
                        op_name,
                        attempt,
                        total_attempts,
                    )
                return last
            if is_retry and attempt < total_attempts:
                gap = _retry_gap(
                    _requested_retry_after(resp_body),
                    default_seconds=sleep_seconds,
                    budget_left=_RETRY_AFTER_BUDGET_SECONDS - honoured_seconds,
                )
                honoured_seconds += gap.seconds - sleep_seconds
                logger.warning(
                    "%s attempt %d/%d: HTTP %d — retrying in %ds%s  body=%r",
                    op_name,
                    attempt,
                    total_attempts,
                    status,
                    gap.seconds,
                    gap.origin_note,
                    resp_body,
                )
                if mutate_before_retry is not None:
                    mutate_before_retry(body)
                time.sleep(gap.seconds)
                continue
            break
        return last

    def create_workflow(
        self,
        name: str,
        description: str = "",
        *,
        retries: int = 4,
        retry_sleep_seconds: int = 5,
    ) -> str:
        """POST ``/automation/api/v1/workflows`` — create or upsert a workflow.

        AE doesn't auto-create workflows on submit: a fresh slug → HTTP
        404 ("Workflow with slug 'X' not found. Create the workflow
        first."). So every full-DAG run begins by creating (or
        re-creating) the workflow under a stable name. The endpoint is
        idempotent on name — submitting the same name returns the
        existing workflow's slug.

        Retries on HTTP 5xx and timeout. 4 retries at 5s intervals covers
        the typical AE recovery window without sitting on a hard failure —
        and when an overloaded origin names its own window via
        ``retry_after``, the gap stretches to match it (see
        :func:`_retry_gap`) instead of expiring the whole budget inside it.

        Returns:
            The workflow slug (used by subsequent version + submit calls).
        """
        status, body = self._post_with_retry(
            "/automation/api/v1/workflows",
            body={"name": name, "description": description},
            total_attempts=retries + 1,
            sleep_seconds=retry_sleep_seconds,
            retryable=lambda s, b: s >= 500,
            op_name="create_workflow",
        )
        if status < 300 and isinstance(body, dict):
            data = body.get("data") if isinstance(body.get("data"), dict) else body
            slug = data.get("slug") if isinstance(data, dict) else None
            if not slug:
                raise AtlanApiResponseInvariantError(
                    message=f"create_workflow returned no slug\nresponse={body!r}",
                    expectation="slug present in create_workflow response",
                )
            return str(slug)
        raise AtlanApiHttpError(
            message=f"create_workflow failed: HTTP {status}\nresponse={body!r}",
            target=f"POST /automation/api/v1/workflows HTTP {status}",
            retry_after_seconds=_requested_retry_after(body),
        )

    def create_version(
        self,
        slug: str,
        version_payload: dict[str, Any],
        *,
        retries: int = 5,
        retry_sleep_seconds: int = 5,
    ) -> int:
        """POST ``/automation/api/v1/workflows/<slug>/versions`` — create a version.

        The version carries the full DAG manifest (extract / qi / publish
        / lineage-app / lineage-publish nodes). A workflow must have at
        least one *published* version before package-workflows submit
        will accept a run against it.

        Retries on HTTP 404 (indexing lag — slug not yet queryable after
        create_workflow), HTTP 5xx (AE under load), and timeout.

        Returns:
            The version number assigned by AE (typically a Unix
            timestamp, but treat as opaque int).
        """
        status, body = self._post_with_retry(
            f"/automation/api/v1/workflows/{slug}/versions",
            body=version_payload,
            total_attempts=retries,
            sleep_seconds=retry_sleep_seconds,
            retryable=lambda s, b: s == 404 or s >= 500,
            op_name="create_version",
        )
        if status < 300 and isinstance(body, dict):
            data = body.get("data") if isinstance(body.get("data"), dict) else body
            version = data.get("version") if isinstance(data, dict) else None
            if version is not None:
                return int(version)
        raise AtlanApiHttpError(
            message=f"create_version failed: HTTP {status}\nresponse={body!r}",
            target=f"POST /automation/api/v1/workflows/.../versions HTTP {status}",
            retry_after_seconds=_requested_retry_after(body),
        )

    def publish_version(
        self,
        slug: str,
        version: int,
        *,
        retries: int = 5,
        retry_sleep_seconds: int = 5,
    ) -> None:
        """POST ``/automation/api/v1/workflows/<slug>/versions/<v>/publish``.

        AE can lag a few seconds between version-create and version-
        publish — early calls return 404 (AE-WF-404-02 "version not
        found"). Retries on any non-success response and timeout.
        """
        status, body = self._post_with_retry(
            f"/automation/api/v1/workflows/{slug}/versions/{version}/publish",
            total_attempts=retries,
            sleep_seconds=retry_sleep_seconds,
            retryable=lambda s, b: (
                s >= 300 or not (isinstance(b, dict) and b.get("status") == "success")
            ),
            op_name="publish_version",
        )
        if status < 300 and isinstance(body, dict) and body.get("status") == "success":
            return
        raise AtlanApiHttpError(
            message=f"publish_version failed after {retries} attempts: {body!r}",
            target="POST /automation/api/v1/workflows/.../versions/.../publish",
            retry_after_seconds=_requested_retry_after(body),
        )

    def find_run_created_since(
        self,
        slug: str,
        since: datetime,
        *,
        timeout_seconds: int = _RECONCILE_TIMEOUT_SECONDS,
        interval_seconds: int = _RECONCILE_INTERVAL_SECONDS,
    ) -> RunLookup:
        """Poll ``GET /automation/api/v1/runs`` for a run of *slug* created at or
        after *since*.

        This is how an ambiguous submit gets resolved: AE writes the run record
        before it answers the submit, so asking AE what runs exist answers the
        question the timeout left open — did the write take effect? A found run
        is adopted (the DAG really is executing; only the response was lost),
        and a genuinely absent one makes a re-POST safe.

        Polls rather than probing once, because production AE serves this list
        from Elasticsearch and a just-committed run needs a moment to become
        searchable. Returns as soon as one appears.

        Args:
            slug: Workflow slug to look under. Unique per CI leg, which is what
                makes "a run exists under this slug" mean "our run exists".
            since: Wall-clock instant the submit was issued. Timezone-aware;
                :data:`_RECONCILE_CLOCK_SKEW_SECONDS` is subtracted internally
                to absorb runner-vs-tenant clock skew.
            timeout_seconds: Total budget for the poll.
            interval_seconds: Gap between polls.

        Returns:
            A :class:`RunLookup`. ``run_id`` is set when a matching run was
            found. ``conclusive`` distinguishes the two ways of finding none:
            AE answered and had no such run (proof of absence, safe to act on)
            versus AE never answered at all (proves nothing).
        """
        floor = since - timedelta(seconds=_RECONCILE_CLOCK_SKEW_SECONDS)
        # A read that errored proves nothing about whether the run exists. If
        # the same network fault that killed the submit also kills every
        # reconcile read, the answer must stay "unknown" — reporting it as
        # "absent" would authorise the duplicate submit this exists to prevent.
        answered = False
        # include_test_runs / include_system both widen an otherwise
        # default-filtered listing. Widening is free here: the slug already
        # restricts the result set to this leg's own runs, so the only effect
        # is that we cannot miss our run because of how AE classified it.
        path = (
            f"/automation/api/v1/runs?workflow_slug={quote(slug, safe='')}"
            f"&page=0&page_size={_RECONCILE_PAGE_SIZE}"
            "&include_test_runs=true&include_system=true"
        )
        for attempt in until_deadline(
            timeout_seconds,
            interval_seconds,
            label=f"an AE run under slug '{slug}' created since {floor.isoformat()}",
        ):
            try:
                status, body = self._request("GET", path)
            except AppError:
                # The reconcile read is itself best-effort: a tenant that
                # cannot answer it leaves the submit exactly as ambiguous as
                # before, which the caller already handles. Never let it
                # replace the submit failure the operator needs to see.
                logger.warning(
                    "reconcile read failed for slug %s — treating this attempt "
                    "as inconclusive",
                    slug,
                    exc_info=True,
                )
                continue
            if status >= 300 or not isinstance(body, dict):
                logger.warning(
                    "reconcile read for slug %s returned HTTP %d — treating "
                    "this attempt as inconclusive\nresponse=%r",
                    slug,
                    status,
                    body,
                )
                continue
            answered = True
            run_id = _newest_run_since(body, floor)
            if run_id is not None:
                return RunLookup(run_id=run_id, conclusive=True)
        if answered:
            logger.warning(
                "AE reports no run under slug %s created since %s across a "
                "%ds window — the submit left no trace",
                slug,
                floor.isoformat(),
                timeout_seconds,
            )
        else:
            logger.warning(
                "no reconcile read of slug %s succeeded within %ds — whether "
                "the submit landed stays unknown",
                slug,
                timeout_seconds,
            )
        return RunLookup(conclusive=answered)

    def probe_run_is_listed(self, slug: str, run_id: str) -> bool | None:
        """Log-only check that *run_id* is visible in AE's run listing for *slug*.

        Exists to settle :data:`_RESUBMIT_WHEN_AE_REPORTS_NO_RUN` with evidence
        instead of waiting for a rare failure to produce it. The reconcile read
        only fires on an ambiguous submit timeout, so on its own it would tell
        us nothing until the next such timeout — and only for that one leg.
        This probe runs on the *success* path, where we already know the true
        run id, and asks the one question the gate turns on: does a run
        submitted through Heracles show up in a listing that filters on
        automation-engine fields?

        Call it after the DAG poll has finished, not right after the submit:
        production AE serves the listing from Elasticsearch, and an immediate
        probe would report a lag as an absence — the exact false negative the
        gate exists to avoid, recorded as if it were the answer.

        Never raises and never affects the run's outcome.

        Returns:
            ``True`` if AE listed the run, ``False`` if it answered without it,
            ``None`` if the read did not get through (which settles nothing).
        """
        path = (
            f"/automation/api/v1/runs?workflow_slug={quote(slug, safe='')}"
            f"&page=0&page_size={_RECONCILE_PAGE_SIZE}"
            "&include_test_runs=true&include_system=true"
        )
        try:
            status, body = self._request("GET", path)
        except AppError:
            logger.warning(
                "AE-listing probe for slug %s did not get through; whether a "
                "Heracles-submitted run is listable stays unanswered",
                slug,
                exc_info=True,
            )
            return None
        if status >= 300 or not isinstance(body, dict):
            logger.warning(
                "AE-listing probe for slug %s returned HTTP %d; whether a "
                "Heracles-submitted run is listable stays unanswered\n"
                "response=%r",
                slug,
                status,
                body,
            )
            return None
        rows = body.get("data")
        listed = (
            [r.get("guid") or r.get("run_id") for r in rows if isinstance(r, dict)]
            if isinstance(rows, list)
            else []
        )
        if run_id in listed:
            logger.info(
                "AE-listing probe: run %s IS listed under slug %s — a "
                "Heracles-submitted run is discoverable via "
                "GET /automation/api/v1/runs, so reconciliation can trust an "
                "empty read as proof of absence (FND-676: this is the evidence "
                "_RESUBMIT_WHEN_AE_REPORTS_NO_RUN waits on)",
                run_id,
                slug,
            )
            return True
        logger.warning(
            "AE-listing probe: run %s is NOT listed under slug %s (AE returned "
            "%d run(s): %r) — reconciliation must NOT treat an empty read as "
            "proof of absence; keep _RESUBMIT_WHEN_AE_REPORTS_NO_RUN off "
            "(FND-676)",
            run_id,
            slug,
            len(listed),
            listed,
        )
        return False

    def submit_workflow(
        self,
        payload: dict[str, Any],
        *,
        slug: str = "",
        retries: int = 4,
        retry_sleep_seconds: int = 5,
    ) -> str:
        """POST ``/api/service/package-workflows?submit=true``.

        Returns the run UUID from the submit response. The submit
        response shape is not officially documented; we look for
        ``run_id`` under either the top level or a nested ``data`` key.

        Retries an AE ``credentials_name_key`` conflict, rotating the
        credential name first so the re-sent submit doesn't collide (AE creates
        the credential non-idempotently per attempt).

        Unlike the other write endpoints, a submit is **not idempotent**:
        re-issuing one AE already accepted spawns a duplicate run that AE marks
        ``Skipped`` and returns as a *fresh* run_id — so a blind retry makes the
        harness poll a phantom skipped run while the real one runs to
        completion under a different id. Three guards prevent that:

        * ``retry_network_errors=False`` — a failure *after* the connection was
          established is ambiguous (the server may have accepted the submit),
          so we never blindly re-POST on one. A connect-phase failure is
          exempt: the request provably never reached AE, so
          ``_post_with_retry`` re-POSTs it within the normal attempt budget.
          See :func:`_classify_delivery`; this is what keeps a blackholed CI
          hairpin from reding a whole e2e leg on the first packet loss.
        * ``recover_ambiguous`` — when *slug* is known, an ambiguous failure is
          resolved by asking AE what actually happened rather than guessing.
          AE writes the run record before it answers the submit, so
          :meth:`find_run_created_since` can see a run that was accepted even
          though the response never arrived: that run is adopted, and the DAG
          the harness goes on to poll is the real one. A submit called without
          a *slug*, or one whose reconcile read cannot get through, keeps the
          old fail-fast behaviour — the guard degrades to what it replaced
          rather than to a duplicate. The converse move, re-POSTing *because*
          AE reports no such run, is gated off pending live verification; see
          :data:`_RESUBMIT_WHEN_AE_REPORTS_NO_RUN`.
        * the ``already active`` conflict (AE-WF-409-03, which Heracles masks
          as a 500 — see :func:`_is_already_active_run`) is treated as terminal,
          not a retryable 5xx, and surfaced as
          :class:`AtlanAEWorkflowAlreadyActiveError`.

        Genuine 5xx that are *not* the already-active conflict remain retryable.

        A tenant app pod that is still cold-starting also answers as a generic
        retryable 5xx (see :func:`_is_app_not_ready`), so the retry above
        already covers it — but the default 4x5s budget expires long before a
        pod boots. Callers that submit against a freshly-installed tenant app
        (both harnesses' ``run_full_dag``) therefore pass a cold-start-sized
        ``retries`` / ``retry_sleep_seconds``, built by
        :func:`cold_start_submit_kwargs`; on exhaustion, a last response
        that still reads as connection-refused is surfaced as
        :class:`AppNotReadyError` rather than an opaque
        :class:`AtlanApiHttpError`, so the failure names the cause. FND-402.

        Args:
            payload: The AE submit body.
            slug: Workflow slug the payload submits against. Optional only for
                backwards compatibility — supplying it is what enables the
                reconcile-before-retry guard described above, so every caller
                that has the slug should pass it.
            retries: Retries on top of the initial attempt.
            retry_sleep_seconds: Fixed gap between attempts.
        """
        started = time.monotonic()
        # Wall clock, not the monotonic one above: this is compared against
        # timestamps AE assigns, so it has to be on the same scale.
        submitted_at = datetime.now(UTC)

        def _recover_from_ae() -> WriteRecovery:
            """Ask AE whether the submit whose response we lost took effect."""
            lookup = self.find_run_created_since(slug, submitted_at)
            if lookup.run_id is not None:
                logger.warning(
                    "submit_workflow: the response was lost but AE has run %s "
                    "under slug %s — adopting it rather than re-submitting",
                    lookup.run_id,
                    slug,
                )
                return WriteRecovery(body={"run_id": lookup.run_id})
            return WriteRecovery(
                proven_absent=lookup.conclusive and _RESUBMIT_WHEN_AE_REPORTS_NO_RUN
            )

        status, body = self._post_with_retry(
            "/api/service/package-workflows?submit=true",
            body=payload,
            total_attempts=retries + 1,
            sleep_seconds=retry_sleep_seconds,
            # Retry genuine 5xx (transient AE/Heracles errors) EXCEPT the
            # already-active conflict, which main surfaces as terminal below.
            # Also retry an AE ``credentials_name_key`` 400: AE creates the
            # credential non-idempotently on each submit, so a retry after a
            # committed transient would re-insert the same name and 400. Rotate
            # the credential name before every retry so the re-sent submit can't
            # collide. ``retry_network_errors=False`` keeps submit safe on an
            # ambiguous timeout (AE may already have accepted it).
            retryable=lambda s, b: (
                (s >= 500 and not _is_already_active_run(s, b))
                or _is_credential_name_conflict(s, b)
            ),
            op_name="submit_workflow",
            mutate_before_retry=_rotate_submit_credential_name,
            retry_network_errors=False,
            # Resolvable only when we know which slug to look under; without
            # one, an ambiguous timeout stays terminal as it always was.
            recover_ambiguous=_recover_from_ae if slug else None,
        )
        if _is_already_active_run(status, body):
            raise AtlanAEWorkflowAlreadyActiveError(
                message=(
                    "AE rejected the submit to "
                    "POST /api/service/package-workflows?submit=true: a run for "
                    "this workflow is already active (AE-WF-409-03) — AE returned "
                    "409 directly, or Heracles masked it as HTTP 500 "
                    f"(status={status}). A run IS executing, but its run_id is "
                    "unrecoverable via native-status (keyed by run_id). Not "
                    "retrying — a retry would spawn a duplicate Skipped run.\n"
                    f"response={body!r}"
                ),
            )
        if status < 300 and isinstance(body, dict):
            data = body.get("data") if isinstance(body.get("data"), dict) else body
            run_id = data.get("run_id") if isinstance(data, dict) else None
            if run_id:
                return run_id
            raise AtlanApiResponseInvariantError(
                message=f"AE submit returned no run_id\nresponse={body!r}",
                expectation="run_id present in submit response",
            )
        if _is_app_not_ready(status, body):
            elapsed = time.monotonic() - started
            raise AppNotReadyError(
                message=(
                    "the tenant app pod never accepted connections on :8000 "
                    f"across {retries + 1} submit attempt(s) over {elapsed:.0f}s. "
                    "Heracles POSTs the credential config to the tenant-deployed "
                    "pod at AE submit and it kept refusing the connection ('dial "
                    "tcp :8000: connect: connection refused'). The deployment "
                    "reconciled (prepare-tenant went green) but the pod never "
                    f"started serving HTTP in time.\nresponse={body!r}"
                ),
                attempts=retries + 1,
                elapsed_seconds=elapsed,
            )
        raise AtlanApiHttpError(
            message=f"AE submit failed: HTTP {status}\nresponse={body!r}",
            target=f"POST /api/service/package-workflows?submit=true HTTP {status}",
            retry_after_seconds=_requested_retry_after(body),
        )

    def get_native_status(self, run_id: str) -> DAGRunResult:
        """GET ``/api/service/package-workflows/native-status/<run_id>``.

        Parses the response into a typed :class:`DAGRunResult` so callers
        don't have to memorize the wire shape.
        """
        status, body = self._request(
            "GET",
            f"/api/service/package-workflows/native-status/{run_id}"
            "?execution_mode=automation-engine",
        )
        if status >= 300 or not isinstance(body, dict):
            raise AtlanApiHttpError(
                message=f"native-status failed: HTTP {status}\nresponse={body!r}",
                target=f"GET /api/service/package-workflows/native-status HTTP {status}",
                # Carried so poll_native_status can back off for as long as the
                # origin asked instead of its fixed poll cadence.
                retry_after_seconds=_requested_retry_after(body),
            )
        nodes_raw = body.get("dag_nodes") or {}
        nodes = [
            DAGNodeResult(
                name=name,
                status=_safe_node_status(n.get("status")),
                started_at_ms=_safe_int(n.get("started_at")),
                completed_at_ms=_safe_int(n.get("completed_at")),
                error_message=n.get("error_message"),
            )
            for name, n in sorted(nodes_raw.items())
        ]
        return DAGRunResult(
            run_id=str(body.get("run_id", run_id)),
            workflow_slug=str(body.get("workflow_slug", "")),
            status=_safe_run_status(body.get("status")),
            nodes=nodes,
        )

    def poll_native_status(
        self,
        run_id: str,
        *,
        interval_seconds: int = 10,
        timeout_seconds: int = 600,
        max_transient_failures: int = 5,
        stall_grace_seconds: int | None = None,
        stall_task_queue: str = "",
        progress_stall_seconds: int | None = None,
    ) -> DAGRunResult:
        """Poll until the run reaches a terminal top-level status.

        Logs a one-line summary per poll only when the status string
        changes (i.e. progress moments), to avoid spamming logs during
        long-running publish / lineage stages.

        Tolerates transient HTTP failures from ``get_native_status``:
        the tenant's Temporal occasionally blips during multi-minute
        runs and AE then returns ``AE-COMMON-500-01: An unexpected
        error occurred`` for a few seconds before recovering. We log
        a warning and keep polling rather than failing the whole test
        on a single bad response. After ``max_transient_failures``
        consecutive errors we give up and re-raise — that's a
        sustained outage, not a blip, and there's no point waiting.
        When the failing response names a ``retry_after``, the next poll
        waits that long instead of ``interval_seconds``, so an origin asking
        for a 2-minute backoff doesn't consume the whole failure streak
        inside its own wait window. Honoured waiting is capped per poll loop
        at ``_RETRY_AFTER_BUDGET_SECONDS`` (same accounting as
        ``_post_with_retry``) and each sleep is clamped to the remaining
        ``timeout_seconds`` budget, so the loop never sleeps past its own
        deadline.

        Fail-fast stall guard: when ``stall_grace_seconds`` is a positive int
        (``None`` or any value ``<= 0`` disables it) and NO DAG node has left the
        not-started set (``Pending`` / ``Scheduled``) within that window, raise
        :class:`NoWorkerOnTaskQueueError` instead of hanging
        for the full ``timeout_seconds``. The parent AE workflow runs on the
        always-on automation-engine queue, so the top-level run flips to
        ``Running`` even when the connector's ``extract`` node is stuck because
        no worker polls its task queue — hence the check is on node-level start,
        not the run status. ``stall_task_queue`` is included in the error
        message so the operator can see which queue had no worker.

        Progress watchdog: when ``progress_stall_seconds`` is a positive int
        (``None`` / ``<= 0`` disables it), the run fails fast if NO DAG node
        changes state for that window *after* at least one node has started.
        This catches a node that began but is wedged ``Running`` (e.g. an
        extract stuck on a slow/failing upload) — the start-stall guard above
        is a one-time latch and would miss it, so the harness would otherwise
        poll the full ``timeout_seconds``. The window is deliberately wide
        (well above a legitimately slow single node — lineage on deep queues
        can sit ``Running`` for many minutes) so healthy runs never trip it;
        it exists to turn an indefinite hang into a fast, self-terminating
        failure with the last-seen node states, instead of a manual cancel.

        On hitting ``timeout_seconds`` the last observation is returned rather
        than raised — but stamped with ``timed_out_after_seconds`` and
        ``seconds_since_last_progress`` so the caller can report "the DAG did
        not complete within Xs" and tell a never-dispatched ``Pending`` node
        apart from one that ran and failed. Callers must therefore check
        :attr:`DAGRunResult.timed_out` before treating the node states as a
        verdict.
        """
        last_summary: str | None = None
        last_result: DAGRunResult | None = None
        transient_streak = 0
        # Seconds spent waiting *beyond* the poll interval because the origin
        # asked for longer — same accounting ``_post_with_retry`` keeps, so
        # both retry loops bound honoured backoff at _RETRY_AFTER_BUDGET_SECONDS.
        honoured_seconds = 0.0
        last_log_elapsed = 0.0  # seconds since the last info log fired
        last_elapsed = 0.0  # elapsed at the last successful observation
        any_node_started = False  # any node reached Running/terminal (stall guard)
        last_progress_elapsed = (
            0.0  # elapsed at the last node-state transition (progress watchdog)
        )
        for attempt in until_deadline(
            timeout_seconds,
            interval_seconds,
            label=f"AE run {run_id}",
            # This loop emits its own richer progress line (per node-state change
            # plus a heartbeat at the same cadence); the generic one would double it.
            heartbeat_seconds=0,
        ):
            elapsed = attempt.elapsed
            try:
                result = self.get_native_status(run_id)
            except AppError as e:
                transient_streak += 1
                if transient_streak >= max_transient_failures:
                    # conformance: ignore[L009] adds caller-invisible loop state (consecutive-failure streak count) not carried by the re-raised exception; not a duplicate of the raise site
                    logger.error(
                        "native-status failed %d times in a row — giving up: %s",
                        transient_streak,
                        e,
                        exc_info=True,
                    )
                    raise
                # Back off for as long as the origin asked when it said so,
                # rather than the poll cadence: an overloaded tenant answering
                # "retry_after: 120" would otherwise burn the whole
                # max_transient_failures streak inside its own wait window.
                # The loop's own clock advances by the real wait, so
                # timeout_seconds still bounds it.
                gap = _retry_gap(
                    e.retry_after_seconds if isinstance(e, AtlanApiHttpError) else None,
                    default_seconds=interval_seconds,
                    budget_left=_RETRY_AFTER_BUDGET_SECONDS - honoured_seconds,
                )
                honoured_seconds += gap.seconds - interval_seconds
                # ``sleep_next`` clamps to the residual budget, so a 120s
                # honoured wait against a 50s remaining budget does not block
                # the full 120s before the timeout is re-checked — and it
                # reports back the gap actually taken, for the log below.
                sleep_for = attempt.sleep_next(gap.seconds)
                logger.warning(
                    "native-status transient error (streak %d/%d): %s — sleeping %ds%s and retrying",
                    transient_streak,
                    max_transient_failures,
                    e,
                    sleep_for,
                    gap.origin_note,
                    exc_info=True,
                )
                continue
            transient_streak = 0
            last_result = result
            last_elapsed = elapsed
            # Stall guard: a node has "started" once it leaves the not-started
            # set. Tracked as a latch so a node that starts and finishes between
            # polls still counts.
            if any(not n.status.is_not_started for n in result.nodes):
                any_node_started = True
            summary = " ".join(_node_glyph(n) for n in result.nodes)
            run_glyph = _RUN_GLYPHS.get(result.status.value, "•")
            # Any change in the node-glyph summary means at least one node
            # changed state = forward progress; reset the watchdog clock.
            if summary != last_summary:
                last_progress_elapsed = elapsed
            # Log on every status change. Also emit a heartbeat every
            # ``_HEARTBEAT_SECONDS`` even when the status hasn't moved,
            # so long-running stages (lineage takes 2-5 min) don't look
            # silent in CI logs. Without the heartbeat the operator
            # can't distinguish "still polling" from "harness wedged".
            should_log = (
                summary != last_summary
                or (elapsed - last_log_elapsed) >= _HEARTBEAT_SECONDS
            )
            if should_log:
                # conformance: ignore[L006] throttled to status-changes plus a heartbeat every _HEARTBEAT_SECONDS (see comment above), not per-iteration; demoting to DEBUG would hide long-running-stage progress in CI
                logger.info(
                    "%s AE run [%3ds] %s — %s",
                    run_glyph,
                    int(elapsed),
                    result.status.value,
                    summary,
                )
                last_summary = summary
                last_log_elapsed = elapsed
            if result.status.is_terminal:
                return result
            # Fail fast when nothing has started within the grace window: the
            # run is live (parent on the AE queue) but no node has begun, which
            # almost always means no worker is polling the extract task queue.
            if (
                # only a positive grace arms the guard; None or any value <= 0
                # disables it (a negative would otherwise fire on the first poll)
                stall_grace_seconds is not None
                and stall_grace_seconds > 0
                and not any_node_started
                and elapsed >= stall_grace_seconds
            ):
                queue_hint = (
                    f" task queue '{stall_task_queue}'"
                    if stall_task_queue
                    else " the extract task queue"
                )
                raise NoWorkerOnTaskQueueError(
                    message=(
                        f"No DAG node started within {stall_grace_seconds}s for run "
                        f"{run_id} (top-level status={result.status.value}). This "
                        f"almost always means no worker is polling{queue_hint}. "
                        "Verify the test's agent_spec().agent_name resolves to the "
                        "queue the deployed worker polls "
                        "(atlan-{ATLAN_APPLICATION_NAME}-{ATLAN_DEPLOYMENT_NAME}); a "
                        "common cause is a second e2e test class using a different "
                        "agent_name than the single worker the CI job started."
                    ),
                )
            # Progress watchdog: a node started but the DAG hasn't changed
            # state for the whole window -> wedged. Fail fast with the last
            # node states instead of polling the full timeout (see docstring).
            if (
                progress_stall_seconds is not None
                and progress_stall_seconds > 0
                and any_node_started
                and (elapsed - last_progress_elapsed) >= progress_stall_seconds
            ):
                node_states = (
                    ", ".join(f"{n.name}={n.status.value}" for n in result.nodes)
                    or "(no nodes)"
                )
                raise DAGProgressStalledError(
                    message=(
                        f"No DAG node changed state for {progress_stall_seconds}s for "
                        f"run {run_id} (top-level status={result.status.value}; nodes: "
                        f"{node_states}). A node started but is not progressing — most "
                        "often wedged on a slow/failing step (e.g. extract stuck on an "
                        f"object-store upload). Failing fast instead of polling the full "
                        f"{timeout_seconds}s."
                    ),
                )
        # Timeout: return the last observation so callers can include
        # node-level state in the failure message rather than just
        # "timed out after Xs". Stamp the ceiling onto it: returning the
        # observation bare made every caller read the last-seen node states as
        # a verdict, so a node that was never dispatched surfaced as a node
        # failure with ``error=None``.
        if last_result is not None:
            return replace(
                last_result,
                timed_out_after_seconds=float(timeout_seconds),
                seconds_since_last_progress=max(
                    0.0, last_elapsed - last_progress_elapsed
                ),
            )
        raise AtlanApiTimeoutError(
            message=f"native-status timed out after {timeout_seconds}s with no response",
            timeout_seconds=float(timeout_seconds),
        )

    def connection_exists_in_atlas_via_search(self, qualified_name: str) -> bool:
        """Search-based Connection probe — works around the direct-fetch ACL.

        Hits the indexsearch endpoint with an exact ``qualifiedName`` +
        ``typeName=Connection`` filter. The search ACL is permissive
        (anyone with read on the connector namespace can see results)
        whereas the direct entity-fetch endpoint enforces the
        Connection's ``adminUsers``/``adminRoles``. Use this when the
        harness's identity isn't expected to be on the Connection's
        admin list — e.g. when adminRoles is just ``$admin`` and the
        OAuth-client service account isn't.

        Returns True iff at least one Connection asset matches the QN.
        Network errors / search failures return False (treated as
        "not yet visible").
        """
        return asyncio.run(self._connection_search_async(qualified_name))

    async def _connection_search_async(self, qualified_name: str) -> bool:
        # Lazy: pyatlan is a heavy import; testing-time-only.
        from pyatlan.model.assets import Asset  # noqa: PLC0415
        from pyatlan.model.fluent_search import FluentSearch  # noqa: PLC0415

        try:
            async with self._build_async_atlan_client() as client:
                request = (
                    FluentSearch()
                    .where(FluentSearch.active_assets())
                    .where(Asset.QUALIFIED_NAME.eq(qualified_name))
                    .where(Asset.TYPE_NAME.eq("Connection"))
                ).to_request()
                request.dsl.size = 0
                return int((await client.asset.search(request)).count) > 0
        except Exception:
            logger.error(
                "Connection search for %s failed (treating as not-yet-visible)",
                qualified_name,
                exc_info=True,
            )
            return False

    def _build_async_atlan_client(self) -> Any:
        """Construct an AsyncAtlanClient using OAuth if configured, else bearer.

        Centralised so every pyatlan call (search, role_cache) goes
        through the same auth path. OAuth-client identity is preferred
        when both are present because OAuth tokens are explicitly
        scoped, whereas the API-key bearer often resolves to a
        broad-permissioned service account whose name confuses RBAC
        diagnostics.
        """
        from pyatlan.client.aio.client import AsyncAtlanClient  # noqa: PLC0415

        if self._oauth_client_id and self._oauth_client_secret:
            return AsyncAtlanClient(
                base_url=self.tenant_url,
                oauth_client_id=self._oauth_client_id,
                oauth_client_secret=self._oauth_client_secret,
            )
        return AsyncAtlanClient(base_url=self.tenant_url, api_key=self._api_token)

    def poll_atlas_for_connection(
        self,
        qualified_name: str,
        *,
        interval_seconds: int = 30,
        timeout_seconds: int = 1500,
        max_forbidden_attempts: int = 5,
        max_not_found_attempts: int = 10,
        max_not_found_attempts_override: int | None = None,
    ) -> bool:
        """Poll Atlas until the Connection appears or timeout elapses.

        Uses :meth:`connection_exists_in_atlas_via_search` rather than
        the direct entity-fetch endpoint because the search index ACL
        is permissive — direct fetch enforces the Connection's
        ``adminUsers``/``adminRoles`` and would 403 for identities not
        explicitly on that list. Indirect side-effect: the
        ``max_forbidden_attempts`` knob is now mostly vestigial since
        search doesn't surface 403. Kept on the signature for back-
        compat.

        Wide default timeout (25 min) because publish runs after the AE
        DAG completes and can take a while to flush large connections.
        Callers with smaller datasets can tighten this.

        ``max_not_found_attempts`` caps consecutive empty-search
        responses (~100s at the default 10s interval, ~5 min at the
        30s default) so the harness fails fast on a publish that
        reports success but doesn't actually land the Connection.
        """
        if max_not_found_attempts_override is not None:
            max_not_found_attempts = max_not_found_attempts_override
        # `max_forbidden_attempts` kept on the signature for back-compat
        # but unused now that the search path doesn't surface 403.
        del max_forbidden_attempts
        for attempt in until_deadline(
            timeout_seconds,
            interval_seconds,
            label=f"Atlas Connection {qualified_name}",
            # The per-poll probe line below already reports progress every
            # iteration; a heartbeat would duplicate it.
            heartbeat_seconds=0,
        ):
            found = self.connection_exists_in_atlas_via_search(qualified_name)
            # conformance: ignore[L006] short, bounded poll (timeout_seconds) with modest iteration count, not a hot loop; the per-iteration probe result is the primary diagnostic signal when an E2E run fails to converge
            logger.info(
                "Atlas Connection probe [%ds] qn=%s exists=%s",
                int(attempt.elapsed),
                qualified_name,
                found,
            )
            if found:
                return True
            # A found probe returns immediately, so every attempt reached here is
            # an empty search and the attempt number *is* the consecutive streak.
            if attempt.number >= max_not_found_attempts:
                logger.error(
                    "Atlas Connection probe found nothing %d times in a row "
                    "(%ds elapsed) — stopping early. The Connection never "
                    "materialised in Atlas: publish likely reported success "
                    "but the entities did not reach the asset server. Check "
                    "publish metrics vs the storage bucket the worker wrote "
                    "to and the one publish reads from.",
                    attempt.number,
                    int(attempt.elapsed),
                )
                return False
        return False

    def count_assets_under_connection(
        self,
        connection_qualified_name: str,
        *,
        type_names: tuple[str, ...] = ("Database", "Schema", "Table", "View", "Column"),
    ) -> dict[str, int]:
        """Per-typeName counts of assets under a connection's QN prefix.

        Uses pyatlan's async client + ``asyncio.gather`` so all
        per-type searches share a single HTTPS connection pool and
        fire concurrently — sequentially this is ~2.7s wall-time for
        the default 5 types, concurrent should land under 700ms once
        the TLS handshake is paid (one-time per harness run).

        Counts ACTIVE assets only: the raw index-search API returns
        archived (``__state=DELETED``) assets too, which silently
        inflates counts after any re-crawl that archives — the
        evolution scenario's "dropped table must leave the active
        count at baseline" assertion is meaningless without this
        filter (seen on a prior connector e2e run).

        Returns ``{typeName: count}`` with zeros for types that
        produced no matches. Used by the harness to assert extract +
        publish actually landed assets in Atlas, not just the
        Connection envelope — a Connection with zero descendants is
        almost always a config bug (filter mismatch, transform error)
        that the basic ``connection_in_atlas`` check would pass.
        """
        if not type_names:
            return {}
        prefix = f"{connection_qualified_name}/"
        results = asyncio.run(self._search_counts_async(prefix, type_names))
        return dict(zip(type_names, results))

    def count_total_assets_under_connection(
        self, connection_qualified_name: str
    ) -> int:
        """Total descendant-asset count under the connection prefix, ALL types.

        Unlike :meth:`count_assets_under_connection` (which requires explicit
        ``type_names``), this counts every asset under the connection's QN
        prefix regardless of type. It is the signal the non-empty backstop needs
        to protect connectors that declare no per-type expectations — the ones
        most likely to silently regress to a zero-asset run. Returns 0 on search
        error (treated as "nothing landed").
        """
        prefix = f"{connection_qualified_name}/"
        return asyncio.run(self._count_total_async(prefix))

    async def _count_total_async(self, prefix: str) -> int:
        """Single ``count`` search under *prefix* with no type filter."""
        from pyatlan.model.assets import Asset  # noqa: PLC0415
        from pyatlan.model.fluent_search import FluentSearch  # noqa: PLC0415

        try:
            async with self._build_async_atlan_client() as client:
                request = (
                    FluentSearch()
                    .where(FluentSearch.active_assets())
                    .where(Asset.QUALIFIED_NAME.startswith(prefix))
                    .to_request()
                )
                request.dsl.size = 0  # cheap response: only .count matters
                return int((await client.asset.search(request)).count)
        except Exception:
            logger.error("Total-asset count under %s failed", prefix, exc_info=True)
            return 0

    def count_lineage_under_connection(
        self,
        connection_qualified_name: str,
        *,
        type_names: tuple[str, ...] = ("Database", "Schema", "Table", "View", "Column"),
    ) -> dict[str, int]:
        """Per-typeName count of entity assets with lineage attached.

        Matches the "Lineage coverage" card in the Atlan workflow-center
        UI — counts entity assets (Database/Schema/Table/View/Column)
        whose ``__hasLineage`` is true under the Connection prefix.
        That's "how many of my assets did QI + lineage-app actually wire
        up", not "how many Process/ColumnProcess edges exist". The two
        signals are correlated but the asset-coverage view is what the
        product surfaces to reviewers, so the PR comment renders it
        verbatim.

        Returns ``{typeName: count}`` including zeros so missing
        coverage at a level (e.g. no lineage on Schemas) is visible
        rather than hidden.
        """
        if not type_names:
            return {}
        prefix = f"{connection_qualified_name}/"
        results = asyncio.run(
            self._search_counts_async(prefix, type_names, has_lineage_only=True)
        )
        return dict(zip(type_names, results))

    async def _search_counts_async(
        self,
        prefix: str,
        type_names: tuple[str, ...],
        *,
        has_lineage_only: bool = False,
    ) -> list[int]:
        """Parallel per-type ``count`` searches via pyatlan AsyncAtlanClient.

        Single async client / connection pool shared across all
        gathered searches — much cheaper than firing one sync HTTPS
        call per type, and the standard pyatlan pattern for batched
        reads.

        When ``has_lineage_only`` is set, the per-type query also
        filters ``HAS_LINEAGE.eq(True)`` so the count matches the
        Atlan UI's "Lineage coverage" card.
        """
        from pyatlan.model.assets import Asset  # noqa: PLC0415
        from pyatlan.model.fluent_search import FluentSearch  # noqa: PLC0415

        async def _count_one(client: Any, type_name: str) -> int:
            try:
                builder = (
                    FluentSearch()
                    .where(FluentSearch.active_assets())
                    .where(Asset.QUALIFIED_NAME.startswith(prefix))
                    .where(Asset.TYPE_NAME.eq(type_name))
                )
                if has_lineage_only:
                    builder = builder.where(Asset.HAS_LINEAGE.eq(True))
                request = builder.to_request()
                request.dsl.size = 0  # cheap response: we only want .count
                return int((await client.asset.search(request)).count)
            except Exception:
                logger.error(
                    "FluentSearch for %s under %s failed",
                    type_name,
                    prefix,
                    exc_info=True,
                )
                return 0

        # Route through _build_async_atlan_client so the count searches
        # honour the OAuth client_credentials config when present
        # (a service account with realm-admin can be missing from an
        # asset ACL the OAuth client *is* on — the choice between the
        # two identities is the entire reason _build_async_atlan_client
        # exists). Using AsyncAtlanClient(api_key=...) here would always
        # fall back to the API-key identity and silently break asset /
        # lineage coverage counts for OAuth-only tenants.
        async with self._build_async_atlan_client() as client:
            return list(
                await asyncio.gather(*(_count_one(client, tn) for tn in type_names))
            )

    def sample_asset_qualified_names_under_connection(
        self,
        connection_qualified_name: str,
        *,
        type_names: tuple[str, ...],
        per_type: int = 3,
    ) -> dict[str, list[str]]:
        """Sample up to *per_type* qualifiedNames per type under the connection.

        Backs the location/hierarchy assertion: the harness checks the *shape*
        (nesting depth) of a few landed assets per type, not just their counts.
        Returns ``{typeName: [qualifiedName, ...]}`` with an empty list for
        types that produced no hits (or on search error).
        """
        if not type_names:
            return {}
        prefix = f"{connection_qualified_name}/"
        results = asyncio.run(self._sample_qns_async(prefix, type_names, per_type))
        return dict(zip(type_names, results))

    async def _sample_qns_async(
        self, prefix: str, type_names: tuple[str, ...], per_type: int
    ) -> list[list[str]]:
        """Parallel per-type searches returning a few qualifiedNames each.

        Mirrors :meth:`_search_counts_async` (same shared async client /
        connection pool, same OAuth-vs-API-key identity handling) but requests a
        small page and reads ``qualifiedName`` off the hits instead of ``.count``.
        """
        from pyatlan.model.assets import Asset  # noqa: PLC0415
        from pyatlan.model.fluent_search import FluentSearch  # noqa: PLC0415

        # connectionQualifiedName is the canonical "which connection owns this
        # asset" field the Atlan UI filters on, and is required to be populated
        # on every asset — so match on it directly (not just the QN path prefix)
        # to sample the assets exactly as the product surfaces them.
        connection_qn = prefix.rstrip("/")

        async def _sample_one(client: "AsyncAtlanClient", type_name: str) -> list[str]:
            try:
                request = (
                    FluentSearch()
                    .where(FluentSearch.active_assets())
                    .where(Asset.QUALIFIED_NAME.startswith(prefix))
                    .where(Asset.CONNECTION_QUALIFIED_NAME.eq(connection_qn))
                    .where(Asset.TYPE_NAME.eq(type_name))
                    .include_on_results(Asset.QUALIFIED_NAME)
                    .include_on_results(Asset.CONNECTION_QUALIFIED_NAME)
                ).to_request()
                request.dsl.size = per_type
                results = await client.asset.search(request)
                page = results.current_page() or []
                # Asset.qualified_name is str | None; the `if qn` narrows it to
                # str so the return stays list[str], and the len cap enforces
                # per_type without a trailing slice.
                qns: list[str] = []
                for asset in page:
                    qn = asset.qualified_name
                    if qn:
                        qns.append(qn)
                    if len(qns) >= per_type:
                        break
                return qns
            except Exception:
                # Fails OPEN: an empty result makes the location check skip this
                # type (a silent pass), unlike the count path where 0 can trip a
                # floor. Hence the location assertion must be validated against a
                # real tenant before adopters rely on it. Logged at exception
                # level so the fault is at least visible in CI output.
                logger.error(
                    "qualifiedName sample for %s under %s failed",
                    type_name,
                    prefix,
                    exc_info=True,
                )
                return []

        async with self._build_async_atlan_client() as client:
            return list(
                await asyncio.gather(*(_sample_one(client, tn) for tn in type_names))
            )


# ---------------------------------------------------------------------------
# Helpers — defensive parsing for forward-compat
# ---------------------------------------------------------------------------


def _safe_node_status(raw: Any) -> DAGNodeStatus:
    """Map unknown / future status strings to ``Pending`` rather than raising.

    The AE service can introduce new intermediate statuses ahead of SDK
    releases; treating unknowns as non-terminal keeps polling alive
    instead of crashing the test on an unexpected enum value.
    """
    if not isinstance(raw, str):
        return DAGNodeStatus.PENDING
    try:
        return DAGNodeStatus(raw)
    except ValueError:
        logger.warning(
            "Unknown DAGNodeStatus value %r; returning PENDING", raw, exc_info=True
        )
        return DAGNodeStatus.PENDING


def _safe_run_status(raw: Any) -> DAGRunStatus:
    """Same defensive mapping for the top-level run status."""
    if not isinstance(raw, str):
        return DAGRunStatus.PENDING
    try:
        return DAGRunStatus(raw)
    except ValueError:
        logger.warning(
            "Unknown DAGRunStatus value %r; returning PENDING", raw, exc_info=True
        )
        return DAGRunStatus.PENDING


def _safe_int(raw: Any) -> int | None:
    """Cast a JSON number to int, returning None on missing / non-numeric."""
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        logger.warning("Cannot cast %r to int; returning None", raw, exc_info=True)
        return None
