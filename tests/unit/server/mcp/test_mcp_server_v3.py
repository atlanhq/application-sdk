"""Unit tests for MCPServer.register_tools_from_registry() (v3 discovery path).

These tests run against **real fastmcp** — there is no FastMCP mock in this
module. A real ``App`` subclass registers itself with the real ``AppRegistry`` /
``TaskRegistry``, and the server is driven through fastmcp's in-memory client
transport (``fastmcp.Client(server)``), so the advertised JSON schema is the one
fastmcp actually generates and every call goes through fastmcp's own argument
validation and result serialization. Nothing external is involved: no port, no
subprocess, no Temporal server.

Why no mock: mocking FastMCP is what let three breaks ship. A ``MagicMock``
accepts any constructor kwarg (so the fastmcp 2.x ``on_duplicate_tools=``
survived the 3.x bump), auto-creates any method (so the removed ``get_tools()``
still "worked"), and never builds a tool schema (so ``self`` leaking into every
tool's schema was invisible). Each of those is asserted here behaviourally.

Test Case Summary:
+-------------------------------------------+-------------------------------------------+
| Test                                      | Purpose                                   |
+-------------------------------------------+-------------------------------------------+
| test_only_visible_decorated_tasks_exposed | visible @mcp_tool tasks are the only      |
|                                           | tools: hidden, plain and inherited        |
|                                           | @task methods are absent                  |
| test_schema_omits_self                    | tool schema carries `input` only — `self` |
|                                           | must not leak (real fastmcp schema gen)   |
| test_name_and_description_from_decorator  | decorator name/description reach the wire |
| test_call_returns_task_result             | a real MCP call executes the task body    |
| test_each_call_binds_fresh_app_instance   | per-call App instantiation, mirroring the |
|                                           | Temporal activity path                    |
| test_duplicate_tool_name_is_an_error      | on_duplicate="error" is really wired      |
|                                           | (fastmcp's default only warns)            |
| test_http_app_exposes_mcp_route           | http_app() builds on the pinned fastmcp / |
|                                           | starlette pair and serves /mcp            |
| test_app_with_no_tasks_registers_nothing  | unknown app registers no tools            |
+-------------------------------------------+-------------------------------------------+
"""

import json

import pytest
from fastmcp import Client

from application_sdk.server.mcp import MCPServer
from tests.unit.server.mcp.conftest import APP_NAME, ProbeApp


@pytest.fixture
async def mcp_server(probe: ProbeApp) -> MCPServer:
    """A real MCPServer with the probe app's tools registered."""
    server = MCPServer(application_name=APP_NAME)
    await server.register_tools_from_registry(APP_NAME)
    return server


# ---------------------------------------------------------------------------
# Discovery — what the client actually sees
# ---------------------------------------------------------------------------


class TestToolDiscovery:
    async def test_only_visible_decorated_tasks_exposed(
        self, mcp_server: MCPServer
    ) -> None:
        """visible=True @mcp_tool tasks, and nothing else.

        The probe app also carries a hidden tool, an undecorated @task, and the
        @task methods it inherits from App (cleanup_files, upload, ...) — none
        of which may be advertised.
        """
        async with Client(mcp_server.server) as client:
            names = {tool.name for tool in await client.list_tools()}

        assert names == {"fetch_schemas"}

    async def test_schema_omits_self(self, mcp_server: MCPServer) -> None:
        """The tool schema must carry the task's Input and nothing else.

        TaskMetadata.func is the unbound method; handing it to FastMCP directly
        put ``self`` in the schema as a required argument and every call died
        with "Missing required argument: self". This asserts against the schema
        fastmcp really generates, which is what makes it a regression test.
        """
        async with Client(mcp_server.server) as client:
            tool = next(
                t for t in await client.list_tools() if t.name == "fetch_schemas"
            )

        assert set(tool.inputSchema["properties"]) == {"input"}
        assert tool.inputSchema["required"] == ["input"]
        assert "self" not in json.dumps(tool.inputSchema)

    async def test_name_and_description_from_decorator(
        self, mcp_server: MCPServer
    ) -> None:
        async with Client(mcp_server.server) as client:
            tool = next(
                t for t in await client.list_tools() if t.name == "fetch_schemas"
            )

        assert tool.description == "Fetch all schemas"


# ---------------------------------------------------------------------------
# Execution — a real MCP call, end to end in-process
# ---------------------------------------------------------------------------


class TestToolExecution:
    async def test_call_returns_task_result(self, mcp_server: MCPServer) -> None:
        async with Client(mcp_server.server) as client:
            result = await client.call_tool("fetch_schemas", {"input": {"value": "a"}})

        assert result.structured_content is not None
        assert result.structured_content["result"] == "schemas:a"

    async def test_each_call_binds_fresh_app_instance(
        self, mcp_server: MCPServer, probe: ProbeApp
    ) -> None:
        """Each call runs against its own App instance, as on the Temporal path."""
        async with Client(mcp_server.server) as client:
            await client.call_tool("fetch_schemas", {"input": {"value": "a"}})
            await client.call_tool("fetch_schemas", {"input": {"value": "b"}})

        assert len(probe.instances) == 2
        assert all(isinstance(i, probe.app_cls) for i in probe.instances)
        assert probe.instances[0] is not probe.instances[1]


# ---------------------------------------------------------------------------
# fastmcp wiring — the API surface the mocks used to hide
# ---------------------------------------------------------------------------


class TestFastMCPWiring:
    async def test_duplicate_tool_name_is_an_error(self, mcp_server: MCPServer) -> None:
        """``on_duplicate="error"`` has to reach the real FastMCP constructor.

        fastmcp's default is to warn and replace, so a second registration
        raising is the observable proof the kwarg landed — the fastmcp 2.x
        spelling (``on_duplicate_tools=``) is a TypeError on 3.x and never gets
        this far.
        """
        with pytest.raises(ValueError, match="already exists"):
            await mcp_server.register_tools_from_registry(APP_NAME)

    async def test_http_app_exposes_mcp_route(self, mcp_server: MCPServer) -> None:
        """http_app() must build against the pinned fastmcp/starlette pair."""
        http_app = await mcp_server.get_http_app()

        assert "/mcp" in {getattr(route, "path", "") for route in http_app.routes}

    async def test_app_with_no_tasks_registers_nothing(self) -> None:
        server = MCPServer(application_name="unregistered-app")

        await server.register_tools_from_registry("unregistered-app")

        assert await server.server.list_tools() == []
