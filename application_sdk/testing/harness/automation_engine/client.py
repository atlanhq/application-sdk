"""The async Automation Engine reader and writer.

The AE half of ``testing/e2e/client.py``, lifted here and converted to
``async`` (child F on FND-224). Three endpoint families:

* ``POST /automation/api/v1/workflows`` and its ``/versions`` children — create
  the workflow and publish the seed DAG.
* ``POST /api/service/package-workflows?submit=true`` — the submit. The one
  non-idempotent write in the harness, and the reason
  :mod:`application_sdk.testing.harness.automation_engine.retry` exists.
* ``GET /api/service/package-workflows/native-status/<run_id>`` — the DAG run's
  per-node breakdown, and the poll over it.

**Why async.** Decision D1: everything below the pytest boundary is ``async``,
and the one bridge back to blocking code is
:func:`~application_sdk.testing.harness.bridge.run_sync`. Concretely for this
half: ``httpx.AsyncClient`` in place of the single-use ``httpx.Client``, so the
happy path reuses one pooled connection across a whole run instead of paying a
TLS handshake per call; :func:`~application_sdk.testing.harness._poll.until_deadline_async`
in place of its sync twin; and no ``time.sleep`` anywhere. ``AEWorkflowClient``
in ``testing/e2e/client.py`` keeps its synchronous public surface as one-line
``run_sync`` shims until child H moves that shim up to ``BaseE2ETest``.

**The pool is dropped on a transport error, not reused.** The single-use client
this replaced had one property worth keeping: a connection that is already
half-dead is exactly the condition a transport retry exists to escape, and
reusing it would retry into the same dead socket. So the pool is closed and
rebuilt before a re-attempt — pooling on the happy path, a fresh connection on
the retry.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import quote

import httpx
import orjson

from application_sdk.errors.base import AppError
from application_sdk.observability.logger_adaptor import get_logger
from application_sdk.testing.harness._poll import (
    _HEARTBEAT_SECONDS,
    sleep_async,
    until_deadline_async,
)
from application_sdk.testing.harness.automation_engine._errors import (
    AppNotReadyError,
    AtlanAEWorkflowAlreadyActiveError,
    AtlanApiHttpError,
    AtlanApiResponseInvariantError,
    AtlanApiTimeoutError,
    AutomationEngineNotDispatchingError,
    DAGProgressStalledError,
    NoWorkerOnTaskQueueError,
    RequestDelivery,
)
from application_sdk.testing.harness.automation_engine.retry import (
    RETRY_AFTER_BUDGET_SECONDS,
    RunLookup,
    WriteRecovery,
    classify_delivery,
    is_already_active_run,
    is_app_not_ready,
    is_credential_name_conflict,
    newest_run_since,
    requested_retry_after,
    retry_gap,
    rotate_submit_credential_name,
    unsubstituted_parameter_tokens,
)
from application_sdk.testing.harness.automation_engine.wire import (
    RUN_GLYPHS,
    DAGNodeResult,
    DAGRunResult,
    DAGRunStatus,
    PublishedVersion,
    first_version_row,
    safe_int,
    safe_node_status,
    safe_run_status,
)

logger = get_logger(__name__)

__all__ = ["AEClient"]


# A default client User-Agent (``Python-urllib/<ver>``, ``python-httpx/<ver>``)
# is blocked by Cloudflare on most Atlan tenants (Error 1010 — browser
# signature banned). Spoofing a real UA keeps the request flowing through.
_USER_AGENT = "atlan-sdk-full-dag-e2e/1.0 (+https://github.com/atlanhq/application-sdk)"

# Timeout budget for individual HTTP calls. Polls run inside outer
# while-loops so the overall budget is driven by ``poll_native_status``;
# the per-request timeout just keeps any one call from hanging the whole loop.
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

# ``_HEARTBEAT_SECONDS`` (imported from ``_poll``) is the cadence for "still
# polling" heartbeat log lines in ``poll_native_status`` — lineage stages take
# 2-5 min on small datasets and the status string doesn't change during that
# time, so the loop would otherwise look wedged in CI output. That loop throttles
# its own richer progress line to the same cadence and disables the generic
# heartbeat, rather than emitting two "still waiting" lines.


class AEClient:
    """Async client for the Automation Engine endpoints a full-DAG run uses.

    Stateless aside from the auth token and one pooled HTTP connection. Every
    method except :meth:`submit_workflow` is idempotent and safe to retry.

    Args:
        tenant_url: Base URL of the tenant (e.g. ``https://devex.atlan.com``).
            Trailing slash is stripped if present.
        api_token: Bearer token used for AE REST calls. Accepts either a
            long-lived API key or a short-lived OAuth ``client_credentials``
            access token.

    Note:
        The pooled ``httpx.AsyncClient`` is bound to the event loop that first
        used it. Call :meth:`aclose` from the same loop when the run is over;
        not calling it leaks one pool until the process exits, which is
        survivable in a test process and is why it is not enforced.
    """

    def __init__(self, tenant_url: str, api_token: str) -> None:
        self.tenant_url = tenant_url.rstrip("/")
        self._api_token = api_token
        self._http: httpx.AsyncClient | None = None

    # ------------------------------------------------------------------
    # Low-level HTTP
    # ------------------------------------------------------------------

    def _transport(self) -> httpx.AsyncClient:
        """This client's pooled HTTP connection, created on first use.

        ``follow_redirects`` preserves the redirect handling ``urlopen`` gave
        the original implementation and ``httpx`` disables by default. The
        per-request timeout is passed per call rather than baked in here,
        because the submit gets a wider one than every other call.
        """
        if self._http is None or self._http.is_closed:
            self._http = httpx.AsyncClient(follow_redirects=True)
        return self._http

    async def _drop_transport(self) -> None:
        """Close the pool so the next attempt opens a fresh connection.

        A transport error means the connection in hand is, or may be, already
        dead — and reusing it is exactly the condition the retry exists to
        escape. The single-use client this replaced got that for free; keeping
        it explicit is what makes pooling on the happy path safe.
        """
        http, self._http = self._http, None
        if http is not None and not http.is_closed:
            await http.aclose()

    async def aclose(self) -> None:
        """Close the pooled connection. Idempotent; a later call reopens one."""
        await self._drop_transport()

    async def _request(
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
        :class:`~application_sdk.testing.harness.automation_engine._errors.RequestDelivery`
        telling that loop whether the origin can have seen the request — which
        is what makes a connect-phase failure safely retryable there.
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
                resp = await self._transport().request(
                    method,
                    url,
                    content=content,
                    headers=headers,
                    timeout=httpx.Timeout(timeout),
                )
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
                delivery = classify_delivery(e)
                # Whatever connection we were holding is suspect; the next
                # attempt (here or in a caller's loop) opens a fresh one.
                await self._drop_transport()
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
                    await sleep_async(_REQUEST_BACKOFF_SECONDS)
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

    async def _post_with_retry(
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
        recover_ambiguous: Callable[[], Awaitable[WriteRecovery]] | None = None,
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
                that attempt, bounded by
                :data:`~application_sdk.testing.harness.automation_engine.retry.MAX_RETRY_AFTER_SECONDS`
                and the per-call
                :data:`~application_sdk.testing.harness.automation_engine.retry.RETRY_AFTER_BUDGET_SECONDS`
                (see
                :func:`~application_sdk.testing.harness.automation_engine.retry.retry_gap`).
                A timeout has no body to read, so it always uses the fixed gap.
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
                :attr:`~application_sdk.testing.harness.automation_engine._errors.RequestDelivery.NOT_DELIVERED`
                is re-POSTed regardless: the connection was never established,
                so the origin provably never saw the request and no duplicate
                is possible. Those re-POSTs are counted against the same
                ``total_attempts`` budget as every other retry, keeping the
                single-retry-loop invariant that ``cold_start_submit_kwargs``
                depends on.
            recover_ambiguous: Optional read of the origin's own state, awaited
                when a network failure leaves it unknown whether the write took
                effect. Return the response body the origin *would* have sent
                if the write is found to have landed — it is returned to the
                caller as a 200 and no retry happens. Return ``None`` if the
                write provably left no trace, which reclassifies the failure as
                :attr:`~application_sdk.testing.harness.automation_engine._errors.RequestDelivery.NOT_APPLIED`
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
                status, resp_body = await self._request(
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
                    recovered = await recover_ambiguous()
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
                    await sleep_async(sleep_seconds)
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
                gap = retry_gap(
                    requested_retry_after(resp_body),
                    default_seconds=sleep_seconds,
                    budget_left=RETRY_AFTER_BUDGET_SECONDS - honoured_seconds,
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
                await sleep_async(gap.seconds)
                continue
            break
        return last

    async def create_workflow(
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
        :func:`~application_sdk.testing.harness.automation_engine.retry.retry_gap`)
        instead of expiring the whole budget inside it.

        Returns:
            The workflow slug (used by subsequent version + submit calls).
        """
        status, body = await self._post_with_retry(
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
            retry_after_seconds=requested_retry_after(body),
        )

    async def create_version(
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
        status, body = await self._post_with_retry(
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
            retry_after_seconds=requested_retry_after(body),
        )

    async def publish_version(
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
        status, body = await self._post_with_retry(
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
            retry_after_seconds=requested_retry_after(body),
        )

    async def get_published_version(self, slug: str) -> PublishedVersion | None:
        """GET the published version of *slug* — the DAG that actually runs.

        ``GET /automation/api/v1/workflows/<slug>/versions?is_published=true
        &page=0&page_size=1`` — the same read Heracles itself uses
        (``GetLatestPublishedVersion``). Read-only, and on a route family the
        harness already authenticates against for create / publish.

        There is deliberately no preflight equivalent of this: the originally
        planned ``?submit=false`` does not exist (``processCreateWorkflow``
        routes native execution to ``processAutomationEngineWorkflow`` without
        forwarding query params, and that function ends in an unconditional
        submit), and there is no ``GET /package-workflows/{name}``. So callers
        read this *after* submit.

        Never raises. Returns ``None`` when the read did not get through — a
        transport failure, a non-2xx, or an envelope
        :func:`~application_sdk.testing.harness.automation_engine.wire.first_version_row`
        could not parse. ``None`` means "no answer", which is not the same as
        "no match", and callers must not treat it as one.

        Returns:
            The published version and its DAG, or ``None`` if unreadable.
        """
        path = (
            f"/automation/api/v1/workflows/{quote(slug, safe='')}/versions"
            "?is_published=true&page=0&page_size=1"
        )
        try:
            status, body = await self._request("GET", path)
        except AppError:
            logger.warning(
                "published-version read for slug %s did not get through; which "
                "DAG AE published stays unknown",
                slug,
                exc_info=True,
            )
            return None
        if status >= 300:
            logger.warning(
                "published-version read for slug %s returned HTTP %d; which DAG "
                "AE published stays unknown\nresponse=%r",
                slug,
                status,
                body,
            )
            return None
        row = first_version_row(body)
        if row is None:
            logger.warning(
                "published-version read for slug %s returned no parseable "
                "version record; which DAG AE published stays unknown\n"
                "response=%r",
                slug,
                body,
            )
            return None
        dag = row.get("dag")
        return PublishedVersion(
            version=safe_int(row.get("version")),
            dag=dag if isinstance(dag, dict) else {},
        )

    async def find_run_created_since(
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
            A :class:`~application_sdk.testing.harness.automation_engine.retry.RunLookup`.
            ``run_id`` is set when a matching run was found. ``conclusive``
            distinguishes the two ways of finding none: AE answered and had no
            such run (proof of absence, safe to act on) versus AE never answered
            at all (proves nothing).
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
        async for _attempt in until_deadline_async(
            timeout_seconds,
            interval_seconds,
            label=f"an AE run under slug '{slug}' created since {floor.isoformat()}",
        ):
            try:
                status, body = await self._request("GET", path)
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
            run_id = newest_run_since(body, floor)
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

    async def probe_run_is_listed(self, slug: str, run_id: str) -> bool | None:
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
            status, body = await self._request("GET", path)
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

    async def submit_workflow(
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
          :meth:`_post_with_retry` re-POSTs it within the normal attempt budget.
          See
          :func:`~application_sdk.testing.harness.automation_engine.retry.classify_delivery`;
          this is what keeps a blackholed CI hairpin from reding a whole e2e leg
          on the first packet loss.
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
          as a 500 — see
          :func:`~application_sdk.testing.harness.automation_engine.retry.is_already_active_run`)
          is treated as terminal, not a retryable 5xx, and surfaced as
          :class:`~application_sdk.testing.harness.automation_engine._errors.AtlanAEWorkflowAlreadyActiveError`.

        Genuine 5xx that are *not* the already-active conflict remain retryable.

        A tenant app pod that is still cold-starting also answers as a generic
        retryable 5xx (see
        :func:`~application_sdk.testing.harness.automation_engine.retry.is_app_not_ready`),
        so the retry above already covers it — but the default 4x5s budget
        expires long before a pod boots. Callers that submit against a
        freshly-installed tenant app (both harnesses' ``run_full_dag``)
        therefore pass a cold-start-sized ``retries`` / ``retry_sleep_seconds``,
        built by
        :func:`~application_sdk.testing.harness.automation_engine.retry.cold_start_submit_kwargs`;
        on exhaustion, a last response that still reads as connection-refused is
        surfaced as
        :class:`~application_sdk.testing.harness.automation_engine._errors.AppNotReadyError`
        rather than an opaque
        :class:`~application_sdk.testing.harness.automation_engine._errors.AtlanApiHttpError`,
        so the failure names the cause. FND-402.

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

        async def _recover_from_ae() -> WriteRecovery:
            """Ask AE whether the submit whose response we lost took effect."""
            lookup = await self.find_run_created_since(slug, submitted_at)
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

        status, body = await self._post_with_retry(
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
                (s >= 500 and not is_already_active_run(s, b))
                or is_credential_name_conflict(s, b)
            ),
            op_name="submit_workflow",
            mutate_before_retry=rotate_submit_credential_name,
            retry_network_errors=False,
            # Resolvable only when we know which slug to look under; without
            # one, an ambiguous timeout stays terminal as it always was.
            recover_ambiguous=_recover_from_ae if slug else None,
        )
        if is_already_active_run(status, body):
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
                self._warn_on_unsubstituted_parameters(body, run_id)
                return run_id
            raise AtlanApiResponseInvariantError(
                message=f"AE submit returned no run_id\nresponse={body!r}",
                expectation="run_id present in submit response",
            )
        if is_app_not_ready(status, body):
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
            retry_after_seconds=requested_retry_after(body),
        )

    def _warn_on_unsubstituted_parameters(
        self, body: dict[str, Any], run_id: str
    ) -> None:
        """Log the parameters AE accepted but left as literal mustache tokens.

        A 2xx submit with a run_id is currently the harness's only success
        signal, so an AE that creates the run without resolving ``payload[]``
        into the request's Argo parameters produces a run that cannot work and
        a harness that does not know it. The extract activity then dies on the
        literal (``[AAF-CRD-005] Invalid credential GUID — must match
        [a-zA-Z0-9_-]+: '{{credentialGuid}}'``) one poll interval later, which
        reads as a connector bug rather than a control-plane one.

        Warn rather than raise, deliberately. AE's submit response shape is
        undocumented (see this method's caller) and may legitimately echo the
        request unmodified, which would make a hard assertion fail every
        connector's green run. A warning is safe on every tenant and is enough
        to attribute the fault; hardening this into an invariant is a follow-up
        for once the logged shape is known. FND-402 / FND-656.
        """
        # Unconditional, because the shape is the unknown. AE's submit response
        # is undocumented; a detector that only reports when it fires can never
        # tell us WHY it did not (verified against a real tenant: the response
        # carries no Argo parameter block at all, so the scan below is inert
        # there). Top-level keys only — never values, which can carry a source
        # credential. This is what makes the next run diagnostic instead of
        # silent.
        logger.info(
            "submit_workflow: AE accepted run %s; response keys=%s data keys=%s",
            run_id,
            sorted(body.keys()),
            sorted(body["data"].keys()) if isinstance(body.get("data"), dict) else None,
        )
        leftover = unsubstituted_parameter_tokens(body)
        if not leftover:
            return
        logger.warning(
            "submit_workflow: AE accepted run %s but left %d parameter(s) as "
            "unresolved mustache literals: %s. If 'credential-guid' is among "
            "them the run WILL fail on the extract node with [AAF-CRD-005] "
            "Invalid credential GUID — AE did not turn the submit's payload[] "
            "credential block into a GUID. That is a tenant/AE control-plane "
            "fault, not a connector defect.",
            run_id,
            len(leftover),
            ", ".join(
                f"{name} -> {{{{{token}}}}}" for name, token in sorted(leftover.items())
            ),
        )

    async def get_native_status(self, run_id: str) -> DAGRunResult:
        """GET ``/api/service/package-workflows/native-status/<run_id>``.

        Parses the response into a typed
        :class:`~application_sdk.testing.harness.automation_engine.wire.DAGRunResult`
        so callers don't have to memorize the wire shape.
        """
        status, body = await self._request(
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
                retry_after_seconds=requested_retry_after(body),
            )
        nodes_raw = body.get("dag_nodes") or {}
        nodes = [
            DAGNodeResult(
                name=name,
                status=safe_node_status(n.get("status")),
                started_at_ms=safe_int(n.get("started_at")),
                completed_at_ms=safe_int(n.get("completed_at")),
                error_message=n.get("error_message"),
            )
            for name, n in sorted(nodes_raw.items())
        ]
        return DAGRunResult(
            run_id=str(body.get("run_id", run_id)),
            workflow_slug=str(body.get("workflow_slug", "")),
            status=safe_run_status(body.get("status")),
            nodes=nodes,
        )

    async def poll_native_status(
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

        Tolerates transient HTTP failures from :meth:`get_native_status`:
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
        at
        :data:`~application_sdk.testing.harness.automation_engine.retry.RETRY_AFTER_BUDGET_SECONDS`
        (same accounting as :meth:`_post_with_retry`) and each sleep is clamped
        to the remaining ``timeout_seconds`` budget, so the loop never sleeps
        past its own deadline.

        Fail-fast stall guard: when ``stall_grace_seconds`` is a positive int
        (``None`` or any value ``<= 0`` disables it) and NO DAG node has left the
        not-started set (``Pending`` / ``Scheduled``) within that window, raise
        :class:`~application_sdk.testing.harness.automation_engine._errors.NoWorkerOnTaskQueueError`
        instead of hanging for the full ``timeout_seconds``. The parent AE
        workflow runs on the always-on automation-engine queue, so the top-level
        run flips to ``Running`` even when the connector's ``extract`` node is
        stuck because no worker polls its task queue — hence the check is on
        node-level start, not the run status. ``stall_task_queue`` is included in
        the error message so the operator can see which queue had no worker.

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
        :attr:`~application_sdk.testing.harness.automation_engine.wire.DAGRunResult.timed_out`
        before treating the node states as a verdict.

        Note:
            Still a hand-rolled loop rather than a call to
            :func:`~application_sdk.testing.harness.waiting.poll_until`, whose
            start-grace latch and no-change watchdog were extracted from exactly
            this function. Consolidating the two is child D (FND-240) and is
            deliberately not folded into this move: the AE-specific leaves this
            loop raises carry connector remediation advice
            (:class:`~application_sdk.testing.harness.automation_engine._errors.NoWorkerOnTaskQueueError`
            names the task queue to check) that
            :func:`~application_sdk.testing.harness.outcome.assert_settled`'s
            generic leaves do not, and re-deriving that mapping is a behaviour
            change to review on its own rather than inside a 2,500-line move.
            ``test_waiting_equivalence.py`` already pins the two loops as
            equivalent on the parts that do transfer.
        """
        last_summary: str | None = None
        last_result: DAGRunResult | None = None
        transient_streak = 0
        # Seconds spent waiting *beyond* the poll interval because the origin
        # asked for longer — same accounting ``_post_with_retry`` keeps, so
        # both retry loops bound honoured backoff at RETRY_AFTER_BUDGET_SECONDS.
        honoured_seconds = 0.0
        last_log_elapsed = 0.0  # seconds since the last info log fired
        last_elapsed = 0.0  # elapsed at the last successful observation
        any_node_started = False  # any node reached Running/terminal (stall guard)
        last_progress_elapsed = (
            0.0  # elapsed at the last node-state transition (progress watchdog)
        )
        async for attempt in until_deadline_async(
            timeout_seconds,
            interval_seconds,
            label=f"AE run {run_id}",
            # This loop emits its own richer progress line (per node-state change
            # plus a heartbeat at the same cadence); the generic one would double it.
            heartbeat_seconds=0,
        ):
            elapsed = attempt.elapsed
            try:
                result = await self.get_native_status(run_id)
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
                gap = retry_gap(
                    e.retry_after_seconds if isinstance(e, AtlanApiHttpError) else None,
                    default_seconds=interval_seconds,
                    budget_left=RETRY_AFTER_BUDGET_SECONDS - honoured_seconds,
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
            summary = result.fingerprint
            run_glyph = RUN_GLYPHS.get(result.status.value, "•")
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
            # Fail fast when nothing has started within the grace window. Which
            # system is at fault depends on the top-level status: a run still
            # Pending was never dispatched by AE, so the app's queue cannot be
            # the cause; a live parent means AE dispatched and the connector's
            # own node is the one nothing picked up.
            if (
                # only a positive grace arms the guard; None or any value <= 0
                # disables it (a negative would otherwise fire on the first poll)
                stall_grace_seconds is not None
                and stall_grace_seconds > 0
                and not any_node_started
                and elapsed >= stall_grace_seconds
            ):
                if result.status is DAGRunStatus.PENDING:
                    raise AutomationEngineNotDispatchingError(
                        message=(
                            f"AE run {run_id} was still Pending after "
                            f"{stall_grace_seconds}s, so it was never dispatched and "
                            "no DAG node could start. Nothing was offered to the "
                            "app's task queue, so the app's worker and agent name "
                            "are not implicated — check the tenant's automation "
                            "engine (contention on a shared e2e tenant, or an AE "
                            "worker not processing new runs)."
                        ),
                    )
                queue_hint = (
                    f" task queue '{stall_task_queue}'"
                    if stall_task_queue
                    else " the extract task queue"
                )
                raise NoWorkerOnTaskQueueError(
                    message=(
                        f"No DAG node started within {stall_grace_seconds}s for run "
                        f"{run_id} (top-level status={result.status.value}), so AE "
                        f"dispatched but nothing picked the node up. This almost "
                        f"always means no worker is polling{queue_hint}. "
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
                # Stamp the stall onto the observation and attach it, rather
                # than rendering a ``name=status`` list here: naming the task
                # queue and the child workflow needs the seed DAG's routing,
                # which only the harness holds. One renderer, two entry points.
                raise DAGProgressStalledError(
                    message=(
                        f"No DAG node changed state for {progress_stall_seconds}s for "
                        f"run {run_id} (top-level status={result.status.value}). A node "
                        "started but is not progressing — most often wedged on a "
                        "slow/failing step (e.g. extract stuck on an object-store "
                        "upload). Failing fast instead of polling the full "
                        f"{timeout_seconds}s. Last-seen node states are attached as "
                        "``result``."
                    ),
                    result=replace(
                        result,
                        progress_stalled_after_seconds=float(progress_stall_seconds),
                        seconds_since_last_progress=max(
                            0.0, elapsed - last_progress_elapsed
                        ),
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
