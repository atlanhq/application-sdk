"""The Dapr pub/sub and event-triggered workflow routes.

``GET /dapr/subscribe`` tells the Dapr sidecar which topics to deliver here and
where: each :class:`~application_sdk.handler.contracts.SubscriptionConfig` to its
``/subscriptions/v1/<route>`` handler, and each
:class:`~application_sdk.handler.contracts.EventTriggerConfig` to
``POST /events/v1/event/{event_id}``, which starts the app's default entry point
with the event's data. An event no trigger matches goes to
``POST /events/v1/drop``.

The worker's handler service registers them through :func:`register_event_routes`;
an :class:`EventRoutesSource` carries the app, its triggers and subscriptions,
and how to reach Temporal, so the routes can be built and tested on their own.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast
from uuid import uuid4

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from application_sdk._logging import get_logger
from application_sdk.app.entrypoint import _resolve_default_entrypoint
from application_sdk.common.dispatch import canonical_workflow_type
from application_sdk.constants import EVENT_STORE_NAME
from application_sdk.handler.contracts import EventTriggerConfig, SubscriptionConfig
from application_sdk.handler.workflow_routes import (
    _execution_timeout,
    _require_configured,
)
from application_sdk.observability.correlation import (
    CorrelationContext,
    set_correlation_context,
)

if TYPE_CHECKING:
    from temporalio.client import Client

    from application_sdk.app.base import App
    from application_sdk.handler.service import WorkflowClientConfig

__all__ = [
    "EventRoutesSource",
    "register_event_routes",
]

# The worker's handler log name, kept from before the routes left
# handler/service.py so log queries and alerts keep matching.
logger = get_logger("application_sdk.handler.service")


@dataclass(frozen=True)
class EventRoutesSource:
    """What the event routes need, for one app.

    Args:
        app_name: The app's name, for event-started workflow IDs.
        workflow_config: Returns the workflow configuration, read once per
            event. The worker reads its module-level configuration here, the
            same one its shared Temporal client is created from, so an event
            is never dispatched with one service's queue over another's client.
        temporal_client: Returns the Temporal client, called once per event.
        event_triggers: The events that start a workflow.
        subscriptions: The pub/sub topics delivered to the app's own handlers.
    """

    app_name: str
    workflow_config: Callable[[], WorkflowClientConfig]
    temporal_client: Callable[[], Awaitable[Client]]
    event_triggers: Sequence[EventTriggerConfig] = ()
    subscriptions: Sequence[SubscriptionConfig] = ()


def _subscription_entry(sub: SubscriptionConfig) -> dict[str, Any]:
    """The ``/dapr/subscribe`` entry routing *sub*'s topic to its handler."""
    entry: dict[str, Any] = {
        "pubsubname": sub.component_name,
        "topic": sub.topic,
        "route": f"/subscriptions/v1/{sub.route}",
    }
    if sub.bulk_enabled:
        entry["bulkSubscribe"] = {
            "enabled": sub.bulk_enabled,
            "maxMessagesCount": sub.bulk_max_messages,
            "maxAwaitDurationMs": sub.bulk_max_await_ms,
        }
    if sub.dead_letter_topic:
        entry["deadLetterTopic"] = sub.dead_letter_topic
    return entry


def _trigger_entry(trigger: EventTriggerConfig) -> dict[str, Any]:
    """The ``/dapr/subscribe`` entry routing *trigger*'s matching events to it."""
    filters = [f"({f.path} {f.operator} '{f.value}')" for f in trigger.event_filters]
    filters.append(f"event.data.event_name == '{trigger.event_name}'")
    filters.append(f"event.data.event_type == '{trigger.event_type}'")
    return {
        "pubsubname": EVENT_STORE_NAME,
        "topic": trigger.event_type,
        "routes": {
            "rules": [
                {
                    "match": " && ".join(filters),
                    "path": f"/events/v1/event/{trigger.event_id}",
                }
            ],
            "default": "/events/v1/drop",
        },
    }


async def _read_event_body(request: Request) -> object:
    """The event's JSON body; 400 when it is not JSON (413 passes unchanged)."""
    try:
        return await request.json()
    except HTTPException:
        # The body-size cap's 413: already the answer, nothing to log.
        raise
    except Exception:
        # Log details server-side; the HTTPException intentionally hides the
        # parse internals from the client (avoid leaking file/line/offset).
        # conformance: ignore[L009] the log is the only server-side record; the raised HTTPException deliberately withholds the parse internals.
        logger.warning("Failed to parse JSON request body", exc_info=True)
        raise HTTPException(status_code=400, detail="Invalid JSON body") from None


