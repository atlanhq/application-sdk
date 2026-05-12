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

import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from enum import Enum
from typing import Any

import orjson

from application_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)


# Standard library urllib's default User-Agent (``Python-urllib/<ver>``)
# is blocked by Cloudflare on most Atlan tenants (Error 1010 — browser
# signature banned). Spoofing a real UA keeps the request flowing through.
_USER_AGENT = "atlan-sdk-full-dag-e2e/1.0 (+https://github.com/atlanhq/application-sdk)"

# Timeout budget for individual HTTP calls. Polls run inside outer
# while-loops so the overall budget is driven by ``poll_native_status``
# / ``poll_atlas_for_connection``; the per-request timeout just keeps
# any one call from hanging the whole loop.
_HTTP_TIMEOUT = 30


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
        api_token: Bearer token. Accepts either a long-lived API key or a
            short-lived OAuth ``client_credentials`` access token.
    """

    def __init__(self, tenant_url: str, api_token: str) -> None:
        self.tenant_url = tenant_url.rstrip("/")
        self._api_token = api_token

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
                    return resp.status, raw.decode(errors="replace")
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                return e.code, orjson.loads(raw)
            except orjson.JSONDecodeError:
                return e.code, raw.decode(errors="replace")

    # ------------------------------------------------------------------
    # Endpoints
    # ------------------------------------------------------------------

    def create_workflow(self, name: str, description: str = "") -> str:
        """POST ``/automation/api/v1/workflows`` — create or upsert a workflow.

        AE doesn't auto-create workflows on submit: a fresh slug → HTTP
        404 ("Workflow with slug 'X' not found. Create the workflow
        first."). So every full-DAG run begins by creating (or
        re-creating) the workflow under a stable name. The endpoint is
        idempotent on name — submitting the same name returns the
        existing workflow's slug.

        Returns:
            The workflow slug (used by subsequent version + submit
            calls).

        Raises:
            RuntimeError: On non-2xx or missing slug in response.
        """
        status, body = self._request(
            "POST",
            "/automation/api/v1/workflows",
            body={"name": name, "description": description},
        )
        if status >= 300 or not isinstance(body, dict):
            raise RuntimeError(
                f"create_workflow failed: HTTP {status}\nresponse={body!r}"
            )
        data = body.get("data") if isinstance(body.get("data"), dict) else body
        slug = data.get("slug") if isinstance(data, dict) else None
        if not slug:
            raise RuntimeError(f"create_workflow returned no slug\nresponse={body!r}")
        return str(slug)

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
                data = (
                    body.get("data") if isinstance(body.get("data"), dict) else body
                )
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
        raise RuntimeError(
            f"create_version failed: HTTP {status}\nresponse={body!r}"
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
        raise RuntimeError(
            f"publish_version failed after {retries} attempts: {last_body!r}"
        )

    def submit_workflow(self, payload: dict[str, Any]) -> str:
        """POST ``/api/service/package-workflows?submit=true``.

        Returns the run UUID from the submit response. The submit
        response shape is not officially documented; we look for
        ``run_id`` under either the top level or a nested ``data`` key.

        Raises:
            RuntimeError: On non-2xx HTTP status or missing ``run_id``.
        """
        status, body = self._request(
            "POST",
            "/api/service/package-workflows?submit=true",
            body=payload,
        )
        if status >= 300 or not isinstance(body, dict):
            raise RuntimeError(f"AE submit failed: HTTP {status}\nresponse={body!r}")
        data = body.get("data") if isinstance(body.get("data"), dict) else body
        run_id = data.get("run_id") if isinstance(data, dict) else None
        if not run_id:
            raise RuntimeError(f"AE submit returned no run_id\nresponse={body!r}")
        return str(run_id)

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
            raise RuntimeError(
                f"native-status failed: HTTP {status}\nresponse={body!r}"
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
    ) -> DAGRunResult:
        """Poll until the run reaches a terminal top-level status.

        Logs a one-line summary per poll only when the status string
        changes (i.e. progress moments), to avoid spamming logs during
        long-running publish / lineage stages.
        """
        elapsed = 0
        last_summary: str | None = None
        last_result: DAGRunResult | None = None
        while elapsed < timeout_seconds:
            result = self.get_native_status(run_id)
            last_result = result
            summary = "; ".join(f"{n.name}={n.status.value}" for n in result.nodes)
            if summary != last_summary:
                logger.info(
                    "AE run %s (slug=%s) status=%s | %s",
                    result.run_id,
                    result.workflow_slug,
                    result.status.value,
                    summary,
                )
                last_summary = summary
            if result.status.is_terminal:
                return result
            time.sleep(interval_seconds)
            elapsed += interval_seconds
        # Timeout: return the last observation so callers can include
        # node-level state in the failure message rather than just
        # "timed out after Xs".
        if last_result is not None:
            return last_result
        raise RuntimeError(
            f"native-status timed out after {timeout_seconds}s with no response"
        )

    def get_connection_in_atlas(self, qualified_name: str) -> int:
        """HEAD-style probe for a Connection asset. Returns HTTP status code.

        200 means the Connection exists and is queryable in Atlas. 404
        means publish hasn't flushed it yet (or the workflow failed).
        Any other status is a real error worth surfacing.
        """
        status, _ = self._request(
            "GET",
            "/api/meta/entity/uniqueAttribute/type/Connection"
            f"?attr:qualifiedName={qualified_name}",
        )
        return status

    def poll_atlas_for_connection(
        self,
        qualified_name: str,
        *,
        interval_seconds: int = 30,
        timeout_seconds: int = 1500,
    ) -> bool:
        """Poll Atlas until the Connection appears or timeout elapses.

        Wide default budget (25 min) because publish runs after the AE
        DAG completes and can take a while to flush large connections.
        Callers with smaller datasets can tighten this.
        """
        elapsed = 0
        while elapsed < timeout_seconds:
            status = self.get_connection_in_atlas(qualified_name)
            logger.info(
                "Atlas Connection probe [%ds] qn=%s HTTP %d",
                elapsed,
                qualified_name,
                status,
            )
            if status == 200:
                return True
            if status >= 500:
                logger.warning(
                    "Atlas returned %d for Connection probe; retrying", status
                )
            time.sleep(interval_seconds)
            elapsed += interval_seconds
        return False


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
        return DAGNodeStatus.PENDING


def _safe_run_status(raw: Any) -> DAGRunStatus:
    """Same defensive mapping for the top-level run status."""
    if not isinstance(raw, str):
        return DAGRunStatus.PENDING
    try:
        return DAGRunStatus(raw)
    except ValueError:
        return DAGRunStatus.PENDING


def _safe_int(raw: Any) -> int | None:
    """Cast a JSON number to int, returning None on missing / non-numeric."""
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None
