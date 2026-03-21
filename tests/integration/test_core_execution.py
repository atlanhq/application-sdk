"""P1: Core execution round-trip tests.

These tests verify that Apps execute correctly through a real Temporal worker,
with typed Input/Output flowing correctly through serialization.

Requires a running Temporal dev server (see conftest.py).
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from application_sdk.app.base import App
from application_sdk.app.context import AppContext
from application_sdk.app.task import task
from application_sdk.contracts.base import Input, Output
from application_sdk.contracts.types import FileReference
from application_sdk.execution.retry import NO_RETRY


@pytest.mark.integration
async def test_single_task_round_trip(run_worker, executor):
    """P1.1: Single @task App correctly executes and returns typed Output."""

    @dataclass
    class AddInput(Input):
        value: int = 0

    @dataclass
    class AddOutput(Output):
        result: int = 0

    class AddOneApp(App):
        @task
        async def add_one(self, input: AddInput) -> AddOutput:
            return AddOutput(result=input.value + 1)

        async def run(self, input: AddInput) -> AddOutput:
            return await self.add_one(input)

    async with run_worker():
        context = AppContext(app_name=AddOneApp._app_name, app_version="1.0.0")
        result = await executor.execute(
            AddOneApp, AddInput(value=41), context=context, retry_policy=NO_RETRY
        )
    assert result.result == 42


@pytest.mark.integration
async def test_multi_task_workflow_data_flows(run_worker, executor):
    """P1.2: Multi-task workflow — data passes correctly between tasks."""

    @dataclass
    class PipeInput(Input):
        text: str = ""

    @dataclass
    class PipeOutput(Output):
        result: str = ""

    class PipeApp(App):
        @task
        async def upper(self, input: PipeInput) -> PipeOutput:
            return PipeOutput(result=input.text.upper())

        @task
        async def exclaim(self, input: PipeOutput) -> PipeOutput:
            return PipeOutput(result=input.result + "!")

        async def run(self, input: PipeInput) -> PipeOutput:
            upper_out = await self.upper(input)
            return await self.exclaim(upper_out)

    async with run_worker():
        context = AppContext(app_name=PipeApp._app_name, app_version="1.0.0")
        result = await executor.execute(
            PipeApp, PipeInput(text="hello"), context=context, retry_policy=NO_RETRY
        )
    assert result.result == "HELLO!"


@pytest.mark.integration
async def test_complex_contract_types(run_worker, executor):
    """P1.3: Complex contract types — nested dataclasses and optional fields."""

    @dataclass
    class Inner(Input):
        x: int = 0
        y: int = 0

    @dataclass
    class ComplexInput(Input):
        inner: Inner = None  # type: ignore[assignment]
        label: str = ""
        optional_flag: bool | None = None

        def __post_init__(self) -> None:
            if self.inner is None:
                self.inner = Inner()

    @dataclass
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
async def test_file_reference_serialization(run_worker, executor):
    """P1.4: FileReference flows through Temporal serialization unchanged."""

    @dataclass
    class FileInput(Input):
        ref: FileReference = None  # type: ignore[assignment]

        def __post_init__(self) -> None:
            if self.ref is None:
                self.ref = FileReference()

    @dataclass
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
