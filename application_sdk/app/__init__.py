"""App module - core developer-facing abstractions for building Apps.

Usage::

    from application_sdk.app import App, task, Input, Output

    @dataclass
    class MyInput(Input):
        source: str

    @dataclass
    class MyOutput(Output):
        count: int

    class MyApp(App):
        @task(timeout_seconds=300)
        async def fetch_data(self, input: MyInput) -> MyOutput:
            return MyOutput(count=42)

        async def run(self, input: MyInput) -> MyOutput:
            return await self.fetch_data(input)

Runtime interaction decorators and primitives are also exported here so app code
never needs to import the underlying orchestrator directly::

    from application_sdk.app import App, signal, query, update, wait_condition

    class MyApp(App):
        async def run(self, input: MyInput) -> MyOutput:
            await wait_condition(lambda: self.ready)
            ...

        @signal
        async def unblock(self) -> None:
            self.ready = True

        @query
        def status(self) -> str:
            return self._state

        @update
        async def pause(self, reason: str) -> str:
            self._state = "paused"
            return f"paused: {reason}"
"""

from temporalio.workflow import (
    HandlerUnfinishedPolicy,
    now,
    query,
    signal,
    sleep,
    update,
    uuid4,
    wait_condition,
)

from application_sdk.app.base import App, AppError, NonRetryableError, RetryableError
from application_sdk.app.context import AppContext
from application_sdk.app.entrypoint import EntryPointMetadata, entrypoint
from application_sdk.app.registry import AppRegistry, TaskRegistry
from application_sdk.app.task import TaskMetadata, task
from application_sdk.contracts.base import Input, Output, OutputStatus
from application_sdk.credentials.atlan_client import AtlanClientMixin
from application_sdk.execution.retry import RetryPolicy
from application_sdk.server.mcp.decorators import mcp_tool

__all__ = [
    "App",
    "AppContext",
    "AppError",
    "AppRegistry",
    "AtlanClientMixin",
    "EntryPointMetadata",
    "HandlerUnfinishedPolicy",
    "Input",
    "NonRetryableError",
    "Output",
    "OutputStatus",
    "RetryPolicy",
    "RetryableError",
    "TaskMetadata",
    "TaskRegistry",
    "entrypoint",
    "mcp_tool",
    "now",
    "query",
    "signal",
    "sleep",
    "task",
    "update",
    "uuid4",
    "wait_condition",
]
