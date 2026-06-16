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
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from enum import Enum
from typing import Any

import orjson

from application_sdk.errors.base import AppError
from application_sdk.observability.logger_adaptor import get_logger
from application_sdk.testing.e2e._errors import (
    AtlanApiHttpError,
    AtlanApiResponseInvariantError,
    AtlanApiTimeoutError,
)

logger = get_logger(__name__)


# Standard library urllib's default User-Agent (``Python-urllib/<ver>``)
# is blocked by Cloudflare on most Atlan tenants (Error 1010 — browser
# signature banned). Spoofing a real UA keeps the request flowing through.
_USER_AGENT = "atlan-sdk-full-dag-e2e/1.0 (+https://github.com/atlanhq/application-sdk)"

# Timeout budget for individual HTTP calls. Polls run inside outer
# while-loops so the overall budget is driven by ``poll_native_status``
# / ``poll_atlas_for_connection``; the per-request timeout just keeps
# any one call from hanging the whole loop.
_HTTP_TIMEOUT = 60
# AE submit blocks while the tenant connector pod accepts the workflow —
# KEDA may spin the pod up from zero, which adds ~60-90 s of startup latency
# on top of the normal request time.
_SUBMIT_TIMEOUT = 120

# Cadence for "still polling" heartbeat log lines in
# ``poll_native_status`` — lineage stages take 2-5 min on small
# datasets and the status string doesn't change during that time, so
# the loop would otherwise look wedged in CI output. Long enough that
# we don't drown the log on a fast happy-path run.
_HEARTBEAT_SECONDS = 30

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
}
_RUN_GLYPHS = {
    "Succeeded": "✅",
    "Failed": "❌",
    "Running": "🔄",
    "Pending": "🟡",
    "Cancelled": "🚫",
    "TimedOut": "⏰",
}


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


class DAGNodeStatus(str, Enum):
    """Status values returned by ``native-status`` per DAG node."""

    PENDING = "Pending"
    SCHEDULED = "Scheduled"
    RUNNING = "Running"
    SUCCEEDED = "Succeeded"
    FAILED = "Failed"
    ERROR = "Error"
    CANCELLED = "Cancelled"

    @property
    def is_terminal(self) -> bool:
        """True if this status will not change without re-submission."""
        return self in {
            DAGNodeStatus.SUCCEEDED,
            DAGNodeStatus.FAILED,
            DAGNodeStatus.ERROR,
            DAGNodeStatus.CANCELLED,
        }

    @property
    def is_success(self) -> bool:
        """True when the node completed without error."""
        return self is DAGNodeStatus.SUCCEEDED


