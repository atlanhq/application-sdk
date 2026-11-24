"""Committed workflow histories replay on every worker flag combination.

The histories under ``fixtures/replay/`` were recorded on the dev server by
``tests/integration/test_workflow_replay.py``: one with the production
defaults and every built-in interceptor (events and cleanup on), one with both
flags off. Each replays here through the interceptor chain ``create_worker``
registers (``build_interceptors``), on a worker with every combination of
``APPLICATION_SDK_ENABLE_EVENT_INTERCEPTOR`` and
``APPLICATION_SDK_ENABLE_CLEANUP_INTERCEPTOR``. A run's flags are pinned in its
history, so the replaying worker's flags must not change its commands; a
nondeterminism error here means a pod's settings leak into workflow code.

Replay needs no server, so this runs in the unit tier on every PR. A change to
what the fixture App's workflow emits (a new command, a reordered one) fails
here too: that is the replay break an in-flight run would hit, so either guard
the change with ``workflow.patched`` or, if old runs cannot reach the new
build, re-record the fixtures (see ``harness.RECORD_ENV``).
"""

from __future__ import annotations

import pytest

from tests.unit.execution.fixtures.replay.harness import (
    FLAG_COMBINATIONS,
    RECORDINGS,
    load_fixture,
    replay,
)
from tests.unit.execution.fixtures.replay.replay_app import ReplayFixtureApp


@pytest.fixture(autouse=True)
def _registered(clean_app_registry, clean_task_registry) -> None:
    """Register the fixture App, as importing it did before the reset."""
    from application_sdk.app.base import _register_tasks

    clean_app_registry.register(
        name=ReplayFixtureApp._app_name,
        version=ReplayFixtureApp.version,
        app_cls=ReplayFixtureApp,
        input_type=ReplayFixtureApp._input_type,
        output_type=ReplayFixtureApp._output_type,
        entry_points=ReplayFixtureApp._app_metadata.entry_points,
        legacy_workflow_types=ReplayFixtureApp._app_metadata.legacy_workflow_types,
        allow_override=True,
    )
    _register_tasks(ReplayFixtureApp, ReplayFixtureApp._app_name)


@pytest.mark.parametrize(
    "flags",
    FLAG_COMBINATIONS,
    ids=lambda f: f"events={f['events']}-cleanup={f['cleanup']}",
)
@pytest.mark.parametrize("recording", sorted(RECORDINGS))
async def test_a_committed_history_replays_whatever_the_worker_flags(
    recording: str, flags: dict[str, bool]
) -> None:
    result = await replay(load_fixture(recording), **flags)
    assert result.replay_failure is None, result.replay_failure
