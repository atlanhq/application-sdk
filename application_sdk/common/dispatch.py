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

from enum import StrEnum
from typing import Any

from pydantic import ValidationError


class StampFailurePolicy(StrEnum):
    """What a rejected ``workflow_id`` stamp does to the dispatch."""

    RAISE = "raise"
    """Let the assignment error propagate — the production default.

    ``/workflows/v1/start`` answers it through its own boundary clauses, as
    it always has (``ValidationError`` -> 500, ``TypeError`` -> 400), and the
    workflow is never dispatched. A run whose ``Input.workflow_id`` disagrees
    with the ID Temporal runs it under is the shape this module exists to
    rule out, so the production path refuses it rather than logging it.
    """

    WARN = "warn"
    """Log at WARNING and dispatch anyway — the integration kit's policy.

    ``TemporalExecutorBackend`` dispatches the dict and frozen test doubles
    the kit deliberately accepts; those cannot take the stamp, are out of
    scope for the artifact-path collision, and failing them would block
    suites that never read the field. The WARNING is what lets a suite that
    does hit the path find out it did.
    """


def resolve_dispatch_workflow_id(
    input_data: Any,
    app_name: str,
    *,
    explicit_workflow_id: str = "",
    on_stamp_failure: StampFailurePolicy = StampFailurePolicy.RAISE,
) -> str:
    """Resolve the Temporal dispatch ID and stamp it onto ``input_data``.

    A caller-supplied ID wins — ``explicit_workflow_id`` (the handler pops it
    from the request body before validation) or, failing that, a non-empty
    ``input_data.workflow_id`` (how a backend caller supplies one). Otherwise
    one is minted as ``{app_name}-{config_hash}-{uuid4().hex[:8]}``. An input
    without ``config_hash`` — test doubles; every real ``Input`` has it — mints
    ``{app_name}-{short_id}`` instead. Either way the resolved ID is stamped
    back onto ``input_data.workflow_id``.

    Unlike the correlation stamp there is no memo fallback, so a rejected
    stamp cannot be repaired: that run's field diverges from its dispatch ID,
    which is exactly the shape this function exists to rule out. The two
    dispatch paths answer that differently, and ``on_stamp_failure`` makes the
    asymmetry deliberate — see :class:`StampFailurePolicy` for what each
    member means and why that path chose it.

    Two shapes are accepted here that the handler's previous inline copy would
    have rejected, both unreachable through a real ``Input``: an input carrying
    a non-empty ``workflow_id`` of its own (the handler pops the caller's value
    from the body before validation, so the field is always at its ``""``
    default there unless a subclass declares another), and an input without
    ``config_hash`` (every real ``Input`` has it).
    """
    # Coerced rather than compared: an untyped caller passing a bare string
    # gets a loud ValueError on an unknown value, where ``is`` would have
    # quietly fallen through to the *less* strict branch.
    policy = StampFailurePolicy(on_stamp_failure)
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
    if policy is StampFailurePolicy.RAISE:
        input_data.workflow_id = workflow_id
        return workflow_id
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
