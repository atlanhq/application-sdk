"""Hello World example using v3 App + @task pattern.

Demonstrates the simplest possible application: a single App class
with one task that returns a greeting message. Uses typed Input/Output
contracts and run_dev_combined() for local development.

Usage:
    python examples/application_hello_world.py
"""

import asyncio

from application_sdk.app import App, Input, Output, task
from application_sdk.main import run_dev_combined
from application_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)


class HelloInput(Input):
    """Input contract for the hello world app."""

    name: str = "World"


class HelloOutput(Output):
    """Output contract for the hello world app."""

    message: str = ""


class HelloWorldApp(App):
    """Minimal hello world application.

    Shows the v3 pattern: one class, @task for side-effects,
    run() for orchestration, typed contracts at every boundary.
    """

    @task(timeout_seconds=10, heartbeat_timeout_seconds=10)
    async def demo_task(self, input: HelloInput) -> HelloOutput:
        """A simple task that returns a greeting."""
        logger.info("Running demo_task", extra={"name": input.name})
        return HelloOutput(message=f"Hello, {input.name}!")

    async def run(self, input: HelloInput) -> HelloOutput:  # type: ignore[override]
        """Orchestrate the hello world workflow."""
        return await self.demo_task(input)


if __name__ == "__main__":
    asyncio.run(
        run_dev_combined(
            HelloWorldApp,
            example_input={"name": "World"},
        )
    )
