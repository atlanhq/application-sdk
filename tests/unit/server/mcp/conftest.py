"""Shared probe App for the MCP server tests.

The MCP tests deliberately avoid mocking FastMCP or the registries: a real
``App`` subclass registers itself with the real ``AppRegistry`` /
``TaskRegistry``, and the tests drive a real FastMCP server. This module owns
the probe App so both the in-memory transport tests and the mounted-HTTP tests
exercise the same task surface.
"""

from collections.abc import Iterator
from dataclasses import dataclass, field

import pytest

from application_sdk.app.base import App
from application_sdk.app.registry import AppRegistry, TaskRegistry
from application_sdk.app.task import task
from application_sdk.contracts.base import Input, Output
from application_sdk.server.mcp.decorators import mcp_tool
from application_sdk.testing import clean_app_registry, clean_task_registry

# Referenced so the fixture signatures below pick them up as dependencies.
__all__ = ["clean_app_registry", "clean_task_registry"]

APP_NAME = "mcp-probe-app"


# Plain Pydantic subclasses — @dataclass would generate a conflicting __init__
# that breaks Pydantic's __pydantic_fields_set__ initialisation.
class ProbeInput(Input):
    value: str = ""


class ProbeOutput(Output):
    result: str = ""


@dataclass
class ProbeApp:
    """The probe App class plus the instances the SDK bound to each call."""

    app_cls: type[App]
    instances: list[App] = field(default_factory=list)


@pytest.fixture
def probe(
    clean_app_registry: AppRegistry,
    clean_task_registry: TaskRegistry,
) -> Iterator[ProbeApp]:
    """Register a real App whose tasks cover every discovery branch.

    The registries are process-wide singletons, so they are reset around the
    fixture (via the shared ``clean_*_registry`` fixtures) and the App class is
    declared inside it — that keeps registration from leaking into (or being
    clobbered by) other modules.
    """
    instances: list[App] = []

    class McpProbeApp(App):
        name = APP_NAME

        async def run(self, input: ProbeInput) -> ProbeOutput:
            return ProbeOutput(result=input.value)

        @task
        @mcp_tool(name="fetch_schemas", description="Fetch all schemas")
        async def fetch_schemas(self, input: ProbeInput) -> ProbeOutput:
            instances.append(self)
            return ProbeOutput(result=f"schemas:{input.value}")

        @task
        @mcp_tool(name="hidden_op", description="Hidden op", visible=False)
        async def hidden_op(self, input: ProbeInput) -> ProbeOutput:
            return ProbeOutput(result="hidden")

        @task
        async def plain_task(self, input: ProbeInput) -> ProbeOutput:
            return ProbeOutput(result="plain")

    yield ProbeApp(app_cls=McpProbeApp, instances=instances)
