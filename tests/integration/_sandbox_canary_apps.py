"""Apps for the workflow-sandbox canary (``test_workflow_sandbox_canary.py``).

They live in their own module, not in the test module, because the canary
starts its worker without passing ``tests`` through the sandbox: Temporal
then re-imports this module inside the sandbox for every workflow run, the
way it re-imports a consumer app's module in production.
"""

from datetime import datetime

from application_sdk.app.base import App
from application_sdk.app.task import task
from application_sdk.contracts.base import Input, Output


class CanaryInput(Input):
    value: int = 0


class CanaryOutput(Output):
    result: int = 0
    note: str = ""


class SandboxCleanApp(App):
    """Deterministic workflow code: must complete under the sandbox."""

    name = "sandbox-clean-app"

    @task
    async def add_one(self, input: CanaryInput) -> CanaryOutput:
        # Activity code is not sandboxed: a clock read here is allowed.
        return CanaryOutput(result=input.value + 1, note=datetime.now().isoformat())

    async def run(self, input: CanaryInput) -> CanaryOutput:
        return await self.add_one(input)


class SandboxCanaryApp(App):
    """Non-deterministic workflow code: the sandbox must reject it."""

    name = "sandbox-canary-app"

    async def run(self, input: CanaryInput) -> CanaryOutput:
        # A wall-clock read in workflow code. Replay of this run on another
        # worker would see a different value, so the sandbox must refuse it.
        return CanaryOutput(result=input.value, note=datetime.now().isoformat())
