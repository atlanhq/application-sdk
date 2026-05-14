"""Last-sync details primitive.

Stamps the three asset attributes that identify *which* run last touched an
asset (``lastSyncRun``, ``lastSyncWorkflowName``, ``lastSyncRunAt``).  Before
this primitive, every connector hand-rolled these values, usually from
``input.workflow_id`` and ``ctx.workflow_run_id`` — both of which resolve to
the *current* (often child) workflow's Temporal id, not the AE-dispatched
workflow that owns the end-to-end run.  The result on assets looked like::

    "lastSyncWorkflowName": "<uuid>-extract",   # child wf id, not clickable to AE
    "lastSyncRun":          "<temporal-run-uuid>"  # internal id, not propagated

The primitive resolves the values from the SDK's execution + correlation
context, which the Temporal interceptor already populates:

* ``lastSyncWorkflowName`` ← the **AE-dispatched workflow's Temporal id**,
  picked up via ``parent_workflow_id`` when a connector workflow is running
  inside an AE-spawned child (``workflow.info().parent``); otherwise the
  top-level ``workflow_id``.  This is a run-unique UUID (e.g.
  ``4b9eade4-de53-4b69-9010-2446e0a8f85c``) — clickable from an asset back
  to that exact AE run's history in Temporal UI for debugging.
* ``lastSyncRun`` ← the correlation id that ties the full end-to-end run
  (extract + publish) together; propagated through Temporal headers + memo
  by the correlation interceptor.
* ``lastSyncRunAt`` ← the current UTC epoch in milliseconds.

The primitive resolves these automatically so callers don't need to know
about workflow id de-referencing.  Explicit overrides are accepted for
non-Temporal callers (tests, batch tools).

Trade-offs to be aware of:

* ``lastSyncRun`` defaults to the SDK correlation id (end-to-end span across
  extract + publish) rather than the Temporal ``run_id`` that appears in the
  Atlan UI's runs-page URL.  Callers that want UI-clickthrough semantics
  should pass ``run=<temporal_run_id>`` explicitly.
* ``workflow_name`` returns the **AE Temporal workflow_id** — a run-unique
  UUID — *not* the Atlan UI's workflow slug (the ``<connector>-<short>``
  string in URLs like ``…/workflows/profile/dbt-AMBSvQPJ/…``).  The UI slug
  isn't plumbed through to the SDK today.  Connection identity is already
  carried on assets via ``connectionName`` / ``connectionQualifiedName``
  (separate fields, not ``lastSyncWorkflowName``).  Callers who want the UI
  slug or a friendly connection name in this field can pass
  ``workflow_name=<value>`` explicitly.
* The resolver walks **one level** of ``info.parent``.  This covers
  connectors that spawn a single layer of child workflows below the AE
  parent (the common shape today).  Deeper nesting would need the AE id
  propagated via Temporal memo (mirroring how the correlation interceptor
  handles ``correlation_id`` in
  ``execution/_temporal/interceptors/log.py``).

See BLDX-1229 for the design discussion.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from application_sdk.observability import get_correlation_context, get_execution_context

LAST_SYNC_RUN: str = "lastSyncRun"
LAST_SYNC_WORKFLOW_NAME: str = "lastSyncWorkflowName"
LAST_SYNC_RUN_AT: str = "lastSyncRunAt"


@dataclass(frozen=True)
class LastSyncDetails:
    """Resolved last-sync values for a single end-to-end run.

    Attributes:
        run: Correlation id tying the full run together (extract + publish).
            Empty string when no correlation context is set.
        workflow_name: AE-assigned workflow id slug (e.g. ``dbt-AMBSvQPJ``).
            Resolves to the topmost workflow id — ``parent_workflow_id``
            when running inside a child workflow, ``workflow_id`` when
            running at the top.  Empty string outside Temporal.
        run_at_ms: Epoch milliseconds at the moment the values were
            resolved.  Always set.
    """

    run: str
    workflow_name: str
    run_at_ms: int


def resolve_last_sync_details(
    *,
    run: str | None = None,
    workflow_name: str | None = None,
    run_at_ms: int | None = None,
) -> LastSyncDetails:
    """Resolve last-sync values from the current execution + correlation
    contexts, with explicit overrides taking precedence.

    Args:
        run: Override for ``lastSyncRun``.  When ``None`` (default), the
            correlation id from the current correlation context is used.
        workflow_name: Override for ``lastSyncWorkflowName``.  When ``None``
            (default), the topmost workflow id from the current execution
            context is used (``parent_workflow_id or workflow_id``).
        run_at_ms: Override for ``lastSyncRunAt``.  When ``None`` (default),
            the current UTC epoch in milliseconds is used.
    """
    if run is None:
        corr = get_correlation_context()
        run = corr.correlation_id if corr else ""

    if workflow_name is None:
        ctx = get_execution_context()
        workflow_name = ctx.parent_workflow_id or ctx.workflow_id

    if run_at_ms is None:
        run_at_ms = int(datetime.now(UTC).timestamp() * 1000)

    return LastSyncDetails(run=run, workflow_name=workflow_name, run_at_ms=run_at_ms)


def set_last_sync_details(
    asset: dict[str, Any],
    *,
    details: LastSyncDetails | None = None,
    run: str | None = None,
    workflow_name: str | None = None,
    run_at_ms: int | None = None,
) -> dict[str, Any]:
    """Stamp ``lastSyncRun`` / ``lastSyncWorkflowName`` / ``lastSyncRunAt``
    onto an asset dict (mutating in place).

    Asset shape follows the Atlas wire format: keys live under
    ``asset["attributes"]`` (camelCase).  Missing ``attributes`` is created.

    Args:
        asset: Asset dict to stamp.  Mutated in place; also returned for
            chaining.
        details: Pre-resolved values.  When provided, ``run`` /
            ``workflow_name`` / ``run_at_ms`` are ignored.  Use this with
            :func:`set_last_sync_details_bulk` to apply the same values to a
            batch consistently.
        run: One-off override for ``lastSyncRun``.  Ignored when ``details``
            is supplied.  ``None`` (default) auto-resolves from correlation
            context.
        workflow_name: One-off override for ``lastSyncWorkflowName``.
            Ignored when ``details`` is supplied.  ``None`` (default)
            auto-resolves from execution context.
        run_at_ms: One-off override for ``lastSyncRunAt``.  Ignored when
            ``details`` is supplied.  ``None`` (default) uses now.
    """
    d = details or resolve_last_sync_details(
        run=run, workflow_name=workflow_name, run_at_ms=run_at_ms
    )

    attrs = asset.setdefault("attributes", {})
    if d.run:
        attrs[LAST_SYNC_RUN] = d.run
    if d.workflow_name:
        attrs[LAST_SYNC_WORKFLOW_NAME] = d.workflow_name
    attrs[LAST_SYNC_RUN_AT] = d.run_at_ms
    return asset


def set_last_sync_details_bulk(
    assets: Iterable[dict[str, Any]],
    *,
    run: str | None = None,
    workflow_name: str | None = None,
    run_at_ms: int | None = None,
) -> list[dict[str, Any]]:
    """Stamp last-sync details on every asset in ``assets``.

    Resolves the values **once** so every asset in the batch carries the
    same ``lastSyncRunAt`` (and the same resolved run / workflow_name when
    auto-resolved).  Each asset is mutated in place and returned in a list
    for caller convenience.
    """
    details = resolve_last_sync_details(
        run=run, workflow_name=workflow_name, run_at_ms=run_at_ms
    )
    return [set_last_sync_details(a, details=details) for a in assets]
