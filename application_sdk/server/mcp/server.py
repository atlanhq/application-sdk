"""
MCP Server implementation using FastMCP for Atlan Application SDK.

This module provides the MCPServer class that automatically discovers
activities marked with @mcp_tool decorators and mounts them on FastAPI
using streamable HTTP transport.
"""

from typing import Any, Callable, List, Optional, Tuple, Type

from fastmcp import FastMCP
from fastmcp.server.http import StarletteWithLifespan

from application_sdk.activities import ActivitiesInterface
from application_sdk.constants import MCP_METADATA_KEY
from application_sdk.observability.logger_adaptor import get_logger
from application_sdk.server.mcp.models import MCPMetadata
from application_sdk.workflows import WorkflowInterface


class MCPServer:
    """
    MCP Server using FastMCP 2.0 with FastAPI mounting capability.

    This server automatically discovers activities marked with @mcp_tool
    and creates a FastMCP server that can be mounted on FastAPI.
    """

    def __init__(self, application_name: str, instructions: Optional[str] = None):
        """
        Initialize the MCP server.

        Args:
            application_name (str): Name of the application
            instructions (Optional[str]): Description for the MCP server
        """
        self.application_name = application_name

        self.logger = get_logger(__name__)

        # FastMCP Server
        self.server = FastMCP(
            name=f"{application_name} MCP",
            instructions=instructions,
            on_duplicate_tools="error",
        )

    async def register_tools(
        self,
        workflow_and_activities_classes: List[
            Tuple[Type[WorkflowInterface], Type[ActivitiesInterface]]
        ],
    ) -> None:
        """
        Discover activities marked with @mcp_tool and register them.

        Args:
            workflow_and_activities_classes: List of (workflow_class, activities_class) tuples
        """
        activity_methods: List[Callable[..., Any]] = []
        for workflow_class, activities_class in workflow_and_activities_classes:
            activities_instance = activities_class()
            activity_methods.extend(workflow_class.get_activities(activities_instance))  # type: ignore

        for f in activity_methods:
            mcp_metadata: Optional[MCPMetadata] = getattr(f, MCP_METADATA_KEY, None)
            if not mcp_metadata:
                self.logger.info(
                    f"No MCP metadata found on activity method {f.__name__}. Skipping tool registration"
                )
                continue

            if mcp_metadata.visible:
                self.logger.info(
                    f"Registering tool {mcp_metadata.name} with description: {mcp_metadata.description}"
                )
                self.server.tool(
                    f,
                    name=mcp_metadata.name,
                    description=mcp_metadata.description,
                    *mcp_metadata.args,
                    **mcp_metadata.kwargs,
                )
            else:
                self.logger.info(
                    f"Tool {mcp_metadata.name} is marked as not visible. Skipping tool registration"
                )

        tools = await self.server.get_tools()
        self.logger.info(f"Registered {len(tools)} tools: {list(tools.keys())}")

    async def get_http_app(self) -> StarletteWithLifespan:
        """
        Get the HTTP app for the MCP server.
        """
        return self.server.http_app()
