"""Records the workflow replay fixtures and replays them across worker flags.

Runs :class:`ReplayFixtureApp` on the dev server under each of
``harness.RECORDINGS`` (the production defaults, and both run-shaping flags
off), with every built-in interceptor the worker registers. Each fresh history
must replay on a worker with every combination of the event and cleanup flags:
the run's flags are pinned in its history, so the replaying pod's settings do
not change its commands.

With ``RECORD_REPLAY_FIXTURES=1`` the histories are also written to
``tests/unit/execution/fixtures/replay/``, which the unit test
``tests/unit/execution/test_workflow_replay.py`` replays on every PR::

    RECORD_REPLAY_FIXTURES=1 uv run pytest tests/integration/test_workflow_replay.py
"""

from __future__ import annotations

import json
import os
from unittest import mock
from uuid import uuid4

import pytest

from application_sdk.app.context import AppContext
from application_sdk.execution.retry import NO_RETRY
from tests.unit.execution.fixtures.replay.harness import (
    FLAG_COMBINATIONS,
    RECORD_ENV,
    RECORDINGS,
    fixture_path,
    replay,
)
from tests.unit.execution.fixtures.replay.replay_app import (
    ReplayFixtureApp,
    ReplayInput,
)

_IDENTITY = "replay-fixture-worker"

_BINDING_TARGET = (
    "application_sdk.execution._temporal.interceptors.events._publish_event_via_binding"
)


@pytest.mark.integration
@pytest.mark.parametrize("name", sorted(RECORDINGS))
async def test_a_recorded_run_replays_on_every_worker_flag_combination(
    name, run_worker, executor, reregister_app, temporal_client, monkeypatch
):
    flags = RECORDINGS[name]
    # The integration conftest switches both off for every test; set them to
    # this recording's values before the worker reads them at start.
    monkeypatch.setenv(
        "APPLICATION_SDK_ENABLE_EVENT_INTERCEPTOR", str(flags["events"]).lower()
    )
    monkeypatch.setenv(
        "APPLICATION_SDK_ENABLE_CLEANUP_INTERCEPTOR", str(flags["cleanup"]).lower()
    )
    reregister_app(ReplayFixtureApp)
    workflow_id = f"replay-{name}-{uuid4().hex[:8]}"

    # No Dapr binding here; publish_event's activity body is irrelevant to
    # replay, which checks the commands the workflow emits.
    with mock.patch(_BINDING_TARGET, new=mock.AsyncMock()):
        async with run_worker():
            output = await executor.execute(
                ReplayFixtureApp,
                ReplayInput(value=41, workflow_id=workflow_id),
                context=AppContext(
                    app_name=ReplayFixtureApp._app_name, app_version="1.0.0"
                ),
                retry_policy=NO_RETRY,
            )
    assert output.result == 42

    history = await temporal_client.get_workflow_handle(workflow_id).fetch_history()
    activity_types = [
        e.activity_task_scheduled_event_attributes.activity_type.name
        for e in history.events
        if e.HasField("activity_task_scheduled_event_attributes")
    ]
    # The recording has the shape its flags say, so the replays below cover it.
    assert ("publish_event" in activity_types) is flags["events"]
    assert any("cleanup_files" in t for t in activity_types) is flags["cleanup"]

    for combination in FLAG_COMBINATIONS:
        result = await replay(history, **combination)
        assert result.replay_failure is None, (combination, result.replay_failure)

    if os.environ.get(RECORD_ENV) == "1":
        # The worker identity (pid@hostname) is stamped on events and on the
        # sticky queue's name; a committed fixture carries a neutral one.
        # Replay never reads it.
        scrubbed = history.to_json().replace(temporal_client.identity, _IDENTITY)
        fixture_path(name).write_text(json.dumps(json.loads(scrubbed), indent=2) + "\n")
