"""P: Pooled-task sandbox integration test.

Declaring ``@task(pool=...)`` makes the workflow resolve that pool to a task
queue during activation: ``_wrap_instance_tasks`` (``app/base.py``) builds
a wrapper for *every* task eagerly, and the wrapper factory calls
``resolve_pool_queue``. That resolution therefore runs inside the Temporal
sandbox, so anything it does at runtime must be sandbox-safe — including how
it reaches ``os.environ``.

The task is never invoked here on purpose. The failure is at activation, before
any activity is scheduled, so a declared-but-uncalled pooled task is the
smallest shape that reproduces it and needs no second worker on the pool queue.

Requires a running Temporal dev server (see conftest.py).
"""

import asyncio

import pytest

from application_sdk.app.base import App
from application_sdk.app.context import AppContext
from application_sdk.app.entrypoint import entrypoint
from application_sdk.app.task import task
from application_sdk.contracts.base import Input, Output
from application_sdk.execution.retry import NO_RETRY

# ---------------------------------------------------------------------------
# Module-level classes so Temporal's sandboxed runner can import them by path.
# ---------------------------------------------------------------------------


class PoolInput(Input):
    value: str = ""


class PoolOutput(Output):
    length: int = 0


class PooledTaskApp(App):
    """Declares a pooled task the entry point never calls."""

    name = "pooled-task-app"

    @task(pool="heavy")
    async def heavy_work(self, input: PoolInput) -> PoolOutput:  # pragma: no cover
        # Never invoked — declaring it is enough to exercise pool resolution.
        return PoolOutput(length=len(input.value))

    @entrypoint
    async def run_light(self, input: PoolInput) -> PoolOutput:
        return PoolOutput(length=len(input.value))


@pytest.mark.integration
async def test_pooled_task_declaration_does_not_break_activation(
    run_worker, executor, reregister_app
):
    """P1.1: a declared pooled task must not fail the workflow at activation.

    Pool resolution runs inside the sandbox. If it is sandbox-unsafe, the app
    fails before its entry point runs — and Temporal retries the failed
    activation forever, so the run never reaches a terminal state. The
    ``wait_for`` bound turns that wedge into a fast, attributable failure
    instead of a hung suite.
    """
    reregister_app(PooledTaskApp)
    async with run_worker():
        context = AppContext(app_name=PooledTaskApp._app_name, app_version="1.0.0")
        try:
            result = await asyncio.wait_for(
                executor.execute(
                    PooledTaskApp,
                    PoolInput(value="abcd"),
                    context=context,
                    retry_policy=NO_RETRY,
                    entry_point="run-light",
                ),
                timeout=60,
            )
        except TimeoutError:
            pytest.fail(
                "Workflow never completed: activation is failing and Temporal is "
                "retrying it forever. Pool resolution is sandbox-unsafe again — "
                "check that resolve_pool_queue reaches os via a module-level import."
            )
    assert result.length == 4