class DAGRunStatus(str, Enum):
    """Top-level status of an AE workflow run."""

    PENDING = "Pending"
    RUNNING = "Running"
    SUCCEEDED = "Succeeded"
    FAILED = "Failed"
    ERROR = "Error"
    CANCELLED = "Cancelled"

    @property
    def is_terminal(self) -> bool:
        return self in {
            DAGRunStatus.SUCCEEDED,
            DAGRunStatus.FAILED,
            DAGRunStatus.ERROR,
            DAGRunStatus.CANCELLED,
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

    @property
    def all_nodes_succeeded(self) -> bool:
        return bool(self.nodes) and all(n.status.is_success for n in self.nodes)

    @property
    def failed_nodes(self) -> list[DAGNodeResult]:
        return [n for n in self.nodes if not n.status.is_success]


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
    ) -> tuple[int, dict[str, Any] | str]:
        """HTTP request returning ``(status_code, parsed_body_or_text)``."""
        url = f"{self.tenant_url}{path}"
        data = orjson.dumps(body) if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Authorization", f"Bearer {self._api_token}")
        req.add_header("Accept", "application/json")
        req.add_header("User-Agent", _USER_AGENT)
        if body is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
                try:
                    return resp.status, orjson.loads(raw)
                except orjson.JSONDecodeError:
                    logger.warning(
                        "Response body is not JSON; returning raw text", exc_info=True
                    )
                    return resp.status, raw.decode(errors="replace")
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                return e.code, orjson.loads(raw)
            except orjson.JSONDecodeError:
                logger.warning(
                    "HTTP error body is not JSON; returning raw text", exc_info=True
                )
                return e.code, raw.decode(errors="replace")

    # ------------------------------------------------------------------
    # Endpoints
    # ------------------------------------------------------------------

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

        Retries on HTTP 5xx (same ``AE-COMMON-500-01: An unexpected
        error occurred`` shape we already handle on ``create_version``
        / ``submit_workflow`` / ``poll_native_status``). 4 attempts at
        5s intervals covers the typical AE recovery window without
        sitting on a hard failure.

        Returns:
            The workflow slug (used by subsequent version + submit
            calls).

        Raises:
            RuntimeError: On non-2xx after retries are exhausted or on
                missing slug in the response body.
        """
        last: tuple[int, Any] = (0, {})
        for attempt in range(1, retries + 2):
            status, body = self._request(
                "POST",
                "/automation/api/v1/workflows",
                body={"name": name, "description": description},
            )
            last = (status, body)
            if status < 300 and isinstance(body, dict):
                data = body.get("data") if isinstance(body.get("data"), dict) else body
                slug = data.get("slug") if isinstance(data, dict) else None
                if not slug:
                    raise AtlanApiResponseInvariantError(
                        message=f"create_workflow returned no slug\nresponse={body!r}",
                        expectation="slug present in create_workflow response",
                    )
                if attempt > 1:
                    logger.info("create_workflow succeeded on attempt %d", attempt)
                return str(slug)
            if status >= 500 and attempt <= retries:
                logger.warning(
                    "create_workflow attempt %d/%d: HTTP %d (retrying in %ds) body=%r",
                    attempt,
                    retries + 1,
                    status,
                    retry_sleep_seconds,
                    body,
                )
                time.sleep(retry_sleep_seconds)
                continue
            break
        status, body = last
        raise AtlanApiHttpError(
            message=f"create_workflow failed: HTTP {status}\nresponse={body!r}",
            target=f"POST /automation/api/v1/workflows HTTP {status}",
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

        Retries on HTTP 404 because AE has a brief indexing window
        between ``create_workflow`` returning a slug and that slug
        being queryable on this endpoint — early calls hit AE-WF-404-02
        ("Workflow with slug 'X' not found. Create the workflow
        first.") even though we just created it. mssql platform-smoke
        bridges the gap with a ``sleep 3`` after create_workflow; we
        retry on 404 directly so the harness self-recovers regardless
        of how slow indexing is on a given tenant.

        Returns:
            The version number assigned by AE (typically a Unix
            timestamp, but treat as opaque int).
        """
        last: tuple[int, dict[str, Any] | str] = (0, "")
        for attempt in range(1, retries + 1):
            status, body = self._request(
                "POST",
                f"/automation/api/v1/workflows/{slug}/versions",
                body=version_payload,
            )
            last = (status, body)
            if status < 300 and isinstance(body, dict):
                data = body.get("data") if isinstance(body.get("data"), dict) else body
                version = data.get("version") if isinstance(data, dict) else None
                if version is not None:
                    return int(version)
            # 404 is the indexing-lag case — retry. Other failures are
            # not retryable (auth, validation, etc.) so bail immediately
            # with the body for diagnostic.
            if status != 404:
                break
            logger.warning(
                "create_version attempt %d/%d: HTTP 404 (slug %s indexing); retrying in %ds",
                attempt,
                retries,
                slug,
                retry_sleep_seconds,
            )
            if attempt < retries:
                time.sleep(retry_sleep_seconds)
        status, body = last
        raise AtlanApiHttpError(
            message=f"create_version failed: HTTP {status}\nresponse={body!r}",
            target=f"POST /automation/api/v1/workflows/.../versions HTTP {status}",
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
        found"). Retries on failure (mssql platform-smoke does the
        same: 5 attempts, 5s spacing).

        Raises:
            RuntimeError: If all retries return a non-success status.
        """
        last_body: dict[str, Any] | str = ""
        for attempt in range(1, retries + 1):
            status, body = self._request(
                "POST",
                f"/automation/api/v1/workflows/{slug}/versions/{version}/publish",
            )
            last_body = body
            if (
                status < 300
                and isinstance(body, dict)
                and body.get("status") == "success"
            ):
                logger.info(
                    "Published workflow %s version %d (attempt %d)",
                    slug,
                    version,
                    attempt,
                )
                return
            logger.warning(
                "publish_version attempt %d/%d: HTTP %s body=%r",
                attempt,
                retries,
                status,
                body,
            )
            if attempt < retries:
                time.sleep(retry_sleep_seconds)
        raise AtlanApiHttpError(
            message=f"publish_version failed after {retries} attempts: {last_body!r}",
            target="POST /automation/api/v1/workflows/.../versions/.../publish",
        )

    def submit_workflow(
        self,
        payload: dict[str, Any],
        *,
        retries: int = 4,
        retry_sleep_seconds: int = 5,
    ) -> str:
        """POST ``/api/service/package-workflows?submit=true``.

        Returns the run UUID from the submit response. The submit
        response shape is not officially documented; we look for
        ``run_id`` under either the top level or a nested ``data`` key.

        Retries on HTTP 5xx — AE's submit can race with the
        publish_version indexing window and surface a generic
        ``AE-COMMON-500-01: An unexpected error occurred`` even after
        publish_version returned 200. 4 retries at 5s intervals
        covers the longest indexing lag we've observed (~15s) without
        sitting on a hard failure.

        Raises:
            RuntimeError: On non-2xx HTTP status after retries are
                exhausted, or on missing ``run_id`` in the response.
        """
        last: tuple[int, Any] = (0, {})
        for attempt in range(1, retries + 2):
            status, body = self._request(
                "POST",
                "/api/service/package-workflows?submit=true",
                body=payload,
                timeout=_SUBMIT_TIMEOUT,
            )
            last = (status, body)
            if status < 300 and isinstance(body, dict):
                data = body.get("data") if isinstance(body.get("data"), dict) else body
                run_id = data.get("run_id") if isinstance(data, dict) else None
                if run_id:
                    if attempt > 1:
                        logger.info("AE submit succeeded on attempt %d", attempt)
                    return run_id
                raise AtlanApiResponseInvariantError(
                    message=f"AE submit returned no run_id\nresponse={body!r}",
                    expectation="run_id present in submit response",
                )
            if status >= 500 and attempt <= retries:
                logger.warning(
                    "AE submit attempt %d/%d: HTTP %d (retrying in %ds) body=%r",
                    attempt,
                    retries + 1,
                    status,
                    retry_sleep_seconds,
                    body,
                )
                time.sleep(retry_sleep_seconds)
                continue
            break
        status, body = last
        raise AtlanApiHttpError(
            message=f"AE submit failed: HTTP {status}\nresponse={body!r}",
            target=f"POST /api/service/package-workflows?submit=true HTTP {status}",
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
        """
        elapsed = 0
        last_summary: str | None = None
        last_result: DAGRunResult | None = None
        transient_streak = 0
        last_log_elapsed = 0  # seconds since the last info log fired
        while elapsed < timeout_seconds:
            try:
                result = self.get_native_status(run_id)
            except AppError as e:
                transient_streak += 1
                if transient_streak >= max_transient_failures:
                    logger.error(
                        "native-status failed %d times in a row — giving up: %s",
                        transient_streak,
                        e,
                        exc_info=True,
                    )
                    raise
                logger.warning(
                    "native-status transient error (streak %d/%d): %s — sleeping %ds and retrying",
                    transient_streak,
                    max_transient_failures,
                    e,
                    interval_seconds,
                    exc_info=True,
                )
                time.sleep(interval_seconds)
                elapsed += interval_seconds
                continue
            transient_streak = 0
            last_result = result
            summary = " ".join(_node_glyph(n) for n in result.nodes)
            run_glyph = _RUN_GLYPHS.get(result.status.value, "•")
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
                logger.info(
                    "%s AE run [%3ds] %s — %s",
                    run_glyph,
                    elapsed,
                    result.status.value,
                    summary,
                )
                last_summary = summary
                last_log_elapsed = elapsed
            if result.status.is_terminal:
                return result
            time.sleep(interval_seconds)
            elapsed += interval_seconds
        # Timeout: return the last observation so callers can include
        # node-level state in the failure message rather than just
        # "timed out after Xs".
        if last_result is not None:
            return last_result
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
                    .where(Asset.QUALIFIED_NAME.eq(qualified_name))
                    .where(Asset.TYPE_NAME.eq("Connection"))
                ).to_request()
                request.dsl.size = 0
                return int((await client.asset.search(request)).count) > 0
        except Exception:
            logger.exception(
                "Connection search for %s failed (treating as not-yet-visible)",
                qualified_name,
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
        elapsed = 0
        not_found_streak = 0
        # `max_forbidden_attempts` kept on the signature for back-compat
        # but unused now that the search path doesn't surface 403.
        del max_forbidden_attempts
        while elapsed < timeout_seconds:
            found = self.connection_exists_in_atlas_via_search(qualified_name)
            logger.info(
                "Atlas Connection probe [%ds] qn=%s exists=%s",
                elapsed,
                qualified_name,
                found,
            )
            if found:
                return True
            not_found_streak += 1
            if not_found_streak >= max_not_found_attempts:
                logger.error(
                    "Atlas Connection probe found nothing %d times in a row "
                    "(%ds elapsed) — stopping early. The Connection never "
                    "materialised in Atlas: publish likely reported success "
                    "but the entities did not reach the asset server. Check "
                    "publish metrics vs the storage bucket the worker wrote "
                    "to and the one publish reads from.",
                    not_found_streak,
                    elapsed,
                )
                return False
            time.sleep(interval_seconds)
            elapsed += interval_seconds
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
                    .where(Asset.QUALIFIED_NAME.startswith(prefix))
                    .to_request()
                )
                request.dsl.size = 0  # cheap response: only .count matters
                return int((await client.asset.search(request)).count)
        except Exception:
            logger.exception("Total-asset count under %s failed", prefix)
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
                    .where(Asset.QUALIFIED_NAME.startswith(prefix))
                    .where(Asset.TYPE_NAME.eq(type_name))
                )
                if has_lineage_only:
                    builder = builder.where(Asset.HAS_LINEAGE.eq(True))
                request = builder.to_request()
                request.dsl.size = 0  # cheap response: we only want .count
                return int((await client.asset.search(request)).count)
            except Exception:
                logger.exception(
                    "FluentSearch for %s under %s failed", type_name, prefix
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
