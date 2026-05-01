"""Integration tests: event interceptor serde through real Temporal (G8).

These tests run against a real Temporal dev server (see conftest.py) and
verify that the event interceptor pipeline — which passes Pydantic BaseModel
instances inside dicts through Temporal's data converter — works end-to-end.

What's being tested
-------------------
``EventWorkflowInboundInterceptor`` schedules ``publish_event`` activities
with payloads like::

    {"metadata": EventMetadata(...), "event_type": "...", ...}

Temporal serialises this dict (EventMetadata → .dict() via AdvancedJSONEncoder),
transmits it, and deserialises it on the activity side as a plain ``dict``.
``publish_event`` then calls ``Event(**event_data)`` to reconstruct the model.

These tests confirm this full path works against real Temporal and that
``_publish_event_via_binding`` receives correctly reconstructed ``Event``
objects with the right field values.

Setup notes
-----------
- The global conftest sets ``APPLICATION_SDK_ENABLE_EVENT_INTERCEPTOR=false``
  via ``os.environ.setdefault``.  Tests that need the interceptor use
  ``monkeypatch.setenv`` to override it before ``create_worker`` is called.
- ``_publish_event_via_binding`` is patched to a no-op that captures events,
  avoiding the need for a real Dapr binding.
- ``publish_event`` activity is only registered on the worker when the
  event interceptor is enabled (see ``execution/_temporal/worker.py``).
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from unittest import mock

import pytest

from application_sdk.app.base import App
from application_sdk.app.context import AppContext
from application_sdk.app.task import task
from application_sdk.contracts.base import Input, Output
from application_sdk.contracts.events import (
    ApplicationEventNames,
    EventMetadata,
    WorkflowStates,
)
from application_sdk.execution.retry import NO_RETRY

# ---------------------------------------------------------------------------
# App definitions — must be at module level for Temporal's sandboxed runner
# ---------------------------------------------------------------------------


class _NullInput(Input):
    pass


class _NullOutput(Output):
    pass


class _NullApp(App):
    """Minimal App: single no-op task, used to trigger workflow/activity events."""

    _app_name = "events-serde-null-app"
    _app_version = "1.0.0"

    @task(retry_max_attempts=1)
    async def no_op(self, input: _NullInput) -> _NullOutput:
        return _NullOutput()

    async def run(self, input: _NullInput) -> _NullOutput:
        return await self.no_op(input)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_BINDING_TARGET = (
    "application_sdk.execution._temporal.interceptors.events._publish_event_via_binding"
)


@asynccontextmanager
async def _run_worker_with_events(run_worker_fixture, monkeypatch):
    """Context manager: enable event interceptor and yield the worker context.

    Overrides APPLICATION_SDK_ENABLE_EVENT_INTERCEPTOR so that create_worker
    registers publish_event as an activity and adds EventInterceptor.
    """
    monkeypatch.setenv("APPLICATION_SDK_ENABLE_EVENT_INTERCEPTOR", "true")
    async with run_worker_fixture():
        yield


def _make_capturing_mock() -> tuple[mock.AsyncMock, list]:
    """Return (async mock, capture list) — mock appends each Event to the list."""
    captured: list = []

    async def _capture(event) -> None:
        captured.append(event)

    return mock.AsyncMock(side_effect=_capture), captured


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_publish_event_activity_receives_correctly_reconstructed_event(
    run_worker, executor, reregister_app, monkeypatch
):
    """The publish_event activity correctly reconstructs Event from Temporal-serde dict.

    Flow:
      EventWorkflowInboundInterceptor
        → workflow.execute_activity(publish_event, {"metadata": EventMetadata(...)})
        → Temporal serde (EventMetadata → .dict() → JSON → plain dict)
        → publish_event activity receives plain dict
        → Event(**event_data) reconstructs the model via Pydantic
        → _publish_event_via_binding called with Event instance
    """
    reregister_app(_NullApp)

    capturing_mock, captured_events = _make_capturing_mock()

    with mock.patch(_BINDING_TARGET, new=capturing_mock):
        async with _run_worker_with_events(run_worker, monkeypatch):
            context = AppContext(
                app_name=_NullApp._app_name, app_version=_NullApp._app_version
            )
            await executor.execute(
                _NullApp, _NullInput(), context=context, retry_policy=NO_RETRY
            )

    # publish_event should have been called at least for workflow_start and workflow_end
    assert (
        len(captured_events) >= 2
    ), f"Expected at least 2 events (workflow start + end), got {len(captured_events)}"

    # Every captured call must have received a proper Event instance, not a dict
    from application_sdk.contracts.events import Event

    for event in captured_events:
        assert isinstance(
            event, Event
        ), f"_publish_event_via_binding received {type(event)!r}, expected Event"


@pytest.mark.integration
async def test_event_interceptor_emits_workflow_start_event(
    run_worker, executor, reregister_app, monkeypatch
):
    """A workflow_start event is published when a workflow begins execution."""
    reregister_app(_NullApp)
    capturing_mock, captured_events = _make_capturing_mock()

    with mock.patch(_BINDING_TARGET, new=capturing_mock):
        async with _run_worker_with_events(run_worker, monkeypatch):
            context = AppContext(
                app_name=_NullApp._app_name, app_version=_NullApp._app_version
            )
            await executor.execute(
                _NullApp, _NullInput(), context=context, retry_policy=NO_RETRY
            )

    event_names = [e.event_name for e in captured_events]
    assert (
        ApplicationEventNames.WORKFLOW_START.value in event_names
    ), f"workflow_start not found in published events: {event_names}"


@pytest.mark.integration
async def test_event_interceptor_emits_workflow_end_event_with_completed_state(
    run_worker, executor, reregister_app, monkeypatch
):
    """A workflow_end event is published with COMPLETED state after successful execution."""
    reregister_app(_NullApp)
    capturing_mock, captured_events = _make_capturing_mock()

    with mock.patch(_BINDING_TARGET, new=capturing_mock):
        async with _run_worker_with_events(run_worker, monkeypatch):
            context = AppContext(
                app_name=_NullApp._app_name, app_version=_NullApp._app_version
            )
            await executor.execute(
                _NullApp, _NullInput(), context=context, retry_policy=NO_RETRY
            )

    end_events = [
        e
        for e in captured_events
        if e.event_name == ApplicationEventNames.WORKFLOW_END.value
    ]
    assert end_events, "No workflow_end event found"

    end_event = end_events[-1]
    assert (
        end_event.metadata.workflow_state == WorkflowStates.COMPLETED.value
    ), f"Expected workflow_state=completed, got {end_event.metadata.workflow_state!r}"


@pytest.mark.integration
async def test_event_interceptor_emits_activity_start_and_end_events(
    run_worker, executor, reregister_app, monkeypatch
):
    """Activity start and end events are published for each @task execution."""
    reregister_app(_NullApp)
    capturing_mock, captured_events = _make_capturing_mock()

    with mock.patch(_BINDING_TARGET, new=capturing_mock):
        async with _run_worker_with_events(run_worker, monkeypatch):
            context = AppContext(
                app_name=_NullApp._app_name, app_version=_NullApp._app_version
            )
            await executor.execute(
                _NullApp, _NullInput(), context=context, retry_policy=NO_RETRY
            )

    event_names = [e.event_name for e in captured_events]
    assert (
        ApplicationEventNames.ACTIVITY_START.value in event_names
    ), f"activity_start not found in published events: {event_names}"
    assert (
        ApplicationEventNames.ACTIVITY_END.value in event_names
    ), f"activity_end not found in published events: {event_names}"


@pytest.mark.integration
async def test_workflow_start_event_metadata_has_correct_workflow_state(
    run_worker, executor, reregister_app, monkeypatch
):
    """The workflow_start event carries RUNNING state in its metadata."""
    reregister_app(_NullApp)
    capturing_mock, captured_events = _make_capturing_mock()

    with mock.patch(_BINDING_TARGET, new=capturing_mock):
        async with _run_worker_with_events(run_worker, monkeypatch):
            context = AppContext(
                app_name=_NullApp._app_name, app_version=_NullApp._app_version
            )
            await executor.execute(
                _NullApp, _NullInput(), context=context, retry_policy=NO_RETRY
            )

    start_events = [
        e
        for e in captured_events
        if e.event_name == ApplicationEventNames.WORKFLOW_START.value
    ]
    assert start_events, "No workflow_start event found"

    start_event = start_events[0]
    assert isinstance(start_event.metadata, EventMetadata), (
        f"metadata is {type(start_event.metadata)!r}, expected EventMetadata — "
        "Pydantic model was not reconstructed correctly after Temporal serde"
    )
    assert start_event.metadata.workflow_state == WorkflowStates.RUNNING.value


@pytest.mark.integration
async def test_activity_end_event_contains_duration_ms(
    run_worker, executor, reregister_app, monkeypatch
):
    """Activity end events include a duration_ms field in their data payload."""
    reregister_app(_NullApp)
    capturing_mock, captured_events = _make_capturing_mock()

    with mock.patch(_BINDING_TARGET, new=capturing_mock):
        async with _run_worker_with_events(run_worker, monkeypatch):
            context = AppContext(
                app_name=_NullApp._app_name, app_version=_NullApp._app_version
            )
            await executor.execute(
                _NullApp, _NullInput(), context=context, retry_policy=NO_RETRY
            )

    end_events = [
        e
        for e in captured_events
        if e.event_name == ApplicationEventNames.ACTIVITY_END.value
    ]
    assert end_events, "No activity_end event found"

    for end_event in end_events:
        assert (
            "duration_ms" in end_event.data
        ), f"activity_end event missing duration_ms in data: {end_event.data}"
        assert isinstance(end_event.data["duration_ms"], (int, float))
