"""Replays a recorded history through the worker's production interceptor chain.

Shared by the unit replay test (committed fixtures, every PR, no server) and the
integration test that records those fixtures against the dev server.
"""

from __future__ import annotations

from pathlib import Path

from temporalio.client import WorkflowHistory
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.worker import Replayer, WorkflowReplayResult
from temporalio.worker.workflow_sandbox import SandboxedWorkflowRunner

from application_sdk.execution._temporal.worker import build_interceptors
from application_sdk.execution._temporal.workflows import get_all_app_workflows
from application_sdk.execution.sandbox import SandboxConfig, host_library_modules
from application_sdk.execution.settings import InterceptorSettings

FIXTURES_DIR = Path(__file__).parent
"""Where the committed histories live, one ``<name>.json`` per recording."""

RECORD_ENV = "RECORD_REPLAY_FIXTURES"
"""Set to ``1`` when running ``tests/integration/test_workflow_replay.py`` to
rewrite the committed histories from fresh recordings."""

RECORDINGS: dict[str, dict[str, bool]] = {
    # Production defaults: every built-in interceptor, events and cleanup on.
    "defaults": {"events": True, "cleanup": True},
    # Both run-shaping flags off, so the run records their markers.
    "flags_off": {"events": False, "cleanup": False},
}
"""Each committed history's name and the worker flags it was recorded under."""

FLAG_COMBINATIONS: list[dict[str, bool]] = [
    {"events": events, "cleanup": cleanup}
    for events in (True, False)
    for cleanup in (True, False)
]
"""Every worker the recorded histories must replay on."""


def fixture_path(name: str) -> Path:
    return FIXTURES_DIR / f"{name}.json"


def load_fixture(name: str) -> WorkflowHistory:
    path = fixture_path(name)
    return WorkflowHistory.from_json(path.stem, path.read_text())


async def replay(
    history: WorkflowHistory, *, events: bool, cleanup: bool
) -> WorkflowReplayResult:
    """Replay ``history`` on a worker configured with ``events`` / ``cleanup``.

    The interceptor chain is :func:`build_interceptors`, the list
    ``create_worker`` registers, and the sandbox passes host libraries through
    the way ``create_worker`` does, leaving ``tests`` (the App's package) to be
    re-imported and checked.
    """
    replayer = Replayer(
        workflows=get_all_app_workflows(),
        interceptors=build_interceptors(
            InterceptorSettings(
                enable_event_interceptor=events, enable_cleanup_interceptor=cleanup
            )
        ),
        workflow_runner=SandboxedWorkflowRunner(
            restrictions=SandboxConfig()
            .with_passthrough_modules(*host_library_modules({"tests"}))
            .to_temporal_restrictions()
        ),
        data_converter=pydantic_data_converter,
    )
    return await replayer.replay_workflow(history, raise_on_replay_failure=False)
