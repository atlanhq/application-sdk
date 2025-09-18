"""Unit tests for MCP Server module.

Test Case Summary:
+-------------------------------+-----------------------------------------------+-----------------------+
| Test Class                    | Test Method                                   | Purpose               |
+-------------------------------+-----------------------------------------------+-----------------------+
| TestMCPServerInitialization   | test_initialization_with_name_and_instructions| Basic init            |
+-------------------------------+-----------------------------------------------+-----------------------+
| TestMCPServerToolRegistration | test_register_tools_success                   | Success case          |
|                               | test_register_tools_with_hidden_tools         | Hidden tools          |
|                               | test_register_tools_without_mcp_metadata      | No MCP tools          |
|                               | test_register_tools_multiple_workflows        | Multi-workflow        |
|                               | test_register_tools_empty_list                | Empty list            |
+-------------------------------+-----------------------------------------------+-----------------------+
"""

from typing import Any, Callable, List
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest

from application_sdk.activities import ActivitiesInterface
from application_sdk.decorators.mcp_tool import mcp_tool
from application_sdk.server.mcp import MCPServer
from application_sdk.workflows import WorkflowInterface


class MockActivities(ActivitiesInterface):
    """Mock activities class for testing."""

    @mcp_tool(name="test_tool", description="Test tool description")
    async def test_activity(self, param: str) -> str:
        """Test activity with MCP tool decorator."""
        return f"Test result: {param}"

    @mcp_tool(name="hidden_tool", description="Hidden tool", visible=False)
    async def hidden_activity(self, param: str) -> str:
        """Hidden activity that should not be registered."""
        return f"Hidden result: {param}"

    @mcp_tool(description="Custom description tool")
    async def custom_description_activity(self, param: int) -> int:
        """Activity with custom description."""
        return param * 2

    async def regular_activity(self, param: str) -> str:
        """Regular activity without MCP decorator."""
        return f"Regular result: {param}"

    @mcp_tool(name="duplicate_tool", description="First duplicate")
    async def duplicate_activity_1(self, param: str) -> str:
        """First duplicate activity."""
        return f"Duplicate 1: {param}"

    @mcp_tool(name="duplicate_tool", description="Second duplicate")
    async def duplicate_activity_2(self, param: str) -> str:
        """Second duplicate activity with same name."""
        return f"Duplicate 2: {param}"


class MockWorkflow(WorkflowInterface):
    """Mock workflow class for testing."""

    @staticmethod
    def get_activities(activities: ActivitiesInterface) -> List[Callable[..., Any]]:
        """Return list of activity methods."""
        return [
            activities.test_activity,  # type: ignore
            activities.hidden_activity,  # type: ignore
            activities.custom_description_activity,  # type: ignore
            activities.regular_activity,  # type: ignore
            activities.duplicate_activity_1,  # type: ignore
            activities.duplicate_activity_2,  # type: ignore
        ]


class MockActivitiesNoMCP(ActivitiesInterface):
    """Mock activities class with no MCP tools."""

    async def regular_activity_1(self, param: str) -> str:
        """Regular activity 1."""
        return f"Regular 1: {param}"

    async def regular_activity_2(self, param: int) -> int:
        """Regular activity 2."""
        return param + 1


class MockWorkflowNoMCP(WorkflowInterface):
    """Mock workflow class with no MCP tools."""

    @staticmethod
    def get_activities(activities: ActivitiesInterface) -> List[Callable[..., Any]]:
        """Return list of regular activity methods."""
        return [
            activities.regular_activity_1,  # type: ignore
            activities.regular_activity_2,  # type: ignore
        ]


class TestMCPServerInitialization:
    """Test suite for MCPServer initialization."""

    def test_initialization_with_name_and_instructions(self):
        """Test MCPServer initialization with application name and instructions."""
        with patch("application_sdk.server.mcp.server.FastMCP") as mock_fastmcp:
            mock_fastmcp.return_value = MagicMock()

            instructions = "Test instructions"
            server = MCPServer(application_name="test-app", instructions=instructions)

            # Verify FastMCP initialization
            mock_fastmcp.assert_called_once_with(
                name=f"{server.application_name} MCP",
                instructions=instructions,
                on_duplicate_tools="error",
            )