async def _start_event_workflow(
    source: EventRoutesSource,
    config: WorkflowClientConfig,
    app_cls: type[App],
    event_id: str,
    body: object,
) -> JSONResponse:
    """Start the app's default entry point with the event's data.

    Raises on any failure; the route answers ``RETRY`` so Dapr redelivers.
    """
    client = await source.temporal_client()

    ep = _resolve_default_entrypoint(
        getattr(getattr(app_cls, "_app_metadata", None), "entry_points", {})
    )
    input_type = (
        ep.input_type if ep is not None else getattr(app_cls, "_input_type", None)
    )
    if input_type is None:
        raise HTTPException(
            status_code=500,
            detail=f"App class {app_cls.__name__} does not define _input_type.",
        )

    if not isinstance(body, dict):
        # Answered RETRY like any other failed start.
        raise TypeError(f"event body is a {type(body).__name__}, not a JSON object")
    envelope = cast("dict[str, Any]", body)
    event_data = envelope.get("data", envelope)
    input_data = (
        input_type(**event_data) if isinstance(event_data, dict) else input_type()
    )

    workflow_id = f"{source.app_name}-event-{event_id}-{uuid4().hex[:8]}"
    # Inject workflow_id so run() can access it via input.workflow_id
    input_data.workflow_id = workflow_id

    # Correlation memo (CNCT-104): event-triggered starts previously passed no
    # memo, so the LogInterceptor fell back to a value the run page never
    # queries and the run's logs were unfindable. Respect an event-supplied
    # correlation_id; otherwise mint one — the same contract as
    # /workflows/v1/start — and return it so the run's logs are addressable.
    correlation_id = getattr(input_data, "correlation_id", "") or str(uuid4())
    input_data.correlation_id = correlation_id
    # The legacy private mirror of correlation_id; the memo below is what the
    # log interceptor reads. Not a declared field, hence setattr.
    setattr(input_data, "_correlation_id", correlation_id)

    # Bind the ContextVar so every handler-side line for this request,
    # including the route's log of a failed dispatch, carries the
    # correlation_id returned to the caller.
    set_correlation_context(CorrelationContext(correlation_id=correlation_id))

    handle = await client.start_workflow(
        canonical_workflow_type(app_cls._app_name, ep)
        if ep is not None
        else config.app_name,
        args=[input_data],
        id=workflow_id,
        task_queue=config.task_queue,
        memo=CorrelationContext(correlation_id=correlation_id).to_temporal_memo(),
        execution_timeout=_execution_timeout(config),
    )
    return JSONResponse(
        content={
            "status": "SUCCESS",
            "workflow_id": handle.id,
            "run_id": handle.result_run_id,
            "correlation_id": correlation_id,
        }
    )


async def _handle_event(
    source: EventRoutesSource, event_id: str, request: Request
) -> JSONResponse:
    """``POST /events/v1/event/{event_id}``: start the trigger's workflow."""
    trigger = next((t for t in source.event_triggers if t.event_id == event_id), None)
    if trigger is None:
        raise HTTPException(
            status_code=404,
            detail=f"No trigger registered for event_id: {event_id}",
        )
    config = source.workflow_config()
    app_cls = config.app_class
    if app_cls is None:
        raise HTTPException(status_code=503, detail="App class not provided.")

    body = await _read_event_body(request)
    try:
        return await _start_event_workflow(source, config, app_cls, event_id, body)
    except Exception as e:
        logger.error(
            "Failed to start event-triggered workflow for event %s: %s",
            event_id,
            e,
            exc_info=True,
        )
        return JSONResponse(content={"status": "RETRY"}, status_code=500)


def register_event_routes(app: FastAPI, source: EventRoutesSource) -> None:
    """Register the Dapr subscription, event and subscription-handler routes on *app*."""

    @app.get("/dapr/subscribe")
    async def get_dapr_subscriptions() -> JSONResponse:
        return JSONResponse(
            content=[_subscription_entry(sub) for sub in source.subscriptions]
            + [_trigger_entry(trigger) for trigger in source.event_triggers]
        )

    @app.post(
        "/events/v1/event/{event_id}",
        dependencies=[Depends(_require_configured(source.workflow_config))],
    )
    async def handle_event(event_id: str, request: Request) -> JSONResponse:
        return await _handle_event(source, event_id, request)

    @app.post("/events/v1/drop")
    async def drop_event() -> JSONResponse:
        return JSONResponse(content={"success": False, "status": "DROP"})

    for sub in source.subscriptions:
        app.add_api_route(
            f"/subscriptions/v1/{sub.route}", sub.handler, methods=["POST"]
        )
