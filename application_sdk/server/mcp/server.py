"""
MCP Server implementation using FastMCP for Atlan Application SDK.

This module provides the MCPServer class that automatically discovers
activities marked with @mcp_tool decorators and mounts them on FastAPI
using streamable HTTP transport.
"""

import functools
import inspect
from typing import Any, Callable, Optional

from fastmcp import FastMCP
from fastmcp.server.http import StarletteWithLifespan

from application_sdk.constants import MCP_METADATA_KEY
from application_sdk.observability.logger_adaptor import get_logger
from application_sdk.server.mcp.models import MCPMetadata


def _bind_task_for_mcp(app_name: str, task_meta: Any) -> Callable[..., Any]:
    """Return an MCP-callable wrapper around an unbound ``@task`` method.

    ``TaskMetadata.func`` is the original *unbound* method, so handing it to
    FastMCP directly leaks ``self`` into the tool's JSON schema and every call
    fails validation. Mirror the Temporal execution path instead: instantiate
    the owning App class per call (``AppRegistry.get(app_name).app_cls()``) and
    invoke the original function against that instance. ``self`` is removed
    from the advertised signature so the schema only carries the task's Input.

    The App class is resolved lazily at call time, not registration time,
    because MCP registration runs during server startup and must not depend on
    AppRegistry population order.
    """
    func = task_meta.func
    signature = inspect.signature(func)
    if "self" not in signature.parameters:
        return func

    @functools.wraps(func)
    async def _invoke(*args: Any, **kwargs: Any) -> Any:
        from application_sdk.app.registry import (  # noqa: PLC0415 — circular: app.registry imports execution-related modules
            AppRegistry,
        )

        app_instance = AppRegistry.get_instance().get(app_name).app_cls()
        return await func(app_instance, *args, **kwargs)

    _invoke.__signature__ = signature.replace(  # type: ignore[attr-defined]
        parameters=[
            param
            for param_name, param in signature.parameters.items()
            if param_name != "self"
        ]
    )
    return _invoke


class MCPServer:
    """
    MCP Server using FastMCP with FastAPI mounting capability.

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
            on_duplicate="error",
        )

    async def register_tools_from_registry(self, app_name: str) -> None:
        """Discover @mcp_tool-decorated tasks via the v3 TaskRegistry.

        This is the v3 equivalent of ``register_tools()``. Instead of iterating
        ``(WorkflowInterface, ActivitiesInterface)`` pairs, it reads
        ``TaskRegistry`` for the given app and checks each ``TaskMetadata.func``
        for the ``MCP_METADATA_KEY`` attribute set by ``@mcp_tool``.

        Tool calls execute in-process in the server (no Temporal hop), against
        a fresh App instance per call — matching the Temporal activity
        execution model. ``@task`` timeout/retry semantics do NOT apply on the
        MCP path.

        Args:
            app_name: The app name used to look up tasks in the registry.
        """
        from application_sdk.app.registry import (  # noqa: PLC0415 — circular: app.registry imports execution-related modules
            TaskRegistry,
        )

        tasks = TaskRegistry.get_instance().get_tasks_for_app(app_name)
        for task_meta in tasks:
            mcp_metadata: Optional[MCPMetadata] = getattr(
                task_meta.func, MCP_METADATA_KEY, None
            )
            if not mcp_metadata:
                self.logger.debug(
                    "No MCP metadata found on task %s, skipping tool registration",
                    task_meta.name,
                )
                continue

            if mcp_metadata.visible:
                # conformance: ignore[L006] one-time startup enumeration over a small, statically-configured tool set; production logs are collected at INFO floor, so DEBUG would delete this from observability
                self.logger.info(
                    "Registering MCP tool %s: %s",
                    mcp_metadata.name,
                    mcp_metadata.description,
                )
                self.server.tool(
                    _bind_task_for_mcp(app_name, task_meta),
                    name=mcp_metadata.name,
                    description=mcp_metadata.description,
                    *mcp_metadata.args,
                    **mcp_metadata.kwargs,
                )
            else:
                # conformance: ignore[L006] one-time startup enumeration over a small, statically-configured tool set; production logs are collected at INFO floor, so DEBUG would delete this from observability
                self.logger.info(
                    "Tool is marked as not visible, skipping registration: %s",
                    mcp_metadata.name,
                )

        tools = await self.server.list_tools()
        self.logger.info(
            "Registered %d MCP tools from registry: %s",
            len(tools),
            [tool.name for tool in tools],
        )

    async def get_http_app(self) -> StarletteWithLifespan:
        """
        Get the HTTP app for the MCP server.
        """
        return self.server.http_app()
