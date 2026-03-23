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
                    "No MCP metadata found on activity method, skipping tool registration",
                    method=f.__name__,
                )
                continue

            if mcp_metadata.visible:
                self.logger.info(
                    "Registering MCP tool",
                    tool_name=mcp_metadata.name,
                    description=mcp_metadata.description,
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
                    "Tool is marked as not visible, skipping registration: %s",
                    mcp_metadata.name,
                )

        tools = await self.server.get_tools()
        self.logger.info("Registered %d tools: %s", len(tools), list(tools.keys()))

    async def register_tools_from_registry(self, app_name: str) -> None:
        """Discover @mcp_tool-decorated tasks via the v3 TaskRegistry.

        This is the v3 equivalent of ``register_tools()``. Instead of iterating
        ``(WorkflowInterface, ActivitiesInterface)`` pairs, it reads
        ``TaskRegistry`` for the given app and checks each ``TaskMetadata.func``
        for the ``MCP_METADATA_KEY`` attribute set by ``@mcp_tool``.

        Args:
            app_name: The app name used to look up tasks in the registry.
        """
        from application_sdk.app.registry import TaskRegistry
        from application_sdk.constants import MCP_METADATA_KEY

        tasks = TaskRegistry.get_instance().get_tasks_for_app(app_name)
        for task_meta in tasks:
            mcp_metadata: Optional[MCPMetadata] = getattr(
                task_meta.func, MCP_METADATA_KEY, None
            )
            if not mcp_metadata:
                self.logger.info(
                    "No MCP metadata found on task, skipping tool registration",
                    task_name=task_meta.name,
                )
                continue

            if mcp_metadata.visible:
                self.logger.info(
                    "Registering MCP tool",
                    tool_name=mcp_metadata.name,
                    description=mcp_metadata.description,
                )
                self.server.tool(
                    task_meta.func,
                    name=mcp_metadata.name,
                    description=mcp_metadata.description,
                    *mcp_metadata.args,
                    **mcp_metadata.kwargs,
                )
            else:
                self.logger.info(
                    "Tool is marked as not visible, skipping registration: %s",
                    mcp_metadata.name,
                )

        tools = await self.server.get_tools()
        self.logger.info(
            "Registered MCP tools from registry",
            count=len(tools),
            tools=list(tools.keys()),
        )

    async def get_http_app(self) -> StarletteWithLifespan:
        """
        Get the HTTP app for the MCP server.
        """
        return self.server.http_app()
