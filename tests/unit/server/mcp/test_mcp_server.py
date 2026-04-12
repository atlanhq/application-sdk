"""Unit tests for MCP Server module.

Test Case Summary:
+-------------------------------+-----------------------------------------------+-----------------------+
| Test Class                    | Test Method                                   | Purpose               |
+-------------------------------+-----------------------------------------------+-----------------------+
| TestMCPServerInitialization   | test_initialization_with_name_and_instructions| Basic init            |
+-------------------------------+-----------------------------------------------+-----------------------+
"""

from unittest.mock import MagicMock, patch

from application_sdk.server.mcp import MCPServer


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
