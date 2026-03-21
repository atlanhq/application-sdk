"""P4: Lifecycle hook and context tests.

Verifies that on_complete() is called after both successful and failed runs,
and that AppContext properties are populated correctly during execution.

Requires a running Temporal dev server (see conftest.py).
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from application_sdk.app.base import App
from application_sdk.app.context import AppContext
from application_sdk.app.task import task
from application_sdk.contracts.base import Input, Output
from application_sdk.execution.retry import NO_RETRY

# Module-level list shared between test thread and worker (via passthrough_modules).
_on_complete_log: list[str] = []


@pytest.mark.integration
async def test_on_complete_called_after_success(run_worker, executor):
    """P4.1: on_complete() is invoked after a successful run()."""
    _on_complete_log.clear()

    @dataclass
    class SuccessInput(Input):
        pass

    @dataclass
    class SuccessOutput(Output):
        pass

    class SuccessLifecycleApp(App):
        async def on_complete(self) -> None:
            _on_complete_log.append("called")

        @task
        async def do_work(self, input: SuccessInput) -> SuccessOutput:
            return SuccessOutput()

        async def run(self, input: SuccessInput) -> SuccessOutput:
            return await self.do_work(input)

    async with run_worker():
        context = AppContext(
            app_name=SuccessLifecycleApp._app_name, app_version="1.0.0"
        )
        await executor.execute(
            SuccessLifecycleApp,
            SuccessInput(),
            context=context,
            retry_policy=NO_RETRY,
        )

    assert _on_complete_log == ["called"]


@pytest.mark.integration
async def test_on_complete_called_after_failure(run_worker, executor):
    """P4.2: on_complete() is invoked even when run() raises an exception."""
    _on_complete_log.clear()

    @dataclass
    class FailInput(Input):
        pass

    @dataclass
    class FailOutput(Output):
        pass

    class FailLifecycleApp(App):
        async def on_complete(self) -> None:
            _on_complete_log.append("called")

        @task(retry_max_attempts=1)
        async def do_work(self, input: FailInput) -> FailOutput:
            raise ValueError("intentional failure")

        async def run(self, input: FailInput) -> FailOutput:
            return await self.do_work(input)

    async with run_worker():
        context = AppContext(app_name=FailLifecycleApp._app_name, app_version="1.0.0")
        with pytest.raises(Exception):
            await executor.execute(
                FailLifecycleApp,
                FailInput(),
                context=context,
                retry_policy=NO_RETRY,
            )

    assert _on_complete_log == ["called"]


@pytest.mark.integration
async def test_context_properties_populated(run_worker, executor):
    """P4.3: self.context exposes app_name, run_id, correlation_id in a @task."""

    @dataclass
    class CtxInput(Input):
        pass

    @dataclass
    class CtxOutput(Output):
        app_name: str = ""
        run_id: str = ""
        correlation_id: str = ""

    class ContextApp(App):
        @task
        async def read_context(self, input: CtxInput) -> CtxOutput:
            ctx = self.context
            return CtxOutput(
                app_name=ctx.app_name,
                run_id=ctx.run_id,
                correlation_id=ctx.correlation_id,
            )

        async def run(self, input: CtxInput) -> CtxOutput:
            return await self.read_context(input)

    async with run_worker():
        context = AppContext(app_name=ContextApp._app_name, app_version="1.0.0")
        result = await executor.execute(
            ContextApp,
            CtxInput(),
            context=context,
            retry_policy=NO_RETRY,
        )

    assert result.app_name == ContextApp._app_name
    assert result.run_id != ""
    assert result.correlation_id != ""
