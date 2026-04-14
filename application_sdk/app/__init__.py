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
"""

from application_sdk.app.base import App, AppError, NonRetryableError, RetryableError
from application_sdk.app.context import AppContext
from application_sdk.app.entrypoint import EntryPointMetadata, entrypoint
from application_sdk.app.registry import AppRegistry, TaskRegistry
from application_sdk.app.task import TaskMetadata, task
from application_sdk.contracts.base import Input, Output
from application_sdk.credentials.atlan_client import AtlanClientMixin
from application_sdk.execution.retry import RetryPolicy

__all__ = [
    "App",
    "AppContext",
    "AtlanClientMixin",
    "AppError",
    "AppRegistry",
    "EntryPointMetadata",
    "Input",
    "NonRetryableError",
    "Output",
    "RetryableError",
    "RetryPolicy",
    "TaskMetadata",
    "TaskRegistry",
    "entrypoint",
    "task",
]
