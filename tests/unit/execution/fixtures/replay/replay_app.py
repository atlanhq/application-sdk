"""The App whose recorded histories the workflow replay tests replay.

Module-level, like a consumer App: the sandbox re-imports this module for every
run. Its registered name (``replay-fixture-app``, derived from the class name)
is the workflow type in every committed history, so renaming the class
invalidates the fixtures; re-record them instead (see ``harness.RECORD_ENV``).
"""

from __future__ import annotations

from application_sdk.app.base import App
from application_sdk.app.task import task
from application_sdk.contracts.base import Input, Output


class ReplayInput(Input):
    value: int = 0


class ReplayOutput(Output):
    result: int = 0


class ReplayFixtureApp(App):
    """One task, then the default ``on_complete`` (cleanup on by default)."""

    @task(timeout_seconds=30, retry_max_attempts=1)
    async def add_one(self, input: ReplayInput) -> ReplayOutput:
        return ReplayOutput(result=input.value + 1)

    async def run(self, input: ReplayInput) -> ReplayOutput:
        return await self.add_one(input)