class TestMCPServerToolRegistration:
    """Test suite for MCPServer tool registration."""

    @pytest.fixture
    def mcp_server(self):
        with patch("application_sdk.server.mcp.server.FastMCP") as mock_fastmcp:
            """Fixture providing MCPServer instance."""
            mock_server = Mock()
            # Mock the async get_tools method
            mock_server.get_tools = AsyncMock(return_value={})
            mock_fastmcp.return_value = mock_server
            return MCPServer(application_name="test-app")

    async def test_register_tools_success(self, mcp_server):
        """Test successful tool registration."""
        workflow_and_activities = [(MockWorkflow, MockActivities)]

        await mcp_server.register_tools(  # type: ignore
            workflow_and_activities
        )

        # Verify tool registration calls
        # Table-driven test for tool registration and descriptions
        assert mcp_server.server.tool.call_count == 4  # type: ignore

        tool_calls = mcp_server.server.tool.call_args_list  # type: ignore

        # Define expected tool registrations as a table of (name, description)
        expected_tools = {
            "test_tool": "Test tool description",
            "custom_description_activity": "Custom description tool",
            "duplicate_tool": "Second duplicate",  # Last registration wins for duplicates
        }

        # Build a lookup of actual tool registrations by name
        actual_tools = {
            call.kwargs.get("name"): call.kwargs.get("description")  # type: ignore
            for call in tool_calls  # type: ignore
        }

        # Table-driven assertions
        for name, description in expected_tools.items():
            assert name in actual_tools
            assert actual_tools[name] == description

    async def test_register_tools_with_hidden_tools(self, mcp_server):
        """Test tool registration with hidden tools (visible=False)."""
        workflow_and_activities = [(MockWorkflow, MockActivities)]

        await mcp_server.register_tools(  # type: ignore
            workflow_and_activities
        )

        # Verify only visible tools are registered (4 out of 5 MCP tools, including duplicates)
        assert mcp_server.server.tool.call_count == 4  # type: ignore

        # Verify hidden tool was not registered
        tool_calls = mcp_server.server.tool.call_args_list  # type: ignore
        tool_names = [call.kwargs.get("name") for call in tool_calls]  # type: ignore
        assert "hidden_tool" not in tool_names

    async def test_register_tools_without_mcp_metadata(self, mcp_server):
        """Test tool registration with activities that have no MCP metadata."""
        workflow_and_activities = [(MockWorkflowNoMCP, MockActivitiesNoMCP)]

        await mcp_server.register_tools(  # type: ignore
            workflow_and_activities
        )

        # Verify no tools were registered
        mcp_server.server.tool.assert_not_called()  # type: ignore
        mcp_server.server.get_tools.assert_called_once()  # type: ignore

    async def test_register_tools_multiple_workflows(self, mcp_server):
        """Test tool registration with multiple workflow/activities classes."""
        workflow_and_activities = [
            (MockWorkflow, MockActivities),
            (MockWorkflowNoMCP, MockActivitiesNoMCP),
        ]

        await mcp_server.register_tools(  # type: ignore
            workflow_and_activities
        )

        # Verify tools from first workflow were registered (4 tools from MockWorkflow)
        assert mcp_server.server.tool.call_count == 4  # type: ignore
        mcp_server.server.get_tools.assert_called_once()  # type: ignore

    async def test_register_tools_empty_list(self, mcp_server):
        """Test tool registration with empty workflow/activities list."""
        workflow_and_activities = []

        await mcp_server.register_tools(  # type: ignore
            workflow_and_activities
        )

        # Verify no tools were registered
        mcp_server.server.tool.assert_not_called()  # type: ignore
        mcp_server.server.get_tools.assert_called_once()  # type: ignore
