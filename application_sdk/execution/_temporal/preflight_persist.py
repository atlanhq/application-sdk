"""Persisting a gate verdict to the tenant's preflight-results store (CONNECT-1142).

The injected preflight gate holds the highest-volume check in the fleet — a real
source, on the cadence the customer actually runs it — and today decides whether to
proceed and throws the result away. This module appends it through the route the
system-workflows app serves. That app holds the only writer principal for the
tenant's Iceberg namespace, so an app posts a row rather than carrying lakehouse
credentials of its own.

Three rules, in order of importance, and they are the same three the heracles write
follows for the same reasons:

 1. No workflow slug means no write and no error. A run the Automation Engine did
    not dispatch has nothing to attribute a row to, and never will.
 2. A write never fails the gate. It is scheduled and abandoned, and every failure
    is logged and dropped: the store is allowed to have holes, a customer's run is
    not allowed to break because of one.
 3. The verdict is relayed, not processed. The app that owns the table derives its
    own columns from the payload, so anything decided here would be a second,
    weaker copy of that.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import httpx
from pydantic import BaseModel, ValidationError

from application_sdk.contracts.base import SerializableEnum
from application_sdk.contracts.types import ConnectionRef
from application_sdk.errors.base import redact_wire_value
from application_sdk.handler.contracts import PreflightOutput
from application_sdk.observability.logger_adaptor import get_logger

if TYPE_CHECKING:
    from application_sdk.execution._temporal.preflight_gate import PreflightGateInput

logger = get_logger(__name__)


class PreflightResultOrigin(SerializableEnum):
    """Which interface produced a stored check.

    A closed vocabulary the store validates against. Only :attr:`ACTIVITY` is
    written from here; the other interfaces write their own rows.
    """

    ACTIVITY = "activity"


class ExtractionMethod(SerializableEnum):
    """How the checked source was reached.

    A closed vocabulary the store validates against. It records agent-or-not
    rather than the connector's own method, so a value such as ``s3`` or
    ``offline`` is :attr:`DIRECT`.
    """

    AGENT = "agent"
    DIRECT = "direct"


#: Keys an agent check may arrive under. Only presence is ever read — the value
#: holds credential material.
_AGENT_HINT_KEYS = ("agent_json", "agent-json", "agent_name", "agent-name")

#: The block the store reads its derived columns out of, and the one key every
#: writer shares. ``/sage`` wraps this same block in a display envelope
#: (``success`` / ``message`` / ``data``) that re-keys the checks for the setup
#: widget; none of it is read here, and synthesising it would be a second
#: implementation of that envelope for a reader that does not want it.
_PREFLIGHT_KEY = "preflight"

#: Handler-authored free text that must not cross the app boundary. The store
#: derives its columns from the typed routing fields (``status``, ``code``,
#: ``category``, ``audience``, ``retryable``); these three are the residual
#: that :func:`redact_wire_value` cannot close, because it is pattern-based.
_RELAY_FREE_TEXT_KEYS = frozenset({"message", "suggested_action", "evidence"})


class PreflightCheckResult(BaseModel):
    """The write route's request body.

    Every field is something only the caller knows. The server stamps the rest and
    will not accept it here: the tenant (a pod serves exactly one, so taking it
    from us would add a spoofing surface and no information) and the load timestamp
    (our clock skew would corrupt the table's only ordering).

    Attributes:
        workflow_slug: AE's slug for the checked workflow. Never empty — a row
            without one is not built at all.
        origin: Which interface produced the check. Always
            :attr:`PreflightResultOrigin.ACTIVITY` from here.
        payload: The verdict, inside its ``preflight`` envelope. Handler-authored
            free text (``message``, ``suggested_action``, ``evidence``) is dropped
            before send; remaining strings are secret-redacted. Kept as
            ``dict[str, Any]``: this is a relay boundary, and the store derives
            its own columns from it, so typing it here would bind the SDK to
            that schema.
        extraction_method: Whether the source was reached through a customer-hosted
            agent.
        connection_qualified_name: The checked workflow's connection asset. Absent
            when the workflow has none, which is legal.
        app_id: Global Marketplace's id for this app — the marketplace card's
            ``installation_id``, which is what the frontend path records, so both
            writers land the same id space. Absent on a deployment whose chart
            does not stamp ``ATLAN_APP_ID`` yet; the column is nullable and takes
            absent over a substitute. ``ATLAN_RELEASE_ID`` is deliberately not
            that substitute — it identifies the release, not the app, so it would
            change under the same app every time it ships.
        app_version: This app's release, as its catalog card carries it.
    """

    workflow_slug: str
    origin: PreflightResultOrigin = PreflightResultOrigin.ACTIVITY
    payload: dict[str, Any]
    extraction_method: ExtractionMethod = ExtractionMethod.DIRECT
    connection_qualified_name: str | None = None
    app_id: str | None = None
    app_version: str | None = None


def _or_none(value: str | None) -> str | None:
    """*value* trimmed, or ``None`` when it is empty — so "unknown" has one shape."""
    return (value.strip() or None) if value else None


def _drop_handler_authored_text(value: Any) -> Any:
    """Strip :data:`_RELAY_FREE_TEXT_KEYS` from a dumped verdict, recursively.

    This is the allowlist the relay uses: typed routing fields stay, free text
    does not. Pattern-based redaction cannot close a DSN, a ``host:port`` or an
    opaque internal id that a connector put in a check message, and this write
    is the path that would otherwise ship that text to another app.
    """
    if isinstance(value, dict):
        return {
            key: _drop_handler_authored_text(item)
            for key, item in value.items()
            if key not in _RELAY_FREE_TEXT_KEYS
        }
    if isinstance(value, list):
        return [_drop_handler_authored_text(item) for item in value]
    return value


def verdict_payload(result: PreflightOutput) -> dict[str, Any]:
    """*result* as the ``preflight`` block the store reads, secret-redacted.

    Checks go through :meth:`PreflightCheck.to_wire` so a failed check's typed
    error is the one on the wire, matching the frontend path's precedence.
    Handler-authored free text (``message``, ``suggested_action``, ``evidence``)
    is then dropped: the store derives its columns from the typed routing
    fields, and those three are not redacted where they are built. Remaining
    strings are walked by :func:`~application_sdk.errors.base.redact_wire_value`
    as belt-and-braces for anything else that still matches a secret pattern.

    Args:
        result: The verdict the handler reached.

    Returns:
        ``{"preflight": ...}``, ready to send.
    """
    block = result.model_dump(mode="json", exclude_none=True)
    block["checks"] = [check.to_wire() for check in result.checks]
    return {_PREFLIGHT_KEY: redact_wire_value(_drop_handler_authored_text(block))}


def extraction_method(input: PreflightGateInput) -> ExtractionMethod:
    """Whether *input*'s source was reached through a customer-hosted agent.

    Two signals, either sufficient, matching what the frontend path records: the
    declared method, and the presence of an agent spec — an agent check can carry
    the spec without setting the method. ``"s3"`` and ``"offline"`` are neither, and
    correctly answer :attr:`ExtractionMethod.DIRECT`.

    Args:
        input: The gate's routing envelope.

    Returns:
        The method this check reached its source by.
    """
    if input.extraction_method.strip().lower() == ExtractionMethod.AGENT:
        return ExtractionMethod.AGENT
    if input.agent_json is not None:
        return ExtractionMethod.AGENT
    snapshot = input.extraction_snapshot
    if any(snapshot.get(key) for key in _AGENT_HINT_KEYS):
        return ExtractionMethod.AGENT
    return ExtractionMethod.DIRECT


def connection_qualified_name(snapshot: dict[str, Any]) -> str | None:
    """The checked workflow's connection, from either shape AE sends.

    The connection asset is read through :class:`ConnectionRef`, which models
    that wire shape and resolves the camelCase/snake_case duality itself. A
    shape it rejects falls through to the bare qualified name — itself either a
    string or a single-element list, which has no typed model.

    A workflow naming no connection is legal, not an error.

    Args:
        snapshot: The raw extraction-input dump the gate carries.

    Returns:
        The qualified name, or ``None`` when the workflow names no connection.
    """
    if connection := snapshot.get("connection"):
        try:
            ref = ConnectionRef.model_validate(connection)
        except ValidationError:
            # Not the asset shape; the bare-name fallback below still applies.
            logger.debug("connection did not fit ConnectionRef; trying the bare name")
        else:
            if name := _or_none(ref.attributes.qualified_name):
                return name

    for key in ("connection_qualified_name", "connection-qualified-name"):
        raw = snapshot.get(key)
        if isinstance(raw, str):
            if name := _or_none(raw):
                return name
        elif isinstance(raw, list) and raw and isinstance(raw[0], str):
            if name := _or_none(raw[0]):
                return name
    return None


def build_check_result(
    input: PreflightGateInput,
    result: PreflightOutput,
    *,
    app_id: str = "",
    app_version: str = "",
) -> PreflightCheckResult | None:
    """The store row for one gate verdict, or ``None`` when there is nothing to
    attribute it to.

    Pure: no clock, no environment, no I/O, so the whole extraction is testable
    without a network or a running loop.

    Args:
        input: The gate's routing envelope, carrying the slug and the snapshot.
        result: The verdict the handler reached.
        app_id: Global Marketplace's id for this app, if the platform stamped one.
        app_version: This app's release, if the platform stamped one.

    Returns:
        The row, or ``None`` when *input* names no workflow — rule 1, a documented
        skip and not a failure.
    """
    slug = _or_none(input.workflow_slug)
    if slug is None:
        return None

    return PreflightCheckResult(
        workflow_slug=slug,
        origin=PreflightResultOrigin.ACTIVITY,
        payload=verdict_payload(result),
        extraction_method=extraction_method(input),
        connection_qualified_name=connection_qualified_name(input.extraction_snapshot),
        app_id=_or_none(app_id),
        app_version=_or_none(app_version),
    )


async def post_check_result(
    row: PreflightCheckResult, *, endpoint: str, timeout: float
) -> None:
    """POST one row to the store, and ignore what comes back.

    The only I/O in this module, so a test substitutes exactly this one function.
    Nothing is returned and no response is recorded: by rule 2 there is no outcome
    a caller could act on, and the response body on a 4xx is FastAPI's validation
    error, which echoes the offending input back — and the offending input is the
    verdict, which is what must not reach a log line.

    No ``Authorization`` header: the route is unauthenticated and would ignore one,
    and forwarding this run's token to a service that does not check it would widen
    that token's reach for nothing.

    Args:
        row: What to write.
        endpoint: The store's write route, whole. Not composed here — see
            :data:`~application_sdk.constants.PREFLIGHT_RESULTS_ENDPOINT`.
        timeout: Seconds to allow the whole request.

    Raises:
        Exception: Whatever httpx raises. :func:`persist_check_result` is what
            guarantees a failure never reaches the gate; this function stays honest
            so a test can assert on the failure.
    """
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.post(
            endpoint,
            content=row.model_dump_json(exclude_none=True),
            headers={"Content-Type": "application/json"},
        )
    if response.is_success:
        logger.debug(
            "preflight result persisted workflow_slug=%s origin=%s",
            row.workflow_slug,
            row.origin,
        )
        return
    # Status only. See the docstring: the body carries the verdict back.
    logger.warning(
        "preflight result rejected by the store; continuing workflow_slug=%s origin=%s status=%s",
        row.workflow_slug,
        row.origin,
        response.status_code,
    )


def persist_check_result(
    input: PreflightGateInput,
    result: PreflightOutput,
    *,
    app_id: str = "",
    app_version: str = "",
    endpoint: str,
    timeout: float,
) -> asyncio.Task[None] | None:
    """Schedule the write and return immediately. Never raises, never blocks.

    Detached on purpose: a customer's run is waiting on the gate, so the gate waits
    on nothing here. The task is handed to the worker's event loop and outlives the
    activity that started it; a task still in flight when the worker shuts down is
    dropped, which is the hole rule 2 accepts.

    Outside a running loop — a direct call, a unit test — there is nowhere to
    schedule it, and that is logged and skipped rather than raised.

    Args:
        input: The gate's routing envelope.
        result: The verdict to persist.
        app_id: Global Marketplace's id for this app, if one is stamped.
        app_version: This app's release, if one is stamped.
        endpoint: The store's write route, whole.
        timeout: Seconds to allow the POST.

    Returns:
        The scheduled task, for callers — in practice, tests — that want to wait
        on it, or ``None`` when nothing was scheduled. Ignoring it is the normal
        case, and the same idiom ``_runtime.offload.submit_in_thread`` uses.
    """
    try:
        row = build_check_result(input, result, app_id=app_id, app_version=app_version)
    except Exception:
        logger.warning(
            "preflight result not persisted: the row could not be built", exc_info=True
        )
        return None
    if row is None:
        logger.debug(
            "preflight result not persisted: this run carries no workflow slug; entrypoint=%s",
            input.entrypoint,
        )
        return None

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        logger.debug(
            "preflight result not persisted: no running event loop to schedule the "
            "write; workflow_slug=%s",
            row.workflow_slug,
        )
        return None

    task = loop.create_task(post_check_result(row, endpoint=endpoint, timeout=timeout))
    # Nothing awaits the task, so an exception it raises would surface as asyncio's
    # "never retrieved" log on GC. Consume it here instead, at WARNING and with the
    # slug, so a store that is down reads as one line per run and not a traceback.
    task.add_done_callback(lambda done: _log_write_failure(done, row.workflow_slug))
    return task


def _log_write_failure(task: asyncio.Task[None], workflow_slug: str) -> None:
    """Consume an abandoned write's exception so it is logged, not GC-reported."""
    if task.cancelled():
        return
    if (error := task.exception()) is not None:
        logger.warning(
            "preflight result not persisted; continuing workflow_slug=%s error=%s",
            workflow_slug,
            error,
        )
