"""Unit tests for MCPServer.register_tools_from_registry() (v3 discovery path).

Verifies that the v3 task-registry-based tool discovery works the same way as
the v2 ``register_tools()`` path: visible ``@mcp_tool``-decorated ``@task``
methods are registered, hidden ones are skipped, and plain tasks (no decorator)
are ignored.

Test Case Summary:
+--------------------------------------+----------------------------------------------+
| Test                                 | Purpose                                      |
+--------------------------------------+----------------------------------------------+
| test_visible_tool_registered         | @mcp_tool(visible=True) task registers       |
| test_hidden_tool_skipped             | @mcp_tool(visible=False) task is skipped     |
| test_plain_task_skipped              | task without @mcp_tool is skipped            |
| test_multiple_tools                  | multiple decorated tasks all register        |
| test_empty_registry                  | app with no tasks registers nothing          |
| test_custom_name_and_description     | name/description overrides pass through      |
+--------------------------------------+----------------------------------------------+
"""

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from application_sdk.decorators.mcp_tool import mcp_tool
from application_sdk.server.mcp import MCPServer

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_task_meta(func: Any, name: str = "") -> MagicMock:
    """Return a minimal TaskMetadata-like mock."""
    meta = MagicMock()
    meta.name = name or func.__name__
    meta.func = func
    return meta


# ---------------------------------------------------------------------------
# Sample callables decorated with @mcp_tool
# ---------------------------------------------------------------------------


@mcp_tool(name="fetch_schemas", description="Fetch all schemas")
async def _fetch_schemas(self: Any, input: Any) -> Any:
    """Fetch schemas task."""
    return None


@mcp_tool(name="hidden_op", description="Hidden op", visible=False)
async def _hidden_op(self: Any, input: Any) -> Any:
    """Hidden task."""
    return None


async def _plain_task(self: Any, input: Any) -> Any:
    """Task with no @mcp_tool decorator."""
    return None


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def mcp_server() -> MCPServer:
    with patch("application_sdk.server.mcp.server.FastMCP") as mock_fastmcp:
        mock_inner = MagicMock()
        mock_inner.get_tools = AsyncMock(return_value={})
        mock_fastmcp.return_value = mock_inner
        return MCPServer(application_name="test-app")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestRegisterToolsFromRegistry:
    async def test_visible_tool_registered(self, mcp_server: MCPServer) -> None:
        tasks = [_make_task_meta(_fetch_schemas)]

        with patch(
            "application_sdk.app.registry.TaskRegistry.get_instance"
        ) as mock_reg:
            mock_reg.return_value.get_tasks_for_app.return_value = tasks
            await mcp_server.register_tools_from_registry("test-app")

        mcp_server.server.tool.assert_called_once()  # type: ignore[union-attr]
        call_kwargs = mcp_server.server.tool.call_args.kwargs  # type: ignore[union-attr]
        assert call_kwargs["name"] == "fetch_schemas"
        assert call_kwargs["description"] == "Fetch all schemas"

    async def test_hidden_tool_skipped(self, mcp_server: MCPServer) -> None:
        tasks = [_make_task_meta(_hidden_op)]

        with patch(
            "application_sdk.app.registry.TaskRegistry.get_instance"
        ) as mock_reg:
            mock_reg.return_value.get_tasks_for_app.return_value = tasks
            await mcp_server.register_tools_from_registry("test-app")

        mcp_server.server.tool.assert_not_called()  # type: ignore[union-attr]

    async def test_plain_task_skipped(self, mcp_server: MCPServer) -> None:
        tasks = [_make_task_meta(_plain_task)]

        with patch(
            "application_sdk.app.registry.TaskRegistry.get_instance"
        ) as mock_reg:
            mock_reg.return_value.get_tasks_for_app.return_value = tasks
            await mcp_server.register_tools_from_registry("test-app")

        mcp_server.server.tool.assert_not_called()  # type: ignore[union-attr]

    async def test_multiple_tools(self, mcp_server: MCPServer) -> None:
        tasks = [
            _make_task_meta(_fetch_schemas),
            _make_task_meta(_hidden_op),
            _make_task_meta(_plain_task),
        ]

        with patch(
            "application_sdk.app.registry.TaskRegistry.get_instance"
        ) as mock_reg:
            mock_reg.return_value.get_tasks_for_app.return_value = tasks
            await mcp_server.register_tools_from_registry("test-app")

        # Only _fetch_schemas is visible
        assert mcp_server.server.tool.call_count == 1  # type: ignore[union-attr]

    async def test_empty_registry(self, mcp_server: MCPServer) -> None:
        with patch(
            "application_sdk.app.registry.TaskRegistry.get_instance"
        ) as mock_reg:
            mock_reg.return_value.get_tasks_for_app.return_value = []
            await mcp_server.register_tools_from_registry("test-app")

        mcp_server.server.tool.assert_not_called()  # type: ignore[union-attr]
        mcp_server.server.get_tools.assert_called_once()  # type: ignore[union-attr]

    async def test_custom_name_and_description(self, mcp_server: MCPServer) -> None:
        @mcp_tool(name="my_custom_tool", description="My custom description")
        async def _custom(self: Any, input: Any) -> Any:
            return None

        tasks = [_make_task_meta(_custom)]

        with patch(
            "application_sdk.app.registry.TaskRegistry.get_instance"
        ) as mock_reg:
            mock_reg.return_value.get_tasks_for_app.return_value = tasks
            await mcp_server.register_tools_from_registry("test-app")

        call_kwargs = mcp_server.server.tool.call_args.kwargs  # type: ignore[union-attr]
        assert call_kwargs["name"] == "my_custom_tool"
        assert call_kwargs["description"] == "My custom description"
