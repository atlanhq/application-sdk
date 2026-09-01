"""Resolving the workflow ID an App run is dispatched under.

One function, called by both dispatch paths — the ``/workflows/v1/start``
handler (``application_sdk.handler.service``) and
``TemporalExecutorBackend.execute``/``start`` — so the invariant they share is
enforced by a shared body rather than by two hand-kept copies: the ID Temporal
runs under and the ``Input.workflow_id`` the workflow reads are always the same
value, and an input dispatched with the field left at its ``""`` default is a
shape production never sends (apps that root artifact paths on the field would
collapse every run of a session onto one shared prefix).
"""

from __future__ import annotations

from typing import Any

from pydantic import ValidationError


def resolve_dispatch_workflow_id(
    input_data: Any, app_name: str, *, explicit_workflow_id: str = ""
) -> str:
    """Resolve the Temporal dispatch ID and stamp it onto ``input_data``.

    A caller-supplied ID wins — ``explicit_workflow_id`` (the handler pops it
    from the request body before validation) or, failing that, a non-empty
    ``input_data.workflow_id`` (how a backend caller supplies one). Otherwise
    one is minted as ``{app_name}-{config_hash}-{uuid4().hex[:8]}``. An input
    without ``config_hash`` — test doubles; every real ``Input`` has it — mints
    ``{app_name}-{short_id}`` instead. Either way the resolved ID is stamped
    back onto ``input_data.workflow_id``.

    Unlike the correlation stamp there is no memo fallback, so an input that
    rejects the stamp (dict/frozen shapes) is dispatched with its field
    unchanged — logged at WARNING, because that run's field diverges from its
    dispatch ID, which is exactly the shape this function exists to rule out.
    """
    workflow_id = explicit_workflow_id or getattr(input_data, "workflow_id", "")
    if not workflow_id:
        from uuid import uuid4  # noqa: PLC0415 — stdlib uuid; lazy use

        config_hash = (
            input_data.config_hash() if hasattr(input_data, "config_hash") else ""
        )
        short_id = uuid4().hex[:8]
        workflow_id = (
            f"{app_name}-{config_hash}-{short_id}"
            if config_hash
            else f"{app_name}-{short_id}"
        )
    try:
        input_data.workflow_id = workflow_id
    except (AttributeError, TypeError, ValidationError):
        from application_sdk.observability.logger_adaptor import (  # noqa: PLC0415 — deferred: keeps observability off this module's import path
            get_logger,
        )

        get_logger(__name__).warning(
            "Input of type %s rejected the workflow_id stamp; it is dispatched "
            "with its workflow_id field unchanged while Temporal runs it as %s",
            type(input_data).__name__,
            workflow_id,
        )
    return workflow_id
