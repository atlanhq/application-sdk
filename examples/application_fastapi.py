"""FastAPI handler example using v3 Handler + App pattern.

Demonstrates how to implement a Handler with typed contracts for
authentication, preflight checks, and metadata discovery, alongside
a simple App. Uses run_dev_combined() which starts both the HTTP
handler service and the Temporal worker in a single process.

Usage:
    python examples/application_fastapi.py
"""

import asyncio

from application_sdk.app import App, Input, Output, task
from application_sdk.handler import (
    AuthInput,
    AuthOutput,
    AuthStatus,
    Handler,
    MetadataInput,
    MetadataOutput,
    PreflightCheck,
    PreflightInput,
    PreflightOutput,
    PreflightStatus,
)
from application_sdk.main import run_dev_combined


class SampleInput(Input):
    """Input contract for the sample workflow."""

    name: str = ""


class SampleOutput(Output):
    """Output contract for the sample workflow."""

    status: str = "completed"


class SampleHandler(Handler):
    """Handler implementing auth, preflight, and metadata endpoints.

    In v3, handlers are stateless — no load() method. The framework
    injects self.context before each call and clears it after.
    """

    async def test_auth(self, input: AuthInput) -> AuthOutput:
        """Test authentication credentials."""
        return AuthOutput(status=AuthStatus.SUCCESS)

    async def preflight_check(self, input: PreflightInput) -> PreflightOutput:
        """Run preflight validation checks."""
        return PreflightOutput(
            status=PreflightStatus.READY,
            checks=[
                PreflightCheck(
                    name="databaseSchemaCheck",
                    passed=True,
                    message="Schemas and Databases check successful",
                ),
                PreflightCheck(
                    name="tablesCheck",
                    passed=True,
                    message="Tables check successful. Table count: 2",
                ),
            ],
        )

    async def fetch_metadata(self, input: MetadataInput) -> MetadataOutput:
        """Return available metadata for the connector UI."""
        return MetadataOutput(objects=[])


class SampleApp(App):
    """Simple app that pairs with the SampleHandler."""

    @task(timeout_seconds=30)
    async def process(self, input: SampleInput) -> SampleOutput:
        """A placeholder task."""
        return SampleOutput(status="completed")

    async def run(self, input: SampleInput) -> SampleOutput:  # type: ignore[override]
        return await self.process(input)


if __name__ == "__main__":
    # ATLAN_HANDLER_MODULE env var can point to the handler class.
    # For local dev, run_dev_combined discovers the handler automatically
    # if it is defined in the same module or set via env var.
    asyncio.run(
        run_dev_combined(
            SampleApp,
            example_input={"name": "test"},
        )
    )
