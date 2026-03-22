"""P1: Core execution round-trip tests.

These tests verify that Apps execute correctly through a real Temporal worker,
with typed Input/Output flowing correctly through serialization.

Requires a running Temporal dev server (see conftest.py).
"""

import pytest
from pydantic import Field

from application_sdk.app.base import App
from application_sdk.app.context import AppContext
from application_sdk.app.task import task
from application_sdk.contracts.base import Input, Output
from application_sdk.contracts.types import FileReference
from application_sdk.execution.retry import NO_RETRY

# ---------------------------------------------------------------------------
# App/Input/Output classes must be at module level so Temporal's sandboxed
# workflow runner can import them by module path.  clean_registries (autouse)
# resets AppRegistry/TaskRegistry before each test, so each test calls
# reregister_app() (from conftest) before spinning up the worker.
# ---------------------------------------------------------------------------


class AddInput(Input):
    value: int = 0


class AddOutput(Output):
    result: int = 0


class AddOneApp(App):
    @task
    async def add_one(self, input: AddInput) -> AddOutput:
        return AddOutput(result=input.value + 1)

    async def run(self, input: AddInput) -> AddOutput:
        return await self.add_one(input)


class PipeInput(Input):
    text: str = ""


class PipeUpperOutput(Output):
    result: str = ""


class ExclaimInput(Input):
    text: str = ""


class PipeOutput(Output):
    result: str = ""


class PipeApp(App):
    @task
    async def upper(self, input: PipeInput) -> PipeUpperOutput:
        return PipeUpperOutput(result=input.text.upper())

    @task
    async def exclaim(self, input: ExclaimInput) -> PipeOutput:
        return PipeOutput(result=input.text + "!")

    async def run(self, input: PipeInput) -> PipeOutput:
        upper_out = await self.upper(input)
        return await self.exclaim(ExclaimInput(text=upper_out.result))


class Inner(Input):
    x: int = 0
    y: int = 0


class ComplexInput(Input):
    inner: Inner = Field(default_factory=Inner)
    label: str = ""
    optional_flag: bool | None = None


class ComplexOutput(Output):
    sum: int = 0
    label: str = ""
    flag_was_set: bool = False


class ComplexApp(App):
    @task
    async def compute(self, input: ComplexInput) -> ComplexOutput:
        return ComplexOutput(
            sum=input.inner.x + input.inner.y,
            label=input.label,
            flag_was_set=input.optional_flag is not None,
        )

    async def run(self, input: ComplexInput) -> ComplexOutput:
        return await self.compute(input)


class FileInput(Input):
    ref: FileReference = Field(default_factory=FileReference)


class FileOutput(Output):
    local_path: str = ""
    is_durable: bool = False


class FileEchoApp(App):
    @task
    async def echo_ref(self, input: FileInput) -> FileOutput:
        return FileOutput(
            local_path=input.ref.local_path or "",
            is_durable=input.ref.is_durable,
        )

    async def run(self, input: FileInput) -> FileOutput:
        return await self.echo_ref(input)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_single_task_round_trip(run_worker, executor, reregister_app):
    """P1.1: Single @task App correctly executes and returns typed Output."""
    reregister_app(AddOneApp)
    async with run_worker():
        context = AppContext(app_name=AddOneApp._app_name, app_version="1.0.0")
        result = await executor.execute(
            AddOneApp, AddInput(value=41), context=context, retry_policy=NO_RETRY
        )
    assert result.result == 42


@pytest.mark.integration
async def test_multi_task_workflow_data_flows(run_worker, executor, reregister_app):
    """P1.2: Multi-task workflow — data passes correctly between tasks."""
    reregister_app(PipeApp)
    async with run_worker():
        context = AppContext(app_name=PipeApp._app_name, app_version="1.0.0")
        result = await executor.execute(
            PipeApp, PipeInput(text="hello"), context=context, retry_policy=NO_RETRY
        )
    assert result.result == "HELLO!"


@pytest.mark.integration
async def test_complex_contract_types(run_worker, executor, reregister_app):
    """P1.3: Complex contract types — nested models and optional fields."""
    reregister_app(ComplexApp)
    async with run_worker():
        context = AppContext(app_name=ComplexApp._app_name, app_version="1.0.0")
        result = await executor.execute(
            ComplexApp,
            ComplexInput(inner=Inner(x=10, y=32), label="test", optional_flag=True),
            context=context,
            retry_policy=NO_RETRY,
        )
    assert result.sum == 42
    assert result.label == "test"
    assert result.flag_was_set is True


@pytest.mark.integration
async def test_file_reference_serialization(run_worker, executor, reregister_app):
    """P1.4: FileReference flows through Temporal serialization unchanged."""
    reregister_app(FileEchoApp)
    async with run_worker():
        context = AppContext(app_name=FileEchoApp._app_name, app_version="1.0.0")
        result = await executor.execute(
            FileEchoApp,
            FileInput(ref=FileReference(local_path="/tmp/data.csv", is_durable=False)),
            context=context,
            retry_policy=NO_RETRY,
        )
    assert result.local_path == "/tmp/data.csv"
    assert result.is_durable is False
