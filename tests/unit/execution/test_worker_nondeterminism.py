"""Regression coverage for terminal Temporal replay failures."""

from unittest import mock

from temporalio.workflow import NondeterminismError

from application_sdk.app.base import App
from application_sdk.contracts.base import Input, Output
from application_sdk.execution._temporal.worker import create_worker


class ReplayInput(Input):
    """Minimal concrete workflow input."""


class ReplayOutput(Output):
    """Minimal concrete workflow output."""


def test_replay_nondeterminism_fails_the_workflow_run(clean_app_registry) -> None:
    """Replay mismatches must fail the run instead of retrying workflow tasks."""

    class ReplayApp(App):
        async def run(self, input: ReplayInput) -> ReplayOutput:
            return ReplayOutput()

    client = mock.MagicMock()
    client.namespace = "default"
    client.service_client.config.target_host = "localhost:7233"

    with mock.patch("application_sdk.execution._temporal.worker.Worker") as worker:
        create_worker(client)

    declared = tuple(worker.call_args.kwargs["workflow_failure_exception_types"])
    assert issubclass(NondeterminismError, declared), (
        "Replay nondeterminism would retry workflow tasks until the run timeout "
        "instead of failing the workflow run with the replay error"
    )
